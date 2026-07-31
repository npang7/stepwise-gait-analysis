from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("MPLBACKEND", "Agg")
_DATA_DIR = tempfile.mkdtemp(prefix="stepwise_api_tests_")
os.environ["STEPWISE_DATA_DIR"] = _DATA_DIR

from stepwise_cloudrun_flask.app import app

FIXTURE = Path(__file__).parent / "fixtures" / "minimal_walk.txt"


class CloudApiTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(_DATA_DIR, ignore_errors=True)

    def setUp(self) -> None:
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def test_health_endpoint(self) -> None:
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "ok")

    def test_missing_walking_text_returns_400(self) -> None:
        response = self.client.post("/api/analyze-text", json={})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json["ok"])

    @patch("stepwise_cloudrun_flask.app.run_stepwise_pipeline", return_value=({}, ""))
    def test_oversized_walking_text_returns_413(self, pipeline) -> None:
        response = self.client.post(
            "/api/analyze-text",
            json={"walkingText": "x" * 2_000_001},
        )
        self.assertEqual(response.status_code, 413)
        self.assertFalse(response.json["ok"])
        pipeline.assert_not_called()

    @patch("stepwise_cloudrun_flask.app.run_stepwise_pipeline")
    def test_pipeline_timeout_returns_504_json(self, pipeline) -> None:
        pipeline.side_effect = subprocess.TimeoutExpired(["python"], 120)
        response = self.client.post(
            "/api/analyze-text",
            json={"walkingText": FIXTURE.read_text(encoding="utf-8")},
        )
        self.assertEqual(response.status_code, 504)
        self.assertFalse(response.json["ok"])
        self.assertIn("timed out", response.json["error"].lower())

    def test_valid_fixture_runs_end_to_end(self) -> None:
        first = self.client.post(
            "/api/analyze-text",
            json={"walkingText": FIXTURE.read_text(encoding="utf-8")},
        )
        second = self.client.post(
            "/api/analyze-text",
            json={"walkingText": FIXTURE.read_text(encoding="utf-8")},
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        payload = first.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["samples"], 20)
        self.assertIn("user_report", payload["urls"])
        self.assertRegex(payload["run_id"], r"^api_[0-9a-f]{32}$")
        self.assertNotEqual(payload["run_id"], second.get_json()["run_id"])


if __name__ == "__main__":
    unittest.main()
