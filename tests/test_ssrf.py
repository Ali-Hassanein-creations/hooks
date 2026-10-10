"""The SSRF guard: tenants choose the URLs we POST to, so every internal address must be
refused, both when the endpoint is registered and again before every delivery attempt."""

from collections.abc import Awaitable, Callable

import httpx
import pytest
from sqlalchemy import select

from webhooks import ssrf, worker
from webhooks.db import Session
from webhooks.models import Delivery, DeliveryAttempt

Headers = dict[str, str]
MakeTenant = Callable[[], Awaitable[Headers]]
EVENT = {"event_type": "order.created", "payload": {"order_id": 1}}

BLOCKED = [
    "http://127.0.0.1/",  # loopback
    "http://localhost/",  # hostname resolving to loopback
    "http://127.1/",  # short form of 127.0.0.1
    "http://2130706433/",  # 127.0.0.1 as a decimal integer
    "http://0x7f000001/",  # 127.0.0.1 in hex
    "http://0.0.0.0/",  # unspecified: reaches local services on many systems
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
    "http://10.0.0.1/",  # RFC 1918 private ranges
    "http://172.16.0.1/",
    "http://192.168.1.1/",
    "http://100.64.0.1/",  # carrier-grade NAT
    "http://[::1]/",  # IPv6 loopback
    "http://[::ffff:127.0.0.1]/",  # IPv4-mapped IPv6 loopback
    "http://[::ffff:169.254.169.254]/",  # IPv4-mapped metadata address
    "http://[fe80::1]/",  # IPv6 link-local
    "http://[fd00::1]/",  # IPv6 unique local
    "http://internal.example/",  # hostname resolving to a private IP
    "http://mixed.example/",  # one public and one private record
    "http://user:pass@example.com/",  # credentials in the URL
    "http://example.com:8080/",  # non-standard port
    "file:///etc/passwd",  # non-http scheme
    "gopher://example.com/",
]


@pytest.fixture(autouse=True)
def _records(dns: dict[str, list[str]]) -> None:
    dns["internal.example"] = ["10.1.2.3"]
    dns["mixed.example"] = ["93.184.216.34", "10.1.2.3"]


@pytest.mark.parametrize("url", BLOCKED)
async def test_blocked_at_registration(
    client: httpx.AsyncClient, make_tenant: MakeTenant, url: str
) -> None:
    body = {"url": url, "event_types": ["order.created"]}
    r = await client.post("/v1/endpoints", json=body, headers=await make_tenant())
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("url", ["https://example.com/hook", "http://93.184.216.34/x"])
async def test_public_destinations_are_allowed(
    client: httpx.AsyncClient, make_tenant: MakeTenant, url: str
) -> None:
    body = {"url": url, "event_types": ["order.created"]}
    assert (await client.post("/v1/endpoints", json=body, headers=await make_tenant())).is_success


@pytest.mark.parametrize("host", ["127.1", "2130706433", "0x7f000001", "017700000001"])
async def test_real_resolver_never_returns_a_target_for_disguised_loopback(
    host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Undo the fake DNS for this one: the OS resolver either parses these as 127.0.0.1
    # (Linux), which must be blocked, or rejects them (Windows). Either way, no target.
    monkeypatch.undo()
    with pytest.raises((ssrf.BlockedURL, ssrf.UnresolvableHost)):
        await ssrf.resolve(f"http://{host}/")


async def _register_and_send(client: httpx.AsyncClient, make_tenant: MakeTenant, url: str) -> None:
    h = await make_tenant()
    r = await client.post(
        "/v1/endpoints", json={"url": url, "event_types": ["order.created"]}, headers=h
    )
    assert r.status_code == 201, r.text
    await client.post("/v1/events", json=EVENT, headers={**h, "Idempotency-Key": "k"})


async def test_dns_rebinding_after_registration_is_blocked_before_sending(
    client: httpx.AsyncClient, make_tenant: MakeTenant, dns: dict[str, list[str]]
) -> None:
    dns["rebind.example"] = ["93.184.216.34"]  # public while the endpoint is registered...
    await _register_and_send(client, make_tenant, "http://rebind.example/hook")
    dns["rebind.example"] = ["169.254.169.254"]  # ...then pointed at cloud metadata

    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as http:
        await worker.run_once(http)

    assert calls == []  # nothing was sent
    async with Session() as s:
        d = await s.scalar(select(Delivery))
        a = await s.scalar(select(DeliveryAttempt))
    assert d is not None and d.status == "dead_lettered"  # retrying can't make it safe
    assert a is not None and a.error is not None and a.error.startswith("blocked")


async def test_dns_failure_at_delivery_is_retried_not_dead_lettered(
    client: httpx.AsyncClient, make_tenant: MakeTenant, dns: dict[str, list[str]]
) -> None:
    dns["flaky.example"] = ["93.184.216.34"]
    await _register_and_send(client, make_tenant, "https://flaky.example/hook")
    del dns["flaky.example"]

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200))
    ) as http:
        await worker.run_once(http)

    async with Session() as s:
        d = await s.scalar(select(Delivery))
    assert d is not None and (d.status, d.attempt_count) == ("pending", 1)


async def test_connects_to_the_validated_ip_with_original_host_and_sni(
    client: httpx.AsyncClient, make_tenant: MakeTenant
) -> None:
    await _register_and_send(client, make_tenant, "https://example.com/hook?x=1")
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as http:
        await worker.run_once(http)

    [req] = calls
    assert str(req.url) == "https://93.184.216.34/hook?x=1"  # pinned: no second DNS lookup
    assert req.headers["Host"] == "example.com"
    assert req.extensions["sni_hostname"] == "example.com"  # TLS cert checked for the name
