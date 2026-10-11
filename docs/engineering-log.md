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

## 2026-10-10 — week 4: security

- **SSRF is checked twice.** At registration for fast feedback, and before every attempt, because DNS can change in between. The worker connects to the IP it just validated and passes the hostname as `Host` and TLS SNI (httpx's `sni_hostname` extension), so certificates are still verified and there's no second lookup to race.
- **Every record must be public, not just the first.** A hostname with one public and one private A record would otherwise get through half the time. There's a test for it, and it fails if only the first record is checked.
- **Blocked is not the same as unresolvable.** A private destination is dead-lettered at once; a DNS failure is retried like any other error.
- **IPv4-mapped IPv6.** Python 3.13's `is_global` already treats `::ffff:127.0.0.1` as loopback, so removing my unwrap didn't fail any test locally. I kept it because the Docker image and CI use 3.12, and the guard shouldn't depend on the Python version.
- **Disguised loopback** (`127.1`, `2130706433`, `0x7f000001`): Linux's resolver parses these as 127.0.0.1 (then blocked) and Windows's rejects them (unresolvable). The test accepts either refusal; what matters is that no target ever comes back.
- **`hmac.compare_digest` raises on non-ASCII str.** The verify helper compares bytes so a garbage header returns False instead of crashing the receiver.
- **`Row.count` is a method.** SQLAlchemy rows are tuples, so `row.count` returns `tuple.count`, not a column named `count`. The comparison would have raised at runtime; mypy caught it first. Renamed the column to `n`.
- **Rate limit in Postgres, not Redis.** An atomic upsert on one row per tenant does it and adds no new service. The fixed window allows 2× bursts across a minute boundary; good enough for now.
- **Docker check:** a metadata URL got 422; the receiver verified the signature; after a rotation it still verified with the old secret during the overlap.

## 2026-10-11 — encryption key out of the repo

- **GitGuardian flagged `SECRET_ENCRYPTION_KEY` in `docker-compose.yml`.** It was a dev-only Fernet key that only ever encrypted endpoint secrets in a local demo database, so nothing real was exposed. But a key in a public repo is still a bad habit, and every change to the file re-triggers the alert.
- **Fix:** compose now reads the key from a git-ignored `.env` and refuses to start without one (`${SECRET_ENCRYPTION_KEY:?…}`). `.env.example` and the README quickstart give a one-line, standard-library command to generate a fresh key.
- **History:** the old key is still in earlier commits. Rewriting public history isn't worth it for a key that protects nothing; it must simply never be used anywhere.
- **Gotcha:** a database encrypted with the old key can't decrypt its endpoint secrets with a new one. For the demo that means `docker compose down -v` once; for a real deployment, rotate by listing both keys (new first) until secrets are re-encrypted.
