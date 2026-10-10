# Spec: delivery-worker

**Status: done (2026-10-10).** Code: `src/webhooks/worker.py`. Tests: `tests/test_worker.py`.

## Objective
Turn `pending` rows in `deliveries` into HTTP POSTs to the tenant's endpoint:
- record every attempt
- retry failures on a jittered exponential schedule
- dead-letter after the last attempt

With concurrent workers, each due delivery is attempted by exactly one worker at a time. A worker crash must never lose a delivery. The worst case is a redelivery, which at-least-once allows.

**Out of scope:** signing and the SSRF guard (module `security`); the circuit breaker, `/v1/deliveries` API, replay and metrics (module `operations`); graceful shutdown (the lease already covers a killed worker).

## Decisions (approved 2026-09-25)
1. **The lease is `next_attempt_at`, so there is no `in_flight` state.** Claiming pushes `next_attempt_at` 60s ahead and increments `attempt_count`. A worker that dies simply lets the lease lapse, and the row becomes due again: no reaper is needed and the existing partial index still covers the claim query. `locked_until` becomes redundant and is dropped.
2. **Seven attempts total:** 1 initial attempt + 6 retries. Retry ceilings are `[10s, 1m, 5m, 30m, 2h, 6h]`. Delay = `uniform(0, ceiling)`, which is full jitter, per the build spec. On average it takes about 4.3h to dead-letter, and at most about 8.6h. A retry can fire almost immediately; that is accepted, since spreading retries out matters more than a minimum gap.
3. **Success means 2xx only.** 3xx (never followed), 4xx, 5xx, timeouts and connection errors all go to retry. No special handling for 410 yet.
4. **Timeouts:** connect 3s, read 10s, 15s total per attempt. At most 4 KB of the response body is read and stored; the rest is never downloaded.
5. **Endpoint state:**
   - Deliveries to a disabled endpoint wait. They resume when it is re-enabled, all at once.
   - Soft-deleting an endpoint dead-letters its pending deliveries in the same transaction.
6. **Request sent:**
   - `POST` of the payload serialised once to bytes (which week 4 will sign)
   - `Content-Type: application/json`
   - `X-Webhook-Id: <delivery id>`, the same on every retry
   - `X-Webhook-Event: <event_type>`
   - `User-Agent: webhooks/0.1`

## Design
- **Process:** `python -m webhooks.worker` is the same image as a new compose service `worker`. It runs one loop:
  1. Claim a batch.
  2. Deliver the batch concurrently, capped by `WORKER_CONCURRENCY` (default 10).
  3. Sleep 1s if the batch was empty.
- **Claim:** one short transaction, `worker.CLAIM`. A CTE selects due rows with `FOR UPDATE OF d SKIP LOCKED`; the `UPDATE` pushes `next_attempt_at` forward by the 60s lease, increments `attempt_count`, and returns everything the send needs (event type and payload, endpoint URL).
- **Deliver:** outside any transaction.
- **Record:** one transaction.
  - Insert a `delivery_attempts` row.
  - Update the delivery `WHERE id = :id AND attempt_count = :claimed`. That condition is a fencing token: a slow worker whose lease expired and was re-claimed cannot overwrite the newer state.
  - The update sets one of:
    - on 2xx: `succeeded`
    - after the 7th attempt fails: `dead_lettered`
    - otherwise: `pending` with `next_attempt_at = now() + delay`
- **Migration 0004** (0003 went to `events.request_hash`):
  - Create `delivery_attempts` with these columns: id, tenant_id, delivery_id, attempt_number, request_headers jsonb, response_status, response_body (≤4 KB), duration_ms, error, attempted_at. It has `UNIQUE(delivery_id, attempt_number)`.
  - Drop `deliveries.locked_until`.
  - Narrow the status CHECK to `pending | succeeded | dead_lettered`.
- **Demo receiver:** `python -m webhooks.receiver` uses only the standard library. It logs each delivery and replies with the status set by `--status`, which compose sets from `RECEIVER_STATUS` (default 200). It runs as compose service `receiver` on port 80, so `http://receiver/hook` passes the port check. The week-4 SSRF guard allowlists it in compose only.

## Success criteria
Integration tests over real Postgres, with outbound HTTP via `httpx.MockTransport`:
- [x] 200 → `succeeded`, with one attempt row holding status, duration and the sent headers
- [x] 500 → `pending`, `attempt_count = 1`, and `next_attempt_at` within [now, now+10s]
- [x] a timeout gives an attempt row with `error` set and the delivery still `pending`
- [x] 302 counts as a failure, and exactly one request was made (not followed)
- [x] a 7th consecutive failure → `dead_lettered`
- [x] a 1 MB response body is stored as ≤ 4096 bytes
- [x] `X-Webhook-Id` is identical across retries of one delivery
- [x] **10 concurrent claim-and-deliver runs against 1 due delivery produce exactly 1 request and 1 attempt row.** The test must fail without `SKIP LOCKED` or the fencing check.
- [x] a lapsed lease is re-claimed with `attempt_count = 2`, and the stale worker's late record changes nothing
- [x] a disabled endpoint's delivery is not claimed; deleting an endpoint dead-letters its pending deliveries
- [x] a unit test on the delay function checks the bounds for every attempt
- [x] ruff, `mypy --strict` and CI are green
- [x] Manual: `docker compose up`, then send an event:
  - it lands on `receiver`
  - restart it with `RECEIVER_STATUS=500 docker compose up -d receiver` and the attempts show the backoff
- [x] The README gains "Claiming work" and "Retry, backoff and jitter" sections, and the engineering log is updated.

## Open questions
None. Items 1, 2 and 5 were decided on 2026-09-25: lease via `next_attempt_at`, full jitter, disabled endpoints wait and deleted endpoints dead-letter.
