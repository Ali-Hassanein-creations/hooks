# Webhook Delivery Platform

[![ci](https://github.com/Ali-Hassanein-creations/hooks/actions/workflows/ci.yml/badge.svg)](https://github.com/Ali-Hassanein-creations/hooks/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.12%2B-blue)
![postgres](https://img.shields.io/badge/postgres-16-blue)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A multi-tenant webhook delivery service with at-least-once guarantees, signed payloads, and automatic retries.

Your application sends an event once. The platform delivers it to every HTTP endpoint your customers have subscribed, even when those endpoints are slow, broken or down for hours. It signs every request so receivers can trust it, refuses to be turned against internal networks, and keeps an audit trail of every attempt.

> **Status:** weeks 1–4 of a 6-week build are done:
> - ingest
> - the delivery worker
> - SSRF defence
> - signed payloads
> - secret rotation
> - rate limiting
>
> **Next:** operations (circuit breaker, deliveries API and replay, Prometheus metrics, public deploy). See the [roadmap](#roadmap).

## Contents

- [The guarantee](#the-guarantee)
- [How it works](#how-it-works)
- [Quickstart](#quickstart)
- [API reference](#api-reference)
- [Receiving webhooks](#receiving-webhooks)
- [Delivery semantics](#delivery-semantics)
- [Design decisions](#design-decisions)
- [Security](#security)
- [Data model](#data-model)
- [Configuration](#configuration)
- [Project layout](#project-layout)
- [Testing](#testing)
- [Development](#development)
- [Limits and what I'd change at scale](#limits-and-what-id-change-at-scale)
- [Roadmap](#roadmap)

## The guarantee

**At-least-once delivery, best-effort ordering per endpoint, and an auditable record of every attempt.**

Exactly-once delivery over HTTP is not achievable. A receiver can process a request and then time out before we see its 2xx; we can't tell that apart from a request that never arrived, so we retry. The responsibility that creates is discharged with one header: every delivery carries an `X-Webhook-Id` that is **identical on every retry**, and **receivers must deduplicate on it**.

## How it works

```mermaid
flowchart LR
    P[Producer app] -->|"POST /v1/events<br/>Idempotency-Key"| API[FastAPI]
    subgraph TX["One Postgres transaction (the outbox)"]
        E[(events)]
        D[(deliveries<br/>status = pending)]
    end
    API --> E
    API --> D
    W[Worker pool] -->|"claim:<br/>FOR UPDATE SKIP LOCKED"| D
    W -->|"resolve + SSRF check,<br/>pin the IP"| DNS[(DNS)]
    W -->|"signed POST<br/>X-Webhook-Id / Signature"| R[Customer endpoint]
    W -->|record| A[(delivery_attempts)]
```

1. **A tenant registers an endpoint:** a URL, the event types it wants, and a generated signing secret, which is shown once.
2. **The tenant's app posts an event** with an `Idempotency-Key`. In **one transaction** the API stores the event and creates one `pending` delivery per matching endpoint. That is the transactional outbox: there is no separate queue that could disagree with the database.
3. **Workers claim due deliveries** from Postgres with `SELECT … FOR UPDATE SKIP LOCKED`, so concurrent workers never pick the same row.
4. **Before sending,** the worker resolves the hostname, checks every address is public, and connects to that exact IP. It then POSTs the payload with an HMAC signature.
5. **A 2xx marks the delivery `succeeded`.** Anything else is recorded and retried on a jittered exponential schedule. After the 7th attempt the delivery becomes `dead_lettered`.
6. **Every attempt is stored** in `delivery_attempts`: request headers, response status, the first 4 KB of the response body, timing, and any error.

```mermaid
stateDiagram-v2
    [*] --> pending: event matches an enabled endpoint
    pending --> succeeded: 2xx response
    pending --> pending: failure, retry scheduled (attempts 1-6)
    pending --> dead_lettered: 7th failure
    pending --> dead_lettered: destination blocked by SSRF guard
    pending --> dead_lettered: endpoint deleted
    succeeded --> [*]
    dead_lettered --> [*]
```

## Quickstart

Requirements: Docker. One command starts Postgres, runs the migrations, and starts the API, a worker and a demo receiver.

```bash
docker compose up -d --build
docker compose exec api python -m webhooks.cli create-tenant acme   # prints an API key, once
export KEY=whk_...                                                    # paste it here
```

**1. Register an endpoint.** `http://receiver/hook` is the demo receiver inside the compose network.
```bash
curl -X POST localhost:8000/v1/endpoints -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"url":"http://receiver/hook","event_types":["order.created"]}'
```
```json
{"id":"ca45…","url":"http://receiver/hook","event_types":["order.created"],
 "description":null,"status":"enabled","created_at":"2026-10-10T18:10:20Z",
 "secret":"whsec_FSwR…"}
```
The `secret` is shown only in this response. Keep it: receivers use it to verify signatures.

**2. Send an event.** It returns 202 the first time, and 200 with the same event on any retry using the same key.
```bash
curl -X POST localhost:8000/v1/events -H "Authorization: Bearer $KEY" \
  -H 'Idempotency-Key: order-42-created' -H 'Content-Type: application/json' \
  -d '{"event_type":"order.created","payload":{"order_id":42}}'
```

**3. Watch it arrive.** The worker picks it up within about a second.
```bash
curl localhost:8000/v1/events/<event id> -H "Authorization: Bearer $KEY"   # deliveries: succeeded
docker compose logs receiver worker
```

**4. Watch retries.** Make the receiver fail, send another event, and see it back off.
```bash
RECEIVER_STATUS=500 docker compose up -d receiver
docker compose logs -f worker      # attempt 1 … attempt 2 ~seconds later … attempt 3 ~a minute later
docker compose up -d receiver      # back to 200; the next retry succeeds
```

**5. Check signatures the way a real receiver would.** Give the receiver the endpoint's secret.
```bash
RECEIVER_SECRET=whsec_... docker compose up -d receiver   # its log now shows signature=valid
```

Interactive API docs (Swagger UI) are at <http://localhost:8000/docs>. Stop everything with `docker compose down`, or `docker compose down -v` to also wipe the database.

## API reference

All `/v1` routes need `Authorization: Bearer whk_…`. Every resource belongs to one tenant; another tenant's resources return **404**, never 403, so ids don't leak.

| Method | Path | What it does | Success |
|---|---|---|---|
| `POST` | `/v1/endpoints` | Register a URL and its event types. Returns the signing secret **once**. | 201 |
| `GET` | `/v1/endpoints?limit=&cursor=` | List endpoints, paginated with an opaque cursor (`limit` ≤ 100). | 200 |
| `GET` | `/v1/endpoints/{id}` | One endpoint. The secret is never shown again. | 200 |
| `PATCH` | `/v1/endpoints/{id}` | Change `url`, `event_types`, `description`, or `status` (`enabled` / `disabled`). | 200 |
| `DELETE` | `/v1/endpoints/{id}` | Soft delete. Its pending deliveries are dead-lettered. | 204 |
| `POST` | `/v1/endpoints/{id}/rotate-secret` | New secret. The old one keeps signing for `overlap_hours` (0–168, default 24). | 200 |
| `POST` | `/v1/events` | Ingest an event. Needs an `Idempotency-Key` header (1–255 chars). | 202 new, 200 duplicate |
| `GET` | `/v1/events/{id}` | The event plus the status of every delivery made from it. | 200 |
| `GET` | `/healthz` | Liveness: the process is up. Checks nothing else. | 200 |
| `GET` | `/readyz` | Readiness: the database answers. Postgres is also the queue, so this covers both. | 200 / 503 |

**Errors:**

| Status | When |
|---|---|
| 400 | Malformed pagination cursor. |
| 401 | Missing, malformed, wrong or revoked API key. |
| 404 | Not found, or it belongs to another tenant. |
| 422 | Validation failed, including: a URL refused by the SSRF guard, a missing `Idempotency-Key`, or **an `Idempotency-Key` reused with a different body**. |
| 429 | Over the tenant's ingest rate limit. `Retry-After` says how many seconds until the next window. |

**Event body:**
```json
{"event_type": "order.created", "payload": {"order_id": 42}}
```
`payload` is any JSON object. It is stored as JSONB and delivered as compact JSON.

## Receiving webhooks

Each delivery is a `POST` with a JSON body and these headers:

| Header | Example | Meaning |
|---|---|---|
| `X-Webhook-Id` | `6d73b4c8-…` | The delivery id. **The same on every retry: deduplicate on it.** |
| `X-Webhook-Event` | `order.created` | The event type. |
| `X-Webhook-Timestamp` | `1760112000` | Unix seconds when this attempt was signed. |
| `X-Webhook-Signature` | `v1=5f2c…` | Hex `HMAC-SHA256(secret, "{timestamp}.{raw body}")`. During a secret rotation it holds two values: `v1=…,v1=…`. |
| `Content-Type` | `application/json` | |
| `User-Agent` | `webhooks/0.1` | |

**What a receiver should do:**
1. **Verify the signature against the raw request bytes.** Don't parse and re-serialise the JSON first; key order and whitespace would change and the signature would no longer match.
2. **Reject timestamps more than 5 minutes off.** This stops a captured request being replayed later.
3. **Deduplicate on `X-Webhook-Id`.** You will occasionally receive the same delivery twice.
4. **Return a 2xx quickly**, and do slow work afterwards. Anything other than 2xx, including a redirect, or no answer within 15 seconds, counts as a failure and is retried.

**Python:** [`src/webhooks/verify.py`](src/webhooks/verify.py) uses only the standard library, so you can copy the file.
```python
from fastapi import FastAPI, HTTPException, Request
from verify import verify  # the copied file

app = FastAPI()


@app.post("/webhooks")
async def receive(request: Request) -> dict[str, bool]:
    body = await request.body()  # raw bytes, exactly as sent
    if not verify(
        SECRET,
        body,
        request.headers.get("X-Webhook-Timestamp", ""),
        request.headers.get("X-Webhook-Signature", ""),
    ):
        raise HTTPException(400, "bad signature")
    if already_processed(request.headers["X-Webhook-Id"]):
        return {"ok": True}  # a retry of something we already handled
    ...
    return {"ok": True}
```

**Node.js:** tested against signatures produced by this service.
```js
const crypto = require("crypto");

function verifyWebhook(secret, rawBody, timestamp, signatureHeader, toleranceSeconds = 300) {
  const ts = Number(timestamp);
  if (!Number.isInteger(ts) || Math.abs(Date.now() / 1000 - ts) > toleranceSeconds) return false;
  const expected = crypto.createHmac("sha256", secret).update(`${timestamp}.`).update(rawBody).digest();
  return signatureHeader
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s.startsWith("v1="))
    .some((s) => {
      const given = Buffer.from(s.slice(3), "hex");
      return given.length === expected.length && crypto.timingSafeEqual(given, expected);
    });
}
```

## Delivery semantics

**Retry schedule:** 7 attempts in total, and each wait is random between 0 and its ceiling ("full jitter"):

| After attempt | Wait before the next (random, up to) | Latest the next attempt can be, counted from the first |
|---|---|---|
| 1 | 10 s | 10 s |
| 2 | 1 min | 1 min 10 s |
| 3 | 5 min | 6 min 10 s |
| 4 | 30 min | 36 min 10 s |
| 5 | 2 h | 2 h 36 min |
| 6 | 6 h | 8 h 36 min |
| 7 | none: dead-lettered | |

On average a delivery that never succeeds is dead-lettered after about 4.3 hours, and at most after about 8.6. That's long enough to ride out most outages without retrying forever.

**Other rules:**
- **Success is 2xx only.** 3xx is never followed and counts as a failure. 4xx, 5xx, timeouts and connection errors are all retried.
- **Timeouts:** 3 s to connect, 10 s to read, 15 s total per attempt.
- **Response size:** at most 4 KB of the response body is read and stored. The rest is never downloaded.
- **Disabling an endpoint** pauses its deliveries. They resume when it's re-enabled.
- **Deleting an endpoint** dead-letters its pending deliveries.
- **A destination the SSRF guard blocks** is dead-lettered immediately, because retrying can't make it safe. A DNS failure, by contrast, is retried.
- **Ordering:** deliveries are claimed oldest-due first, so ordering per endpoint is best-effort. Retries can reorder events; receivers that care should use the payload's own sequence or timestamp.

## Design decisions

### Postgres is the queue
**Problem:** workers need a queue of deliveries to attempt.
**Options:** Redis with Arq or Celery, or Postgres with `SELECT … FOR UPDATE SKIP LOCKED`.
**Chosen:** Postgres. A worker claims a row in the same database, and the same transaction, that holds the delivery record. That removes a whole class of lost-job and ghost-job bugs, and it means one fewer moving part to run.
**Trade-off:** throughput is capped by Postgres write capacity, and polling adds up to a second of latency compared with push. The partial index `ix_deliveries_claimable ON deliveries (next_attempt_at) WHERE status = 'pending'` keeps the claim query cheap however many finished deliveries pile up. It only covers rows a worker could actually claim.

### The transactional outbox
**Problem:** the naive handler writes the event to Postgres and then pushes a job to a queue.
- If the process dies between those two steps, the event exists but is never delivered.
- If the order is reversed and the database write fails, we deliver an event that doesn't exist.

**Chosen:** `POST /v1/events` writes the event **and one `pending` delivery row per matching endpoint in a single transaction**, using one `INSERT … SELECT`. There is no second system to keep in sync, so there is no window where the two can disagree.
**Trade-off:** ingest latency grows with the number of matching endpoints per tenant. A tenant with thousands of endpoints on one event type would need the fan-out moved to an async step, still inside the outbox.

### Idempotent ingest
`UNIQUE (tenant_id, idempotency_key)` means a duplicate can never be stored. The constraint alone would turn two concurrent retries into a 500, though, so the handler first takes `pg_advisory_xact_lock` on the tenant and key.
- A retry that arrives while the original is still in flight waits for it, then gets the original event with **200**.
- A new event gets **202**.
- Reusing a key with a *different* body gets **422**: a SHA-256 of the canonical body is stored with the event and compared.

`tests/test_events.py::test_concurrent_duplicates_create_one_event` fires 20 identical requests at once and asserts exactly one event. It fails if the lock is removed.

### Claiming work
**Problem:** several workers poll the same table. Each due delivery must be attempted by exactly one of them, and a worker that crashes mid-request must not lose the delivery.
**Chosen:** one statement (`worker.CLAIM`) selects due rows with `FOR UPDATE … SKIP LOCKED` and pushes their `next_attempt_at` 60 seconds into the future. That timestamp *is* the lease, so there is no `in_flight` state and no cleanup job: if the worker dies, the row simply becomes due again. The HTTP request happens **outside** any transaction, and the result is recorded in a second short one.
**The subtle part:** a worker that stalls past its lease could report back after another worker has re-claimed the row. The record step updates `WHERE attempt_count = <the count it claimed with>`. That is a fencing token: a stale result is still audited, but it can't overwrite newer state.
**Proof:** `tests/test_worker.py::test_concurrent_workers_attempt_a_delivery_exactly_once` runs 10 workers against one delivery and asserts exactly one request. It fails without `SKIP LOCKED`, and the lease test fails without the fencing check.

### Retry with full jitter
**Problem:** a fixed retry interval makes every failed delivery to a recovering endpoint arrive at the same moment and knock it over again.
**Chosen:** exponential ceilings with full jitter, `wait = uniform(0, ceiling)`, which spreads retries across the whole window ([schedule above](#delivery-semantics)).
**Trade-off:** a retry can occasionally fire almost immediately. Spreading the load matters more than a minimum gap.

## Security

### SSRF defence
Tenants choose the URLs we send requests to. Without a guard, a tenant could register `http://169.254.169.254/latest/meta-data/` and have us fetch cloud credentials for them, or probe our private network. [`ssrf.py`](src/webhooks/ssrf.py):
- **Allows only** http(s) on ports 80 and 443, and rejects `user:pass@` in URLs.
- **Resolves the hostname itself and requires every returned address to be public.** That excludes:
  - loopback in any notation, and `0.0.0.0`
  - link-local (`169.254.0.0/16`, `fe80::/10`)
  - the private ranges and CGNAT
  - IPv6 unique-local
  - IPv4-mapped IPv6 such as `::ffff:127.0.0.1`

  Checking only the first record would let a hostname with one public and one private record through half the time.
- **Defends against DNS rebinding.** A hostname can resolve to a public IP at registration and to `169.254.169.254` a minute later. The guard therefore runs again before *every* attempt, and the worker connects to the exact IP it just validated, sending the original `Host` header and TLS SNI. The certificate is still checked against the hostname, and there is no second DNS lookup to race.
- **Never follows redirects**, so a receiver can't bounce us to an internal address.

[`tests/test_ssrf.py`](tests/test_ssrf.py) has 22 blocked-target cases, plus tests for rebinding, IP pinning, and disguised loopback (`127.1`, `2130706433`, hex, octal) through the real OS resolver.

### Signed payloads and replay protection
- The body is serialised **once**, and those exact bytes are signed and sent.
- The timestamp is inside the signed content, so it can't be swapped for a fresh one.
- Receivers reject anything more than 5 minutes off, so a captured request can't be replayed later.
- [`verify.py`](src/webhooks/verify.py) compares with `hmac.compare_digest`, which takes constant time and leaks nothing through timing. It compares bytes, so a garbage header returns `False` instead of raising.

### Secret storage and rotation
- **Signing secrets are encrypted at rest** with Fernet and shown only once.
- **The encryption key itself can be rotated:** `SECRET_ENCRYPTION_KEY` accepts several comma-separated keys. The first encrypts; all of them decrypt.
- **`POST /v1/endpoints/{id}/rotate-secret` issues a new signing secret.** Until the overlap ends, deliveries carry **two** `v1=` signatures, one per secret, so receivers can switch over with no window of failed verification. See `tests/test_signing.py`.

### Rate limiting
- **Ingest is limited per tenant** to `RATE_LIMIT_PER_MINUTE` (default 600) events per minute. Beyond that it returns **429** with `Retry-After`.
- **The counter is one row per tenant,** updated by a single atomic upsert in Postgres. That holds across any number of API replicas with no Redis.
- **Trade-off:** a fixed window allows a burst of up to 2× across a minute boundary.

### API keys and tenant isolation
- **API keys** look like `whk_<prefix>_<random>`.
  - Only a SHA-256 hash is stored, looked up by prefix and compared in constant time.
  - The keys are 256-bit random values, so a slow password hash would add latency without adding security.
  - `last_used_at` is written at most once a minute per key.
- **Tenant isolation:**
  - Every table carries `tenant_id` and every query filters on it.
  - Cross-tenant access returns 404.
  - See `tests/test_endpoints.py::test_tenant_isolation`.

## Data model

| Table | Purpose | Notable constraints and indexes |
|---|---|---|
| `tenants` | Accounts | |
| `api_keys` | Bearer keys (hash only) | `UNIQUE (prefix)` |
| `endpoints` | Destinations | `secret_encrypted`, plus `previous_secret_*` while a rotation overlaps. Soft-deleted via `deleted_at`. Index `(tenant_id, created_at, id)` for cursor pagination. |
| `events` | What tenants sent | `UNIQUE (tenant_id, idempotency_key)`, `request_hash`, payload as JSONB |
| `deliveries` | One per event per endpoint | `UNIQUE (event_id, endpoint_id)`. Status is `pending`, `succeeded` or `dead_lettered`. `next_attempt_at` is also the lease. **Partial index** on `next_attempt_at WHERE status = 'pending'`. |
| `delivery_attempts` | Audit trail | `UNIQUE (delivery_id, attempt_number)`. Stores request headers, response status, body (≤ 4 KB), duration and error. |
| `rate_limits` | Ingest counters | One row per tenant: the current minute and its count |

Statuses are `TEXT` with a `CHECK` constraint rather than Postgres enums. Changing a `CHECK` is an ordinary migration, while `ALTER TYPE … ADD VALUE` can't run inside a transaction.

**Migrations** (`migrations/versions/`) are written by hand, and each one has a working downgrade:

| Revision | Change |
|---|---|
| 0001 | tenants, api_keys, endpoints |
| 0002 | events and deliveries (the outbox) |
| 0003 | `events.request_hash`, to reject a reused key with a different body |
| 0004 | `delivery_attempts`; drop `locked_until`; narrow the status check |
| 0005 | secret-rotation columns; `rate_limits` |

In compose, migrations run in a one-shot `migrate` service before the API and worker start. The API never migrates on boot, so scaling it out can't race two migrations.

## Configuration

| Variable | Required | Default | Used by | Meaning |
|---|---|---|---|---|
| `DATABASE_URL` | yes | | api, worker, migrate | `postgresql+asyncpg://user:pass@host:5432/db` |
| `SECRET_ENCRYPTION_KEY` | yes | | api, worker | Fernet key(s), comma-separated. The first encrypts; all decrypt. |
| `RATE_LIMIT_PER_MINUTE` | no | `600` | api | Events per tenant per minute |
| `WORKER_CONCURRENCY` | no | `10` | worker | Deliveries claimed and sent in parallel per loop |
| `SSRF_ALLOW_HOSTS` | no | empty | api, worker | **Dev only.** Hostnames exempt from the SSRF guard. Compose sets it to `receiver`. |
| `RECEIVER_STATUS`, `RECEIVER_SECRET` | no | `200`, empty | compose only | Make the demo receiver fail, or verify signatures |

Generate an encryption key:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

The key in `docker-compose.yml` is a published, dev-only value. Never use it outside your machine.

## Project layout

```
src/webhooks/
  main.py        FastAPI app, /healthz, /readyz
  db.py          engine, sessions, Fernet encrypt/decrypt
  models.py      SQLAlchemy models
  auth.py        API keys: generation, hashing, the bearer dependency
  endpoints.py   /v1/endpoints routes, including rotate-secret
  events.py      /v1/events: rate limit, idempotent ingest, transactional fan-out
  worker.py      claim → SSRF check → sign → POST → record → retry/dead-letter
  ssrf.py        URL and IP policy, DNS resolution, IP pinning
  verify.py      sign() and verify(); stdlib-only, copyable by receivers
  receiver.py    demo receiver used by compose
  cli.py         create-tenant
migrations/      Alembic, written by hand
tests/           pytest against a real Postgres 16 (testcontainers)
docs/engineering-log.md   what broke and how it was fixed, per session
SPEC*.md         specs: the project map, delivery-worker, security
```

## Testing

```bash
pytest -q          # Docker must be running: the tests start a throwaway Postgres 16
```

There are 74 tests. They run against **real Postgres, never SQLite**: advisory locks, arrays and `SKIP LOCKED` are exactly what's under test, and SQLite has none of them. Outbound HTTP goes through `httpx.MockTransport`, and DNS through a fake resolver, so no test touches the network.

| File | Tests | What it proves |
|---|---|---|
| `test_endpoints.py` | 12 | Auth failures get 401; revoked keys; URL validation; the secret is shown once; soft delete; tenant isolation; cursor pagination returns every row exactly once |
| `test_events.py` | 7 | Fan-out only to matching, enabled, non-deleted endpoints; idempotency (sequential, 20 concurrent, key reused with a different body); per-tenant rate limit |
| `test_worker.py` | 10 | Success; retries and their timing; timeout; redirects not followed; dead-letter after 7; 4 KB cap; stable `X-Webhook-Id`; **10 concurrent workers send exactly once**; lapsed lease and fencing; disabled and deleted endpoints |
| `test_ssrf.py` | 31 | 22 blocked targets; allowed public targets; disguised loopback via the real resolver; DNS rebinding blocked before sending; DNS failure is retried; IP pinning with `Host` and SNI |
| `test_signing.py` | 14 | Valid and 10 kinds of invalid signature; multiple signatures; rotation (both signatures during the overlap, only the new one after); rotation is tenant-scoped |

**Mutation-checked:** each of these safeguards was removed on purpose to confirm a test then fails:
- the idempotency advisory lock
- `SKIP LOCKED`
- the fencing check
- IP pinning
- the every-DNS-record check

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs ruff, a format check, `mypy --strict`, the full test suite and a Docker build on every push and PR.

## Development

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"        # .venv/bin/pip on macOS/Linux
ruff check . && ruff format --check . && mypy --strict src && pytest -q
```

To run the services outside Docker, set the two required variables and then:
```bash
alembic upgrade head
uvicorn webhooks.main:app --reload
python -m webhooks.worker
```

To add a migration:
```bash
alembic revision -m "describe the change"    # then write upgrade() and downgrade() by hand
```

## Limits and what I'd change at scale

- **Postgres as the queue** is bounded by one primary's write capacity. The real ceiling hasn't been measured yet (see the load test on the roadmap). Past it:
  - partition `deliveries` by time, so old rows are dropped rather than deleted
  - or move dispatch to a real broker (Kafka, SQS), keeping the outbox and adding a relay
- **Head-of-line blocking:** a slow or dead endpoint still takes up worker capacity on each attempt. A per-endpoint circuit breaker is next on the roadmap, and per-tenant worker pools would follow at scale.
- **Fan-out inside the ingest transaction** gets slow for tenants with very many endpoints per event type. It would move to an async step that reads from the outbox.
- **The rate limit uses a fixed window,** which allows 2× bursts at window edges. A token bucket would fix that.
- **SSRF:** 6to4 and Teredo IPv6 addresses can embed IPv4 addresses and aren't unwrapped yet. That matters only if the workers get IPv6 egress.
- **`delivery_attempts` grows forever.** It needs a retention policy and time partitioning.

## Roadmap

- [x] Week 1–2: tenants, API keys, endpoints, idempotent ingest, transactional outbox
- [x] Week 3: delivery worker with `SKIP LOCKED`, leases, fencing, jittered retries, dead-letter
- [x] Week 4: SSRF defence with DNS-rebinding pinning, HMAC signing, secret rotation, rate limiting
- [ ] Week 5: circuit breaker, `/v1/deliveries` with replay, Prometheus `/metrics`, JSON logs with `request_id`, public deploy
- [ ] Week 6: a one-page dashboard showing endpoint health and a live delivery log
- [ ] Load test (k6 or Locust), with deliveries per second and p50/p99 latency published here

## License

[MIT](LICENSE)
