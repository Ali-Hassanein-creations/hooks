# Spec: security

**Status: done (2026-10-10).** Code: `src/webhooks/ssrf.py`, `src/webhooks/verify.py`, the rotation route in `endpoints.py`, the rate limit in `events.py`. Tests: `tests/test_ssrf.py`, `tests/test_signing.py`, `tests/test_events.py::test_ingest_is_rate_limited_per_tenant`.

## Objective
- Tenants pick the URLs we send requests to, so the service must never be usable to reach internal addresses (SSRF).
- Receivers must be able to prove a request came from us and is not a replay.
- Signing secrets must be rotatable without breaking receivers.
- One tenant must not be able to flood ingest.

## Decisions
1. **SSRF guard, checked twice:**
   - **At registration** (`POST`/`PATCH /v1/endpoints`), so a bad URL fails fast with 422.
   - **Before every delivery attempt**, because DNS can change in between.
   - The rules:
     - only http(s), only ports 80 and 443, and no `user:pass@` in the URL
     - we resolve the hostname ourselves, and **every** address it resolves to must be public (`ipaddress.is_global`, not multicast)
     - IPv4-mapped IPv6 is unwrapped first
2. **DNS-rebinding defence:** `ssrf.resolve()` returns the validated IP, and the worker connects to that IP while sending the original `Host` header and TLS SNI. TLS certificates are therefore still checked against the hostname, and no second DNS lookup can happen between the check and the connection.
3. **Blocked versus unresolvable:**
   - A non-public destination is dead-lettered immediately, since retrying can't make it safe.
   - A DNS failure is treated like any other failure and retried.
4. **Redirects are never followed.** A 3xx is a failed attempt.
5. **Signing:** `X-Webhook-Signature: v1=<hex HMAC-SHA256(secret, "{timestamp}.{raw body}")>` plus `X-Webhook-Timestamp`.
   - The body is serialised once; those exact bytes are signed and sent.
   - Receivers reject timestamps more than 5 minutes off, which defeats replays.
   - Comparison uses `hmac.compare_digest` on bytes.
   - `webhooks/verify.py` is standard-library-only, so receivers can copy it.
6. **Rotation:** `POST /v1/endpoints/{id}/rotate-secret` with an optional `{"overlap_hours": 0..168}` (default 24).
   - The new secret is returned once.
   - Until the overlap ends, the old secret keeps signing too, so the header carries two `v1=` signatures.
   - Rotating again during an overlap drops the oldest secret.
7. **Rate limit:** a fixed one-minute window per tenant, counted with an atomic upsert in Postgres (`rate_limits`, one row per tenant).
   - The default is 600 events per minute (`RATE_LIMIT_PER_MINUTE`). Going over returns 429 with `Retry-After`.
   - No Redis was needed, so no new service.
   - A fixed window allows up to 2× the limit across a minute boundary. That is accepted for now and marked with a `ponytail:` comment.
8. **Dev-only allowlist:** `SSRF_ALLOW_HOSTS`, set only in compose, lets the demo `receiver` (which has a private IP) through.

## Success criteria
- [x] 22 blocked targets are refused at registration with 422:
  - loopback in several notations
  - `0.0.0.0` and `169.254.169.254`
  - the RFC 1918 private ranges and CGNAT
  - IPv6 loopback, link-local and unique-local
  - IPv4-mapped IPv6
  - hostnames resolving to private addresses (including one where only one of several records is private)
  - URLs with credentials, a non-standard port, or a non-http scheme
- [x] The OS resolver never yields a target for disguised loopback (`127.1`, `2130706433`, hex, octal).
- [x] DNS rebinding after registration means no request is sent and the delivery is dead-lettered.
- [x] A DNS failure at delivery time is retried, not dead-lettered.
- [x] The worker connects to the validated IP with the original `Host` and SNI. The test fails if pinning is removed.
- [x] The verify helper accepts a valid signature and rejects:
  - a tampered body
  - a re-serialised body
  - the wrong secret
  - a timestamp that is too old, too new or swapped
  - a missing scheme, non-ASCII input, or an empty header
- [x] Rotation: both signatures verify during the overlap and only the new one after it; the route is tenant-scoped and the overlap bounds are validated.
- [x] Rate limit: the 4th request in a minute gets 429 with `Retry-After` when the limit is 3, and other tenants are unaffected.
- [x] Docker check:
  - a metadata URL gets 422
  - the receiver reports `signature=valid`
  - after a rotation, the receiver still holding the old secret also reports `valid`
