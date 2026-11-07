"""Local web UI for the engine (stdlib only, no extra dependencies).

    python -m fuel_cap_engine.webapp                  # http://127.0.0.1:8000
    python -m fuel_cap_engine.webapp --port 8080 --host 0.0.0.0
    python -m fuel_cap_engine.webapp --export snapshot.html   # static page with a demo quote baked in

Routes
    GET  /               the UI
    GET  /api/defaults   default form values
    POST /api/quote      QuoteRequest JSON -> quote payload (see service.py)
    POST /api/sweep      {"request": QuoteRequest, "caps": [..]} -> pricing menu
    GET  /healthz        liveness
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .service import QuoteRequest, defaults, json_safe, run_quote, run_sweep

log = logging.getLogger("fuel_cap_engine.webapp")

PAGE = Path(__file__).with_name("web") / "index.html"
SNAPSHOT_SLOT = '<script type="application/json" id="snapshot"></script>'
DOC_HEAD = (
    '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
    "</head>\n<body>\n"
)
DOC_TAIL = "\n</body>\n</html>\n"
MAX_BODY = 64 * 1024


def render_page(snapshot: dict[str, Any] | None = None, standalone: bool = True) -> str:
    """The UI page. With ``snapshot``, the page renders that data and runs offline."""
    html = PAGE.read_text(encoding="utf-8")
    if snapshot is not None:
        blob = json.dumps(json_safe(snapshot), separators=(",", ":")).replace("</", "<\\/")
        html = html.replace(SNAPSHOT_SLOT, f'<script type="application/json" id="snapshot">{blob}</script>')
    return DOC_HEAD + html + DOC_TAIL if standalone else html


class Handler(BaseHTTPRequestHandler):
    server_version = "FuelCapDesk/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # route access logs through logging
        log.info("%s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------------ helpers
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(json_safe(payload)).encode(), "application/json")

    def _body(self) -> Any:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("request body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    # ------------------------------------------------------------------- routes
    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(HTTPStatus.OK, render_page().encode(), "text/html; charset=utf-8")
        elif self.path == "/api/defaults":
            self._json(HTTPStatus.OK, defaults())
        elif self.path == "/healthz":
            self._json(HTTPStatus.OK, {"ok": True})
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": f"no route {self.path}"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self._body()
            if self.path == "/api/quote":
                self._json(HTTPStatus.OK, run_quote(QuoteRequest(**body)))
            elif self.path == "/api/sweep":
                req = QuoteRequest(**(body.get("request") or {}))
                self._json(HTTPStatus.OK, run_sweep(req, body.get("caps")))
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": f"no route {self.path}"})
        except ValidationError as exc:
            msgs = [f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors()]
            self._json(HTTPStatus.BAD_REQUEST, {"error": "; ".join(msgs)})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - surface engine failures to the UI
            log.exception("request failed")
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"engine error: {exc}"})


def export_snapshot(path: Path, req: QuoteRequest | None = None, standalone: bool = True) -> None:
    req = req or QuoteRequest()
    snap = {"quote": run_quote(req), "sweep": run_sweep(req)}
    path.write_text(render_page(snap, standalone=standalone), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fuel_cap_engine.webapp", description="Fuel Cap Desk web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--export", type=Path, help="write a static snapshot page and exit")
    p.add_argument("--fragment", action="store_true",
                   help="with --export: omit the <html>/<head> wrapper (for hosts that add their own)")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if args.export:
        export_snapshot(args.export, standalone=not args.fragment)
        log.info("snapshot written to %s", args.export)
        return 0
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    log.info("Fuel Cap Desk on http://%s:%d", args.host, httpd.server_address[1])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
