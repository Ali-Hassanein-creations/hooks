# Engineering log

## 2026-09-25 — weeks 1–2: skeleton, auth, endpoints, ingest

- I chose Postgres over Redis as the queue so the ingest write and the "job" are the same row: that is the outbox, with no relay needed.
- **Idempotency race.** The unique constraint alone isn't enough. Twenty concurrent requests with the same key made one insert succeed and the other nineteen fail with 500s. `pg_advisory_xact_lock(hashtextextended(tenant:key))` makes them wait instead. I removed the lock to confirm the concurrency test fails without it.
- **Naive vs aware timestamps.** SQLAlchemy mapped `datetime` to a naive `TIMESTAMP` while the migrations used `timestamptz`. Cursor pagination blew up when binding an aware datetime. Fixed with `type_annotation_map = {datetime: DateTime(timezone=True)}` on `Base`.
- **Expired attributes.** On a duplicate key, rolling back before serialising expired the ORM object and caused `MissingGreenlet`. The fix is to serialise first, then roll back to release the lock.
- **Statuses are TEXT + CHECK, not Postgres enums.** `ALTER TYPE … ADD VALUE` can't run in a transaction, which makes enum migrations awkward.
- **Deferred:**
  - the `delivery_attempts` table (week 3, with the worker)
  - secret-rotation columns (week 4; an additive migration)
  - a request-hash check for a key reused with a different body

## 2026-10-10 — week 3: the delivery worker

- **The lease is `next_attempt_at`.** Claiming pushes it 60s ahead instead of setting an `in_flight` status, so a crashed worker needs no reaper and the existing partial index still serves the claim query. Dropped the now-redundant `locked_until` column.
- **Fencing token.** The record step only updates the row if `attempt_count` still equals what the worker claimed with. Without that, a worker that stalled past its lease could overwrite the result of the worker that re-claimed the row. I removed the check to confirm the lease test fails without it, and did the same with `SKIP LOCKED` for the concurrency test.
- **Never download big error pages.** The response is streamed and reading stops at 4 KB.
- **Test collection broke on a top-level import.** `webhooks.db` reads `DATABASE_URL` at import time, and the Postgres container used to start inside a session fixture, after collection. Moved it to `pytest_sessionstart`, which runs before any test module is imported.
- **Docker check:** with the receiver answering 500, attempt 2 came 9s after attempt 1 (ceiling 10s) and attempt 3 was scheduled 47s later (ceiling 60s).
