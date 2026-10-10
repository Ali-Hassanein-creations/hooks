"""Delivery worker: python -m webhooks.worker

Loop: claim due deliveries, POST each one, record the attempt, schedule the retry.
Postgres is the queue, so claiming and recording are plain transactions and a crashed
worker loses nothing: its claimed rows become due again when their lease runs out.
"""

import asyncio
import json
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import func, text, update

from webhooks import ssrf
from webhooks.db import Session, decrypt
from webhooks.models import Delivery, DeliveryAttempt
from webhooks.verify import sign

log = logging.getLogger("webhooks.worker")

# Ceiling of the wait before retry n (1-based). 1 initial attempt + 6 retries.
RETRY_CEILINGS = (10, 60, 300, 1800, 7200, 21600)
MAX_ATTEMPTS = len(RETRY_CEILINGS) + 1
LEASE_SECONDS = 60  # must comfortably exceed TOTAL_TIMEOUT
TOTAL_TIMEOUT = 15
TIMEOUTS = httpx.Timeout(10, connect=3)
MAX_STORED_BODY = 4096
CONCURRENCY = int(os.environ.get("WORKER_CONCURRENCY", "10"))

# Claim = push next_attempt_at forward by the lease and count the attempt, in one statement.
# SKIP LOCKED makes concurrent workers pass over each other's rows instead of queueing
# behind them, and the partial index on pending rows keeps the scan cheap.
CLAIM = text("""
WITH due AS (
    SELECT d.id FROM deliveries d
    JOIN endpoints e ON e.id = d.endpoint_id
    WHERE d.status = 'pending' AND d.next_attempt_at <= now()
      AND e.status = 'enabled' AND e.deleted_at IS NULL
    ORDER BY d.next_attempt_at
    LIMIT :batch
    FOR UPDATE OF d SKIP LOCKED
)
UPDATE deliveries d
SET next_attempt_at = now() + make_interval(secs => :lease),
    attempt_count = d.attempt_count + 1
FROM due, events ev, endpoints e
WHERE d.id = due.id AND ev.id = d.event_id AND e.id = d.endpoint_id
RETURNING d.id, d.tenant_id, d.attempt_count, ev.event_type, ev.payload, e.url,
          e.secret_encrypted, e.previous_secret_encrypted,
          e.previous_secret_expires_at > now() AS previous_active
""")


@dataclass(frozen=True)
class Claimed:
    id: uuid.UUID
    tenant_id: uuid.UUID
    attempt: int  # 1-based; also the fencing token for record()
    event_type: str
    payload: Any
    url: str
    secrets: tuple[str, ...]  # current secret, plus the previous one during a rotation


@dataclass(frozen=True)
class Result:
    headers: dict[str, str]
    status: int | None
    body: str | None
    duration_ms: int
    error: str | None
    blocked: bool = False  # SSRF guard refused the destination: never worth retrying

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


def retry_delay(attempt: int) -> float:
    """Full jitter: uniform(0, ceiling), so retries to a recovering endpoint spread out."""
    return random.uniform(0, RETRY_CEILINGS[attempt - 1])  # noqa: S311 - not crypto


async def claim(batch: int) -> list[Claimed]:
    async with Session() as session:
        rows = (await session.execute(CLAIM, {"batch": batch, "lease": LEASE_SECONDS})).all()
        await session.commit()
    return [
        Claimed(
            r.id,
            r.tenant_id,
            r.attempt_count,
            r.event_type,
            json.loads(r.payload) if isinstance(r.payload, str) else r.payload,
            r.url,
            (decrypt(r.secret_encrypted),)
            + ((decrypt(r.previous_secret_encrypted),) if r.previous_active else ()),
        )
        for r in rows
    ]


async def send(client: httpx.AsyncClient, c: Claimed) -> Result:
    # Serialised once: these exact bytes are both signed and sent.
    body = json.dumps(c.payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "webhooks/0.1",
        # Identical on every retry: this is what receivers deduplicate on.
        "X-Webhook-Id": str(c.id),
        "X-Webhook-Event": c.event_type,
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature": ",".join(f"v1={sign(s, timestamp, body)}" for s in c.secrets),
    }
    status, resp_body, error = None, None, None
    start = time.perf_counter()
    try:
        # Re-checked on every attempt, and the connection goes to the IP we just validated,
        # so DNS that changed since registration (rebinding) can't redirect us inward.
        target = await ssrf.resolve(c.url)
    except ssrf.BlockedURL as exc:
        return Result(headers, None, None, 0, f"blocked: {exc}", blocked=True)
    except ssrf.UnresolvableHost as exc:
        return Result(headers, None, None, 0, str(exc))
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT):
            async with client.stream(
                "POST",
                target.url,
                content=body,
                headers={**headers, "Host": target.host},
                extensions={"sni_hostname": target.sni} if target.sni else {},
            ) as resp:
                status = resp.status_code
                # Read at most MAX_STORED_BODY bytes: a 50 MB error page is never downloaded.
                chunks = bytearray()
                async for chunk in resp.aiter_bytes():
                    chunks += chunk
                    if len(chunks) >= MAX_STORED_BODY:
                        break
                resp_body = chunks[:MAX_STORED_BODY].decode(errors="replace")
    except (TimeoutError, httpx.TimeoutException):
        error = "timeout"
    except httpx.HTTPError as exc:
        error = f"{type(exc).__name__}: {exc}"
    duration_ms = int((time.perf_counter() - start) * 1000)
    return Result(headers, status, resp_body, duration_ms, error)


async def record(c: Claimed, r: Result) -> None:
    async with Session() as session:
        session.add(
            DeliveryAttempt(
                tenant_id=c.tenant_id,
                delivery_id=c.id,
                attempt_number=c.attempt,
                request_headers=r.headers,
                response_status=r.status,
                response_body=r.body,
                duration_ms=r.duration_ms,
                error=r.error,
            )
        )
        if r.ok:
            values: dict[str, Any] = {"status": "succeeded"}
        elif r.blocked or c.attempt >= MAX_ATTEMPTS:
            values = {"status": "dead_lettered"}
        else:
            values = {"next_attempt_at": func.now() + timedelta(seconds=retry_delay(c.attempt))}
        # Fencing: if our lease lapsed and another worker re-claimed the row, attempt_count
        # has moved on and this update is a no-op. The attempt row above is still recorded,
        # because the request really was sent.
        await session.execute(
            update(Delivery)
            .where(Delivery.id == c.id, Delivery.attempt_count == c.attempt)
            .values(**values)
        )
        await session.commit()
    log.info("delivery=%s attempt=%d status=%s error=%s", c.id, c.attempt, r.status, r.error)


async def process(client: httpx.AsyncClient, c: Claimed) -> None:
    await record(c, await send(client, c))


async def run_once(client: httpx.AsyncClient, batch: int = CONCURRENCY) -> int:
    claimed = await claim(batch)
    await asyncio.gather(*(process(client, c) for c in claimed))
    return len(claimed)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    async with httpx.AsyncClient(timeout=TIMEOUTS, follow_redirects=False) as client:
        while True:
            try:
                if await run_once(client) == 0:
                    await asyncio.sleep(1)
            except Exception:
                # e.g. the database restarting. Claimed rows are safe: their lease lapses.
                log.exception("worker loop failed; retrying in 5s")
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
