"""Demo webhook receiver: python -m webhooks.receiver [--port 80] [--status 200]

Logs every delivery it gets and replies with a fixed status, so you can watch the worker
succeed (200) or back off and retry (e.g. --status 500). Standard library only.
"""

import argparse
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("webhooks.receiver")


def main() -> None:
    parser = argparse.ArgumentParser(prog="webhooks.receiver")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--status", type=int, default=200)
    args = parser.parse_args()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            log.info(
                "id=%s event=%s -> %d body=%s",
                self.headers.get("X-Webhook-Id"),
                self.headers.get("X-Webhook-Event"),
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
