"""Sign and verify webhook payloads. Standard library only: receivers can copy this file.

Every delivery carries:
    X-Webhook-Timestamp: 1760112000
    X-Webhook-Signature: v1=<hex HMAC-SHA256(secret, f"{timestamp}.{raw body}")>

In a receiver:
    ok = verify(secret, raw_body, headers["X-Webhook-Timestamp"], headers["X-Webhook-Signature"])

`raw_body` must be the request bytes exactly as received. Re-serialising the parsed JSON
can reorder keys or change whitespace, and the signature will no longer match.
"""

import hashlib
import hmac
import time

TOLERANCE_SECONDS = 300


def sign(secret: str, timestamp: str, body: bytes) -> str:
    # The timestamp is inside the signed content, so it can't be swapped for a fresh one.
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify(
    secret: str,
    body: bytes,
    timestamp: str,
    signature_header: str,
    *,
    now: float | None = None,
    tolerance: int = TOLERANCE_SECONDS,
) -> bool:
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((time.time() if now is None else now) - ts) > tolerance:
        return False  # too old (a captured request being replayed) or too far in the future
    expected = sign(secret, timestamp, body).encode()
    # During a secret rotation the header holds one v1= signature per active secret.
    candidates = [p.strip() for p in signature_header.split(",")]
    return any(
        # compare_digest takes constant time, so the comparison leaks nothing about the
        # expected value. Bytes, because it rejects non-ASCII str instead of returning False.
        hmac.compare_digest(expected, c.removeprefix("v1=").encode())
        for c in candidates
        if c.startswith("v1=")
    )
