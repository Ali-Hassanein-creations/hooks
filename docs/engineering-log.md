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
