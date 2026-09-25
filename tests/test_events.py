import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from httpx import AsyncClient
from sqlalchemy import func, select

Headers = dict[str, str]
MakeTenant = Callable[[], Awaitable[Headers]]
EVENT = {"event_type": "order.created", "payload": {"order_id": 42}}


async def _endpoint(client: AsyncClient, h: Headers, types: list[str], **patch: Any) -> str:
    body = {"url": "https://example.com/hook", "event_types": types}
    ep: str = (await client.post("/v1/endpoints", json=body, headers=h)).json()["id"]
    if patch:
        await client.patch(f"/v1/endpoints/{ep}", json=patch, headers=h)
    return ep


async def _count(model: Any) -> int:
    from webhooks.db import Session

    async with Session() as s:
        return int(await s.scalar(select(func.count()).select_from(model)) or 0)


async def test_fans_out_only_to_matching_enabled_endpoints(
    client: AsyncClient, make_tenant: MakeTenant
) -> None:
    h, other = await make_tenant(), await make_tenant()
    match = await _endpoint(client, h, ["order.created", "order.paid"])
    await _endpoint(client, h, ["order.paid"])  # wrong type
    await _endpoint(client, h, ["order.created"], status="disabled")
    deleted = await _endpoint(client, h, ["order.created"])
    await client.delete(f"/v1/endpoints/{deleted}", headers=h)
    await _endpoint(client, other, ["order.created"])  # other tenant

    r = await client.post("/v1/events", json=EVENT, headers={**h, "Idempotency-Key": "k1"})
    assert r.status_code == 202
    detail = (await client.get(f"/v1/events/{r.json()['id']}", headers=h)).json()
    assert [(d["endpoint_id"], d["status"]) for d in detail["deliveries"]] == [(match, "pending")]
    assert (await client.get(f"/v1/events/{r.json()['id']}", headers=other)).status_code == 404


async def test_idempotency_key_required(client: AsyncClient, make_tenant: MakeTenant) -> None:
    r = await client.post("/v1/events", json=EVENT, headers=await make_tenant())
    assert r.status_code == 422


async def test_duplicate_returns_original_with_200(
    client: AsyncClient, make_tenant: MakeTenant
) -> None:
    h = {**await make_tenant(), "Idempotency-Key": "same"}
    first = await client.post("/v1/events", json=EVENT, headers=h)
    second = await client.post("/v1/events", json=EVENT, headers=h)
    assert (first.status_code, second.status_code) == (202, 200)
    assert first.json()["id"] == second.json()["id"]


async def test_concurrent_duplicates_create_one_event(
    client: AsyncClient, make_tenant: MakeTenant
) -> None:
    from webhooks.models import Delivery, Event

    h = await make_tenant()
    await _endpoint(client, h, ["order.created"])
    await _endpoint(client, h, ["order.created"])
    hk = {**h, "Idempotency-Key": "race"}

    rs = await asyncio.gather(
        *(client.post("/v1/events", json=EVENT, headers=hk) for _ in range(20))
    )

    assert sorted(r.status_code for r in rs) == [200] * 19 + [202]
    assert len({r.json()["id"] for r in rs}) == 1
    assert await _count(Event) == 1
    assert await _count(Delivery) == 2


async def test_same_key_different_tenants_are_independent(
    client: AsyncClient, make_tenant: MakeTenant
) -> None:
    a, b = await make_tenant(), await make_tenant()
    ra = await client.post("/v1/events", json=EVENT, headers={**a, "Idempotency-Key": "k"})
    rb = await client.post("/v1/events", json=EVENT, headers={**b, "Idempotency-Key": "k"})
    assert (ra.status_code, rb.status_code) == (202, 202)
    assert ra.json()["id"] != rb.json()["id"]
