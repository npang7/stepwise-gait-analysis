from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "minimal_walk.txt"


class _SmokeHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, str]]] = []

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.requests.append(("GET", self.path))
        if self.path == "/healthz":
            self._json(200, {"status": "ok"})
            return
        if self.path == "/api/v1/analyses/run-1":
            self._json(200, {"run_id": "run-1", "status": "succeeded"})
            return
        if self.path == "/api/v1/analyses/run-1/result":
            self._json(
                200,
                {
                    "summary": {"samples": 20},
                    "metrics": {},
                    "risk_cards": [],
                    "artifacts": [
                        {
                            "name": "result.json",
                            "media_type": "application/json",
                            "size_bytes": 2,
                        }
                    ],
                },
            )
            return
        if self.path == "/api/v1/analyses/run-1/artifacts/result.json":
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json(404, {"error": {"code": "not_found"}})

    def do_POST(self) -> None:
        self.requests.append(("POST", self.path))
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if self.path != "/api/v1/analyses":
            self._json(404, {"error": {"code": "not_found"}})
            return
        if "multipart/form-data" not in self.headers.get("Content-Type", ""):
            self._json(422, {"error": {"code": "invalid_content_type"}})
            return
        if b"StepWise anonymous synthetic gait fixture" not in body:
            self._json(422, {"error": {"code": "missing_fixture"}})
            return
        self._json(
            202,
            {
                "run_id": "run-1",
                "status": "queued",
                "status_url": "/api/v1/analyses/run-1",
                "result_url": "/api/v1/analyses/run-1/result",
            },
        )


class ContainerSmokeTests(unittest.TestCase):
    def test_smoke_client_checks_health_analysis_result_and_artifact(self) -> None:
        try:
            from stepwise.ci_smoke import run_container_smoke
        except ImportError:
            self.fail("stepwise.ci_smoke should provide the CI smoke client")

        _SmokeHandler.requests = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _SmokeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            result = run_container_smoke(base_url, FIXTURE, timeout_seconds=5)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(result["run_id"], "run-1")
        self.assertEqual(result["artifact"], "result.json")
        self.assertEqual(result["artifact_bytes"], 2)
        self.assertEqual(
            _SmokeHandler.requests,
            [
                ("GET", "/healthz"),
                ("POST", "/api/v1/analyses"),
                ("GET", "/api/v1/analyses/run-1"),
                ("GET", "/api/v1/analyses/run-1/result"),
                ("GET", "/api/v1/analyses/run-1/artifacts/result.json"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
