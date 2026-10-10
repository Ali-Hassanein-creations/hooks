import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, text

from webhooks import worker
from webhooks.db import Session
from webhooks.models import Delivery, DeliveryAttempt

Headers = dict[str, str]
MakeTenant = Callable[[], Awaitable[Headers]]
Handler = Callable[[httpx.Request], httpx.Response]
EVENT = {"event_type": "order.created", "payload": {"order_id": 42}}


async def _setup(client: httpx.AsyncClient, make_tenant: MakeTenant) -> tuple[Headers, str]:
    """One tenant, one matching endpoint, one event. Returns (auth headers, endpoint id)."""
    h = await make_tenant()
    body = {"url": "https://example.com/hook", "event_types": ["order.created"]}
    ep: str = (await client.post("/v1/endpoints", json=body, headers=h)).json()["id"]
    r = await client.post("/v1/events", json=EVENT, headers={**h, "Idempotency-Key": "e1"})
    assert r.status_code == 202
    return h, ep


def _client(handler: Handler, calls: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    def wrapped(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(wrapped), follow_redirects=False)


async def _delivery() -> Delivery:
    async with Session() as s:
        d = await s.scalar(select(Delivery))
        assert d is not None
        return d


async def _attempts() -> list[DeliveryAttempt]:
    async with Session() as s:
        return list(await s.scalars(select(DeliveryAttempt).order_by("attempt_number")))


async def _make_due() -> None:
    """Time travel: make every pending delivery due now, skipping the backoff wait."""
    async with Session() as s:
        await s.execute(text("UPDATE deliveries SET next_attempt_at = now() - interval '1 second'"))
        await s.commit()


def test_retry_delay_stays_within_each_ceiling() -> None:
    for attempt, ceiling in enumerate(worker.RETRY_CEILINGS, start=1):
        delays = [worker.retry_delay(attempt) for _ in range(500)]
        assert all(0 <= d <= ceiling for d in delays)
        assert max(delays) > ceiling / 2  # actually jittered across the range, not constant


async def test_2xx_succeeds_and_records_the_attempt(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    await _setup(client, make_tenant)
    calls: list[httpx.Request] = []
    async with _client(lambda r: httpx.Response(200, text="ok"), calls) as http:
        assert await worker.run_once(http) == 1

    d = await _delivery()
    assert (d.status, d.attempt_count) == ("succeeded", 1)
    [a] = await _attempts()
    assert (a.attempt_number, a.response_status, a.response_body, a.error) == (1, 200, "ok", None)
    assert a.duration_ms >= 0
    assert a.request_headers["X-Webhook-Id"] == str(d.id)
    assert calls[0].read() == b'{"order_id":42}'
    assert calls[0].headers["X-Webhook-Event"] == "order.created"


async def test_500_schedules_a_jittered_retry(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    await _setup(client, make_tenant)
    before = datetime.now(UTC)
    async with _client(lambda r: httpx.Response(500)) as http:
        await worker.run_once(http)

    d = await _delivery()
    assert (d.status, d.attempt_count) == ("pending", 1)
    # First retry ceiling is 10s; allow a little clock slack between Python and Postgres.
    assert before - timedelta(seconds=1) <= d.next_attempt_at <= before + timedelta(seconds=11)
    [a] = await _attempts()
    assert a.response_status == 500


async def test_timeout_is_recorded_and_retried(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    await _setup(client, make_tenant)

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    async with _client(slow) as http:
        await worker.run_once(http)

    assert (await _delivery()).status == "pending"
    [a] = await _attempts()
    assert (a.response_status, a.error) == (None, "timeout")


async def test_redirect_is_a_failure_and_is_not_followed(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    await _setup(client, make_tenant)
    calls: list[httpx.Request] = []
    to_metadata = httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/"})
    async with _client(lambda r: to_metadata, calls) as http:
        await worker.run_once(http)

    assert len(calls) == 1  # the redirect target was never requested
    assert (await _delivery()).status == "pending"


async def test_dead_letters_after_the_last_attempt_with_a_stable_webhook_id(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    await _setup(client, make_tenant)
    calls: list[httpx.Request] = []
    async with _client(lambda r: httpx.Response(503), calls) as http:
        for _ in range(worker.MAX_ATTEMPTS):
            assert await worker.run_once(http) == 1
            await _make_due()
        assert await worker.run_once(http) == 0  # nothing left to claim

    d = await _delivery()
    assert (d.status, d.attempt_count) == ("dead_lettered", worker.MAX_ATTEMPTS)
    assert [a.attempt_number for a in await _attempts()] == list(range(1, 8))
    assert {r.headers["X-Webhook-Id"] for r in calls} == {str(d.id)}


async def test_huge_response_body_is_truncated(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    await _setup(client, make_tenant)
    async with _client(lambda r: httpx.Response(500, content=b"x" * 1_000_000)) as http:
        await worker.run_once(http)

    [a] = await _attempts()
    assert a.response_body is not None and len(a.response_body) == worker.MAX_STORED_BODY


async def test_concurrent_workers_attempt_a_delivery_exactly_once(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    await _setup(client, make_tenant)
    calls: list[httpx.Request] = []

    async with _client(lambda r: httpx.Response(200), calls) as http:
        claimed = await asyncio.gather(*(worker.run_once(http) for _ in range(10)))

    assert sum(claimed) == 1
    assert len(calls) == 1
    assert len(await _attempts()) == 1


async def test_lapsed_lease_is_reclaimed_and_the_stale_worker_cannot_overwrite(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    await _setup(client, make_tenant)
    [stale] = await worker.claim(10)
    assert await worker.claim(10) == []  # leased: nobody else can take it

    await _make_due()  # the first worker "hung" past its lease
    [fresh] = await worker.claim(10)
    assert (stale.attempt, fresh.attempt) == (1, 2)

    # The hung worker wakes up and reports success; the fencing check must ignore it.
    await worker.record(stale, worker.Result({}, 200, "", 5, None))
    d = await _delivery()
    assert (d.status, d.attempt_count) == ("pending", 2)
    assert [a.attempt_number for a in await _attempts()] == [1]  # still audited


async def test_disabled_endpoint_waits_and_deleted_endpoint_dead_letters(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    h, ep = await _setup(client, make_tenant)
    await client.patch(f"/v1/endpoints/{ep}", json={"status": "disabled"}, headers=h)
    assert await worker.claim(10) == []

    await client.patch(f"/v1/endpoints/{ep}", json={"status": "enabled"}, headers=h)
    assert len(await worker.claim(10)) == 1

    await client.delete(f"/v1/endpoints/{ep}", headers=h)
    assert (await _delivery()).status == "dead_lettered"
