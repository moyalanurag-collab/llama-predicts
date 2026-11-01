"""Dashboard server tests: pages load on a fresh install, actions are protected."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from ufc_edge.dashboard import api
from ufc_edge.dashboard.server import make_handler


class TestDashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp())
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api.Paths(cls.tmp / "data")))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path: str) -> tuple[int, dict | bytes]:
        try:
            with urllib.request.urlopen(self.base + path) as r:
                body = r.read()
                return r.status, json.loads(body) if path.startswith("/api/") else body
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def post(self, path: str, body: dict, header: bool = True) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json", **({"X-UFC-Edge": "1"} if header else {})}
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_static_files(self) -> None:
        code, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Llama Predicts", body)
        for f in ("/app.js", "/charts.js", "/app.css", "/fonts/archivo-var.woff", "/img/llama.png", "/img/llama-head.png"):
            self.assertEqual(self.get(f)[0], 200, f)

    def test_path_traversal_blocked(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/../server.py")
        self.assertEqual(ctx.exception.code, 404)

    def test_every_page_loads_on_a_fresh_install(self) -> None:
        for page in ("overview", "card", "plan", "bankroll", "performance", "settings"):
            code, body = self.get(f"/api/{page}?mode=paper")
            self.assertEqual(code, 200, (page, body))

    def test_actions_need_the_dashboard_header(self) -> None:
        code, _ = self.post("/api/kill", {"mode": "paper", "reason": "x"}, header=False)
        self.assertEqual(code, 403)

    def test_init_needs_exact_phrase_then_kill_and_release(self) -> None:
        bad = {"mode": "live", "bankroll": "40", "operator": "A", "reason": "r", "confirmation": "yes"}
        self.assertEqual(self.post("/api/init", bad)[0], 400)
        ok = dict(bad, confirmation="AUTHORISE BANKROLL 40.00")
        self.assertEqual(self.post("/api/init", ok), (200, {"ok": True}))
        self.assertEqual(self.post("/api/kill", {"mode": "live", "reason": "test"})[0], 200)
        _, ov = self.get("/api/overview?mode=live")
        self.assertTrue(ov["status"]["kill_switch"])
        self.assertEqual(self.post("/api/release", {"mode": "live", "operator": "A", "reason": "r",
                                                    "confirmation": "nope"})[0], 400)
        self.assertEqual(self.post("/api/release", {"mode": "live", "operator": "A", "reason": "r",
                                                    "confirmation": "RELEASE KILL SWITCH"})[0], 200)

    def test_record_needs_phrase_and_a_real_proposal(self) -> None:
        code, body = self.post("/api/record", {"mode": "paper", "key": "nope", "odds": "2", "stake": "1",
                                               "confirmation": "RECORD PAPER BET"})
        self.assertEqual(code, 400)
        self.assertIn("no longer in the plan", body["error"])
        code, _ = self.post("/api/record", {"mode": "paper", "key": "x", "odds": "2", "stake": "1",
                                            "confirmation": "wrong"})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
