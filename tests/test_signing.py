from collections.abc import Awaitable, Callable

import httpx
import pytest
from sqlalchemy import text

from webhooks import worker
from webhooks.db import Session
from webhooks.verify import sign, verify

Headers = dict[str, str]
MakeTenant = Callable[[], Awaitable[Headers]]
SECRET = "whsec_test"  # noqa: S105 - fake secret for tests
BODY = b'{"order_id":42}'
TS = "1760000000"
GOOD = f"v1={sign(SECRET, TS, BODY)}"


def test_valid_signature_verifies() -> None:
    assert verify(SECRET, BODY, TS, GOOD, now=int(TS))


@pytest.mark.parametrize(
    ("secret", "body", "timestamp", "header", "now"),
    [
        (SECRET, b'{"order_id":43}', TS, GOOD, int(TS)),  # tampered body
        (SECRET, b'{"order_id": 42}', TS, GOOD, int(TS)),  # re-serialised body
        ("whsec_other", BODY, TS, GOOD, int(TS)),  # wrong secret
        (SECRET, BODY, TS, GOOD, int(TS) + 301),  # older than 5 minutes: a replay
        (SECRET, BODY, TS, GOOD, int(TS) - 301),  # too far in the future
        (SECRET, BODY, str(int(TS) + 1), GOOD, int(TS)),  # timestamp swapped
        (SECRET, BODY, "not-a-number", GOOD, int(TS)),
        (SECRET, BODY, TS, GOOD.removeprefix("v1="), int(TS)),  # missing scheme
        (SECRET, BODY, TS, "v1=é", int(TS)),  # non-ASCII must not raise
        (SECRET, BODY, TS, "", int(TS)),
    ],
)
def test_bad_signatures_are_rejected(
    secret: str, body: bytes, timestamp: str, header: str, now: int
) -> None:
    assert not verify(secret, body, timestamp, header, now=now)


def test_any_of_several_signatures_verifies() -> None:
    header = f"v1={sign('whsec_new', TS, BODY)}, {GOOD}"
    assert verify(SECRET, BODY, TS, header, now=int(TS))
    assert verify("whsec_new", BODY, TS, header, now=int(TS))


async def _deliver(client: httpx.AsyncClient, h: Headers, key: str) -> httpx.Request:
    r = await client.post(
        "/v1/events",
        json={"event_type": "order.created", "payload": {"order_id": 42}},
        headers={**h, "Idempotency-Key": key},
    )
    assert r.status_code == 202
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as http:
        await worker.run_once(http)
    [req] = calls
    return req


def _verifies(secret: str, req: httpx.Request) -> bool:
    return verify(
        secret, req.content, req.headers["X-Webhook-Timestamp"], req.headers["X-Webhook-Signature"]
    )


async def test_secret_rotation_signs_with_both_secrets_until_the_overlap_ends(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    h = await make_tenant()
    body = {"url": "https://example.com/hook", "event_types": ["order.created"]}
    ep = (await client.post("/v1/endpoints", json=body, headers=h)).json()
    old = ep["secret"]

    req = await _deliver(client, h, "before")
    assert _verifies(old, req)  # what a receiver does with the raw body it got

    rotated = await client.post(f"/v1/endpoints/{ep['id']}/rotate-secret", headers=h)
    new = rotated.json()["secret"]
    assert new != old and rotated.json()["previous_secret_expires_at"]

    req = await _deliver(client, h, "during")
    assert req.headers["X-Webhook-Signature"].count("v1=") == 2
    assert _verifies(old, req) and _verifies(new, req)

    async with Session() as s:  # fast-forward past the overlap window
        await s.execute(
            text("UPDATE endpoints SET previous_secret_expires_at = now() - interval '1 second'")
        )
        await s.commit()
    req = await _deliver(client, h, "after")
    assert _verifies(new, req) and not _verifies(old, req)


async def test_rotate_secret_is_tenant_scoped_and_validates_overlap(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    a, b = await make_tenant(), await make_tenant()
    body = {"url": "https://example.com/hook", "event_types": ["order.created"]}
    ep = (await client.post("/v1/endpoints", json=body, headers=a)).json()["id"]
    path = f"/v1/endpoints/{ep}/rotate-secret"
    assert (await client.post(path, headers=b)).status_code == 404
    assert (await client.post(path, json={"overlap_hours": 1000}, headers=a)).status_code == 422
    assert (await client.post(path, json={"overlap_hours": 0}, headers=a)).status_code == 200
