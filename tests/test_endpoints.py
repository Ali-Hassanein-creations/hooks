from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient
from sqlalchemy import func, update

Headers = dict[str, str]
MakeTenant = Callable[[], Awaitable[Headers]]
EP = {"url": "https://example.com/hook", "event_types": ["order.created"]}


@pytest.mark.parametrize(
    "auth", [None, "Bearer nope", "Bearer whk_deadbeef_wrong", "Basic whk_x_y"]
)
async def test_bad_keys_are_401(client: AsyncClient, auth: str | None) -> None:
    headers = {"Authorization": auth} if auth else {}
    assert (await client.get("/v1/endpoints", headers=headers)).status_code == 401


async def test_revoked_key_is_401(client: AsyncClient, make_tenant: MakeTenant) -> None:
    from webhooks.db import Session
    from webhooks.models import ApiKey

    h = await make_tenant()
    assert (await client.get("/v1/endpoints", headers=h)).status_code == 200
    async with Session() as s:
        await s.execute(update(ApiKey).values(revoked_at=func.now()))
        await s.commit()
    assert (await client.get("/v1/endpoints", headers=h)).status_code == 401


@pytest.mark.parametrize(
    "url", ["ftp://example.com", "https://example.com:5432/x", "not a url", "http://"]
)
async def test_rejects_bad_urls(client: AsyncClient, make_tenant: MakeTenant, url: str) -> None:
    r = await client.post("/v1/endpoints", json={**EP, "url": url}, headers=await make_tenant())
    assert r.status_code == 422


async def test_crud_and_secret_shown_once(client: AsyncClient, make_tenant: MakeTenant) -> None:
    h = await make_tenant()
    created = (await client.post("/v1/endpoints", json=EP, headers=h)).json()
    assert created["secret"].startswith("whsec_")
    path = f"/v1/endpoints/{created['id']}"

    got = (await client.get(path, headers=h)).json()
    assert "secret" not in got and got["url"] == EP["url"]

    patched = await client.patch(path, json={"status": "disabled"}, headers=h)
    assert patched.json()["status"] == "disabled"

    assert (await client.delete(path, headers=h)).status_code == 204
    assert (await client.get(path, headers=h)).status_code == 404
    assert (await client.get("/v1/endpoints", headers=h)).json()["data"] == []


async def test_tenant_isolation(client: AsyncClient, make_tenant: MakeTenant) -> None:
    a, b = await make_tenant(), await make_tenant()
    ep = (await client.post("/v1/endpoints", json=EP, headers=a)).json()
    path = f"/v1/endpoints/{ep['id']}"
    assert (await client.get(path, headers=b)).status_code == 404
    assert (await client.patch(path, json={"status": "disabled"}, headers=b)).status_code == 404
    assert (await client.delete(path, headers=b)).status_code == 404
    assert (await client.get("/v1/endpoints", headers=b)).json()["data"] == []


async def test_cursor_pagination_returns_each_row_once(
    client: AsyncClient, make_tenant: MakeTenant
) -> None:
    h = await make_tenant()
    ids = {(await client.post("/v1/endpoints", json=EP, headers=h)).json()["id"] for _ in range(7)}
    seen: list[str] = []
    params: dict[str, str | int] = {"limit": 3}
    while True:
        page = (await client.get("/v1/endpoints", params=params, headers=h)).json()
        seen += [e["id"] for e in page["data"]]
        if not page["next_cursor"]:
            break
        params["cursor"] = page["next_cursor"]
    assert len(seen) == 7 and set(seen) == ids
