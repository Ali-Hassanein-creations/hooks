"""Demo webhook receiver: python -m webhooks.receiver [--port 80] [--status 200] [--secret S]

Logs every delivery it gets and replies with a fixed status, so you can watch the worker
succeed (200) or back off and retry (e.g. --status 500). With --secret it also checks each
signature the way a real receiver should (see webhooks/verify.py).
"""

import argparse
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from webhooks.verify import verify

log = logging.getLogger("webhooks.receiver")


def main() -> None:
    parser = argparse.ArgumentParser(prog="webhooks.receiver")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--status", type=int, default=200)
    parser.add_argument("--secret", default="", help="endpoint secret, to verify signatures")
    args = parser.parse_args()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            signature = "not checked"
            if args.secret:
                ok = verify(
                    args.secret,
                    body,
                    self.headers.get("X-Webhook-Timestamp", ""),
                    self.headers.get("X-Webhook-Signature", ""),
                )
                signature = "valid" if ok else "INVALID"
            log.info(
                "id=%s event=%s signature=%s -> %d body=%s",
                self.headers.get("X-Webhook-Id"),
                self.headers.get("X-Webhook-Event"),
                signature,
                args.status,
                body.decode(errors="replace"),
            )
            self.send_response(args.status)
            self.end_headers()

        def log_message(self, format: str, *a: object) -> None:  # silence default access log
            pass

    logging.basicConfig(level=logging.INFO, format="%(asctime)s receiver %(message)s")
    log.info("listening on :%d, answering %d", args.port, args.status)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()
