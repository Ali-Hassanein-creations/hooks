# Spec: delivery-worker

## Objective
Turn `pending` rows in `deliveries` into HTTP POSTs to the tenant's endpoint:
- record every attempt
- retry failures on a jittered exponential schedule
- dead-letter after the last attempt

With concurrent workers, each due delivery is attempted by exactly one worker at a time. A worker crash must never lose a delivery. The worst case is a redelivery, which at-least-once allows.

**Out of scope:** signing and the SSRF guard (module `security`); the circuit breaker, `/v1/deliveries` API, replay and metrics (module `operations`); graceful shutdown (the lease already covers a killed worker).

## Assumptions (correct any of these)
1. **The lease is `next_attempt_at`, so there is no `in_flight` state.** Claiming pushes `next_attempt_at` 60s ahead and increments `attempt_count`. A worker that dies simply lets the lease lapse, and the row becomes due again: no reaper is needed and the existing partial index still covers the claim query. `locked_until` becomes redundant and is dropped.
2. **Seven attempts total:** 1 initial attempt + 6 retries. Retry ceilings are `[10s, 1m, 5m, 30m, 2h, 6h]`. Delay = `uniform(0.5, 1.0) × ceiling`, which is equal jitter. Worst case is about 8.8h to dead-letter.
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
- **Claim:** one short transaction.
  ```sql
  UPDATE deliveries d
  SET next_attempt_at = now() + interval '60 seconds', attempt_count = d.attempt_count + 1
  FROM (SELECT d2.id FROM deliveries d2 JOIN endpoints e ON e.id = d2.endpoint_id
        WHERE d2.status = 'pending' AND d2.next_attempt_at <= now()
          AND e.status = 'enabled' AND e.deleted_at IS NULL
        ORDER BY d2.next_attempt_at LIMIT :batch
        FOR UPDATE OF d2 SKIP LOCKED) due
  WHERE d.id = due.id
  RETURNING d.id, d.attempt_count, ...
  ```
- **Deliver:** outside any transaction.
- **Record:** one transaction.
  - Insert a `delivery_attempts` row.
  - Update the delivery `WHERE id = :id AND attempt_count = :claimed`. That condition is a fencing token: a slow worker whose lease expired and was re-claimed cannot overwrite the newer state.
  - The update sets one of:
    - on 2xx: `succeeded`
    - after the 7th attempt fails: `dead_lettered`
    - otherwise: `pending` with `next_attempt_at = now() + delay`
- **Migration 0003:**
  - Create `delivery_attempts` with these columns: id, tenant_id, delivery_id, attempt_number, request_headers jsonb, response_status, response_body (≤4 KB), duration_ms, error, attempted_at. It has `UNIQUE(delivery_id, attempt_number)`.
  - Drop `deliveries.locked_until`.
  - Narrow the status CHECK to `pending | succeeded | dead_lettered`.
- **Demo receiver:** `scripts/receiver.py` uses only the standard library. It logs headers and body and replies with a status set by the `--status` flag. It runs as compose service `receiver` on port 80, so an endpoint of `http://receiver/hook` passes the port check. Week 4's SSRF guard will need a dev allowlist for it.

## Success criteria
Integration tests over real Postgres, with outbound HTTP via `httpx.MockTransport`:
- [ ] 200 → `succeeded`, with one attempt row holding status, duration and the sent headers
- [ ] 500 → `pending`, `attempt_count = 1`, and `next_attempt_at` within [now+5s, now+10s]
- [ ] a timeout gives an attempt row with `error` set and the delivery still `pending`
- [ ] 302 counts as a failure, and exactly one request was made (not followed)
- [ ] a 7th consecutive failure → `dead_lettered`
- [ ] a 1 MB response body is stored as ≤ 4096 bytes
- [ ] `X-Webhook-Id` is identical across retries of one delivery
- [ ] **10 concurrent claim-and-deliver runs against 1 due delivery produce exactly 1 request and 1 attempt row.** The test must fail without `SKIP LOCKED` or the fencing check.
- [ ] a lapsed lease is re-claimed with `attempt_count = 2`, and the stale worker's late record changes nothing
- [ ] a disabled endpoint's delivery is not claimed; deleting an endpoint dead-letters its pending deliveries
- [ ] a unit test on the delay function checks the bounds for every attempt
- [ ] ruff, `mypy --strict` and CI are green
- [ ] Manual: `docker compose up`, then send an event:
  - it lands on `receiver`
  - restart the receiver with `--status 500` and the attempts show the backoff
- [ ] The README gains "Claiming work" and "Retry, backoff and jitter" sections, and the engineering log is updated.

## Open questions
See the assumptions above. Items 1, 2 and 5 change the schema or behaviour, so they need a decision before any code is written.
