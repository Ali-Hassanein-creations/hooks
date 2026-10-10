# Webhook Delivery Platform

A multi-tenant webhook delivery service with at-least-once guarantees, signed payloads, and automatic retries.

> **Status:** weeks 1–3 of the build. Ingest, idempotency, the transactional fan-out and the delivery worker (retries, backoff, dead-letter) are done. Signing and SSRF defence come next.

## The guarantee

**At-least-once delivery, best-effort ordering per endpoint, and an auditable record of every attempt.**

Exactly-once delivery over HTTP is not achievable. A receiver can process a request and then time out before we see the 2xx, so we retry. Every delivery carries a stable `X-Webhook-Id` header that is identical across retries, and **receivers must deduplicate on it**.

## Quickstart

```bash
docker compose up -d --build     # postgres, migrate, api, worker, and a demo receiver
docker compose exec api python -m webhooks.cli create-tenant acme   # prints an API key, once
export KEY=whk_...   # from the line above

# 1. Register an endpoint (the signing secret is returned once).
#    http://receiver/hook is the demo receiver inside the compose network.
curl -X POST localhost:8000/v1/endpoints -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"url":"http://receiver/hook","event_types":["order.created"]}'

# 2. Send an event: 202 the first time, 200 + the same event on any retry with the same key
curl -X POST localhost:8000/v1/events -H "Authorization: Bearer $KEY" \
  -H 'Idempotency-Key: order-42-created' -H 'Content-Type: application/json' \
  -d '{"event_type":"order.created","payload":{"order_id":42}}'

# 3. See the delivery: the worker picks it up within a second, so it shows "succeeded"
curl localhost:8000/v1/events/<event id> -H "Authorization: Bearer $KEY"
docker compose logs receiver worker

# 4. Watch retries: make the receiver fail, send another event, and see it back off
RECEIVER_STATUS=500 docker compose up -d receiver
docker compose logs -f worker
```

OpenAPI docs are at `localhost:8000/docs`.

## Design decisions

### Postgres is the queue
**Problem:** workers need a queue of deliveries to attempt.
**Options:** Redis with Arq or Celery, or Postgres with `SELECT … FOR UPDATE SKIP LOCKED`.
**Chosen:** Postgres. A worker claims a row in the same database, and the same transaction, that holds the delivery record. That removes a whole class of lost-job and ghost-job bugs, and it means one fewer moving part to run.
**Trade-off:** throughput is capped by Postgres write capacity, and polling adds a little latency compared with push. A partial index `ix_deliveries_claimable ON deliveries (next_attempt_at) WHERE status = 'pending'` keeps the claim query cheap no matter how many finished deliveries accumulate. The index only covers rows the worker could actually claim.

### The transactional outbox
**Problem:** the naive handler writes the event to Postgres and then pushes a job to a queue.
- If the process dies between those two steps, the event exists but is never delivered.
- If the order is reversed and the database write fails, we deliver an event that doesn't exist.

**Chosen:** `POST /v1/events` writes the event **and one `pending` delivery row per matching endpoint in a single transaction**. It uses one `INSERT … SELECT`. There is no second system to keep in sync, so there is no window where the two can disagree.
**Trade-off:** ingest latency grows with the number of matching endpoints per tenant. A tenant with thousands of endpoints subscribed to one event type would need the fan-out moved to an async step, still in the outbox.

### Idempotent ingest
`UNIQUE (tenant_id, idempotency_key)` means a duplicate can never be stored. The constraint alone would turn two concurrent retries into a 500, though. So the handler takes `pg_advisory_xact_lock` on the tenant and key first. A retry that arrives while the original request is still in flight waits for it, then returns the original event with **200**; a new event gets **202**. `tests/test_events.py::test_concurrent_duplicates_create_one_event` fires 20 concurrent identical requests and asserts exactly one event and one set of deliveries. That test fails if the lock is removed.

### Claiming work
**Problem:** several workers poll the same table; each due delivery must be attempted by exactly one of them, and a worker that crashes mid-request must not lose the delivery.
**Chosen:** one statement (`worker.CLAIM`) selects due rows with `FOR UPDATE … SKIP LOCKED` and pushes their `next_attempt_at` 60 seconds into the future. That timestamp is the lease, so there is no `in_flight` state and no reaper: if the worker dies, the row simply becomes due again. The HTTP request happens **outside** any transaction, and the result is recorded in a second short one.
**The subtle part:** a worker that stalls past its lease could report back after another worker re-claimed the row. The record step updates `WHERE attempt_count = <the count it claimed with>`, a fencing token, so a stale result is audited but can't overwrite newer state.
**Proof:** `tests/test_worker.py::test_concurrent_workers_attempt_a_delivery_exactly_once` runs 10 workers against one delivery and asserts exactly one request. It fails without `SKIP LOCKED`, and the lease test fails without the fencing check.

### Retry, backoff and jitter
**Problem:** a fixed retry interval makes every failed delivery to a recovering endpoint arrive at once and knock it over again.
**Chosen:** 7 attempts (1 + 6 retries) with ceilings of 10s, 1m, 5m, 30m, 2h and 6h, and **full jitter**: each wait is `uniform(0, ceiling)`. A delivery dead-letters after about 4.4 hours on average and at most about 8.8.
- Success is 2xx only. Redirects are never followed: a 3xx is a failure, so a receiver can't bounce us to an internal address.
- Timeouts: 3s to connect, 10s to read, 15s total. At most 4 KB of a response is read and stored, so a 50 MB error page is never downloaded.
- Deleting an endpoint dead-letters its waiting deliveries; disabling one pauses them until it is re-enabled.

## Security (so far)
- **API keys** use the format `whk_<prefix>_<random>`. Only a SHA-256 hash is stored, looked up by prefix and compared with `hmac.compare_digest`. The keys are 256-bit random values, so a slow KDF would add latency without adding security.
- **Tenant isolation:** every table carries `tenant_id` and every query filters on it. A resource that belongs to another tenant returns 404, not 403, so ids don't leak. See `tests/test_endpoints.py::test_tenant_isolation`.
- **Signing secrets** are encrypted at rest with Fernet and returned only once, on creation.
- **Endpoint URLs** are limited to http(s) on ports 80 and 443. The full SSRF guard (resolve, block private and link-local ranges, pin the IP against DNS rebinding) lands in week 4.

## Development

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # bin/ on macOS/Linux
ruff check . && mypy --strict src && pytest      # tests start a real Postgres 16 via testcontainers
```
