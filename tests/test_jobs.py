from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stepwise.jobs import JobManager, QueueFullError
from stepwise.models import AnalysisConfig
from stepwise.storage import JobRepository

FIXTURE_BYTES = (ROOT / "tests" / "fixtures" / "minimal_walk.txt").read_bytes()


def successful_runner(root: str, run_id: str, _config: dict) -> dict:
    repository = JobRepository(root)
    artifact_path = repository.artifact_dir(run_id) / "result.json"
    artifact_path.write_text("{}", encoding="utf-8")
    return {
        "summary": {"samples": 20},
        "metrics": {},
        "risk_cards": [],
        "artifacts": [
            {"name": "result.json", "media_type": "application/json", "size_bytes": 2}
        ],
    }


def slow_runner(_root: str, _run_id: str, _config: dict) -> dict:
    time.sleep(10)
    return {"summary": {}, "metrics": {}, "risk_cards": [], "artifacts": []}


def crashing_runner(_root: str, _run_id: str, _config: dict) -> dict:
    os._exit(17)


def wait_for_terminal(repository: JobRepository, run_id: str, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        manifest = repository.get(run_id)
        if manifest.status in {"succeeded", "failed"}:
            return manifest
        time.sleep(0.025)
    raise AssertionError(f"job {run_id} did not finish")


class JobManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_worker_process_completes_and_persists_strict_result(self) -> None:
        manager = JobManager(
            self.root,
            max_workers=1,
            max_queue=2,
            timeout_seconds=3,
            runner=successful_runner,
        )
        try:
            manifest = manager.submit(FIXTURE_BYTES, None, AnalysisConfig())
            terminal = wait_for_terminal(manager.repository, manifest.run_id)
        finally:
            manager.close()
        self.assertEqual(terminal.status, "succeeded")
        self.assertEqual(terminal.result["summary"]["samples"], 20)
        json.dumps(terminal.to_dict(), allow_nan=False)

    def test_timeout_terminates_worker_instead_of_leaving_it_running(self) -> None:
        manager = JobManager(
            self.root,
            max_workers=1,
            max_queue=1,
            timeout_seconds=0.15,
            runner=slow_runner,
        )
        started = time.monotonic()
        try:
            manifest = manager.submit(FIXTURE_BYTES, None, AnalysisConfig())
            terminal = wait_for_terminal(manager.repository, manifest.run_id)
            self.assertEqual(manager.active_count, 0)
        finally:
            manager.close()
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(terminal.error_code, "analysis_timeout")

    def test_worker_crash_is_reported_with_stable_error_code(self) -> None:
        manager = JobManager(
            self.root,
            max_workers=1,
            max_queue=1,
            timeout_seconds=3,
            runner=crashing_runner,
        )
        try:
            manifest = manager.submit(FIXTURE_BYTES, None, AnalysisConfig())
            terminal = wait_for_terminal(manager.repository, manifest.run_id)
        finally:
            manager.close()
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(terminal.error_code, "worker_crashed")

    def test_bounded_queue_rejects_excess_work(self) -> None:
        manager = JobManager(
            self.root,
            max_workers=1,
            max_queue=1,
            timeout_seconds=3,
            runner=slow_runner,
        )
        try:
            first = manager.submit(FIXTURE_BYTES, None, AnalysisConfig())
            deadline = time.monotonic() + 3
            while manager.repository.get(first.run_id).status != "running":
                if time.monotonic() >= deadline:
                    self.fail("first job never started")
                time.sleep(0.025)
            manager.submit(FIXTURE_BYTES, None, AnalysisConfig())
            with self.assertRaises(QueueFullError):
                manager.submit(FIXTURE_BYTES, None, AnalysisConfig())
        finally:
            manager.close()

    def test_submit_rejects_oversized_and_binary_inputs_before_queueing(self) -> None:
        manager = JobManager(
            self.root,
            max_workers=1,
            max_queue=1,
            timeout_seconds=3,
            max_upload_bytes=16,
            runner=successful_runner,
        )
        try:
            with self.assertRaisesRegex(ValueError, "upload_too_large"):
                manager.submit(FIXTURE_BYTES, None, AnalysisConfig())
            with self.assertRaisesRegex(ValueError, "binary_input"):
                manager.submit(b"\x00\x01", None, AnalysisConfig())
        finally:
            manager.close()


if __name__ == "__main__":
    unittest.main()
