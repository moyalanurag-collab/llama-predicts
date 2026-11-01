"""A small local web server for the dashboard.

* It listens on 127.0.0.1 only, so it's reachable from this computer and nowhere else.
* Every state-changing request must carry the ``X-UFC-Edge`` header. Browsers
  won't let another website add a custom header without permission, so a
  malicious page can't trigger actions here (CSRF protection).
* It uses the Python standard library only, so there's nothing extra to install.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..errors import UfcEdgeError
from . import api

STATIC = Path(__file__).parent / "static"
_write_lock = threading.Lock()  # one state-changing action at a time

mimetypes.add_type("font/woff", ".woff")
mimetypes.add_type("text/javascript", ".js")


def make_handler(paths: api.Paths):  # type: ignore[no-untyped-def]
    class Handler(BaseHTTPRequestHandler):
        server_version = "UFCEdge/1.0"

        def log_message(self, fmt: str, *args: object) -> None:  # quieter console
            if "/api/" in (args[0] if args else ""):
                return

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: object) -> None:
            self._send(code, json.dumps(api.clean(obj), default=str).encode(), "application/json")

        def _mode(self, q: dict) -> str:
            return q.get("mode", ["paper"])[0]

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            q = parse_qs(url.query)
            routes = {
                "/api/overview": lambda: api.overview(paths, self._mode(q)),
                "/api/card": lambda: api.card(paths),
                "/api/plan": lambda: api.plan(paths, self._mode(q)),
                "/api/bankroll": lambda: api.bankroll(paths, self._mode(q)),
                "/api/performance": lambda: api.performance(paths, self._mode(q)),
                "/api/settings": lambda: api.settings(paths, self._mode(q)),
            }
            if url.path in routes:
                try:
                    self._json(200, routes[url.path]())
                except UfcEdgeError as exc:
                    self._json(400, {"error": str(exc)})
                except Exception as exc:  # noqa: BLE001
                    traceback.print_exc()
                    self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
                return
            rel = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
            target = (STATIC / rel).resolve()
            if STATIC.resolve() not in target.parents or not target.is_file():
                self._send(404, b"Not found", "text/plain")
                return
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            self._send(200, target.read_bytes(), ctype)

        def do_POST(self) -> None:  # noqa: N802
            if self.headers.get("X-UFC-Edge") != "1":
                self._json(403, {"error": "Missing dashboard header."})
                return
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "Bad JSON"})
                return
            mode = body.get("mode", "paper")
            actions = {
                "/api/kill": lambda: api.kill(paths, mode, body.get("reason", "")),
                "/api/release": lambda: api.release(paths, mode, body.get("operator", ""), body.get("reason", ""),
                                                    body.get("confirmation", "")),
                "/api/init": lambda: api.init_bankroll(paths, mode, str(body.get("bankroll", "")),
                                                       body.get("operator", ""), body.get("reason", ""),
                                                       body.get("confirmation", "")),
                "/api/record": lambda: api.record_bet(paths, mode, body.get("key", ""), str(body.get("odds", "")),
                                                      str(body.get("stake", "")), body.get("confirmation", "")),
                "/api/refresh": lambda: api.refresh(paths),
            }
            if url.path not in actions:
                self._json(404, {"error": "Unknown action"})
                return
            with _write_lock:
                try:
                    self._json(200, actions[url.path]())
                except (UfcEdgeError, ValueError) as exc:
                    self._json(400, {"error": str(exc)})
                except Exception as exc:  # noqa: BLE001
                    traceback.print_exc()
                    self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    return Handler


def serve(data_dir: str | Path = "data", port: int = 8765, open_browser: bool = True) -> None:
    paths = api.Paths(data_dir)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(paths))
    url = f"http://127.0.0.1:{port}/"
    print(f"Llama Predicts dashboard running at {url}")
    print("Leave this window open while you use it. Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        httpd.server_close()
