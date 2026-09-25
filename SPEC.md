# Spec: Webhook Delivery Platform

## Objective
A multi-tenant service that accepts events from a tenant's application and delivers them to the HTTP endpoints the tenant registered. It guarantees **at-least-once delivery**, best-effort ordering per endpoint, and an auditable record of every attempt. Receivers deduplicate on `X-Webhook-Id`.

It serves two users:
- **The tenant's developer** registers endpoints, sends events, and inspects or replays deliveries.
- **The tenant's receiver** gets signed, verifiable POSTs.

Success means a portfolio-grade system whose hard parts are built and proven by tests against real Postgres:
- the outbox
- claiming work
- retries
- SSRF defence
- signing

## Capability map

| Module id | Responsibility | Depends on | Status |
|---|---|---|---|
| `ingest` | tenants, API keys, endpoints CRUD, idempotent `POST /v1/events` with transactional fan-out | none | **done** (see README) |
| `delivery-worker` | claim due deliveries, POST them, record attempts, retry with jittered backoff, dead-letter | ingest | **next**, see [SPEC-delivery-worker.md](SPEC-delivery-worker.md) |
| `security` | HMAC signing with timestamp, SSRF guard including DNS-rebinding pinning, secret rotation overlap, per-tenant ingest rate limit | delivery-worker | planned |
| `operations` | circuit breaker, `/v1/deliveries` API and replay, Prometheus `/metrics`, JSON logs with request_id, public deploy | delivery-worker | planned |
| `dashboard` | one page of endpoint health and a live delivery log (optional; cut first) | operations | planned |

Build order: ingest → delivery-worker → security → operations → dashboard.

security and operations both depend only on delivery-worker. security goes first because it is what sets the project apart.

## Tech stack
- Python 3.12+ and FastAPI
- SQLAlchemy 2 async with asyncpg, on PostgreSQL 16
- Alembic, with migrations written by hand
- Pydantic v2 and `cryptography` (Fernet)
- httpx, which becomes a runtime dependency in delivery-worker
- Tooling: pytest, pytest-asyncio, testcontainers, ruff, mypy `--strict`
- Docker Compose; CI on GitHub Actions

## Commands
```
Install:    python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"
Lint:       ruff check . && ruff format --check .
Types:      mypy --strict src
Test:       pytest -q                       # Docker must be running (testcontainers)
Run:        docker compose up -d --build
Tenant:     docker compose exec api python -m webhooks.cli create-tenant NAME
Migrate:    alembic upgrade head            # needs DATABASE_URL
New mig:    alembic revision -m "..."        # hand-written; env.py has no autogenerate metadata
```

## Project structure
```
src/webhooks/     one module per concern: db, models, auth, endpoints (router+schemas), events, main, cli
migrations/       numbered, hand-written, each with a working downgrade
tests/            integration tests over real Postgres; conftest sets env before importing the app
docs/engineering-log.md   what broke and why, per session
SPEC*.md          this file + one spec per module id
```

## Code style
Every query filters on `tenant_id`. A resource from another tenant returns 404. Schemas live next to their router.
```python
async def _get(session: DbSession, tenant: uuid.UUID, id: uuid.UUID) -> Endpoint:
    ep = await session.scalar(
        select(Endpoint).where(
            Endpoint.id == id, Endpoint.tenant_id == tenant, Endpoint.deleted_at.is_(None)
        )
    )
    if ep is None:  # other tenants' endpoints 404 too, so ids don't leak
        raise HTTPException(404, "endpoint not found")
    return ep
```
- Lines are at most 100 characters.
- Statuses are TEXT + CHECK, never Postgres enums.
- The database clock (`now()`) is the only clock used for scheduling.
- A deliberate shortcut with a known ceiling gets a `# ponytail:` comment naming the upgrade path.

## Testing strategy
- pytest-asyncio with one session event loop and a single Postgres 16 container, migrated with `alembic upgrade head`. Every table is truncated between tests.
- **No SQLite, ever.** Advisory locks, arrays and `SKIP LOCKED` are the parts under test.
- Test through the HTTP API where one exists; call worker functions directly otherwise.
- Every concurrency claim gets a test that fails if the guard is removed. That was verified for the ingest advisory lock.
- Outbound HTTP in tests goes through `httpx.MockTransport`. No test touches the network.
- There is no coverage target. Failure paths are what must be covered.

## Boundaries
- **Always:**
  - Filter every query on `tenant_id`.
  - Run ruff, mypy and pytest before committing.
  - Make migrations reversible.
  - Keep secrets out of logs and responses (the signing secret is shown only once).
- **Ask first:**
  - Adding a dependency or service (including Redis for rate limiting).
  - A destructive schema change once deployed.
  - Changing the delivery guarantee or the status set.
  - Changing CI.
  - Deploying anywhere.
- **Never:**
  - Commit real secrets. The compose Fernet key is the one labelled dev-only exception.
  - Hold a DB transaction open across outbound HTTP.
  - Follow redirects.
  - Store unbounded response bodies.
  - Compare secrets with `==`.
  - Delete or skip a failing test to get to green.

## Success criteria (whole project)
- Every module's spec criteria are met, CI is green, and `docker compose up` shows a delivery succeed, fail, retry and dead-letter.
- The README has sections for the five hard parts: outbox, idempotency, retry, SSRF, and signing.
