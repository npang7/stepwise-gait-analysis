from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stepwise.api import UPLOAD_CHUNK_BYTES, _stream_upload, create_app
from stepwise.jobs import JobManager, SubmissionError
from stepwise.settings import Settings
from tests.test_jobs import slow_runner, successful_runner

FIXTURE_BYTES = (ROOT / "tests" / "fixtures" / "minimal_walk.txt").read_bytes()


class ChunkUpload:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.reads = 0

    async def read(self, size: int) -> bytes:
        assert size == UPLOAD_CHUNK_BYTES
        self.reads += 1
        return self.chunks.pop(0) if self.chunks else b""


def wait_for_api_terminal(client: TestClient, status_url: str, timeout: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(status_url)
        if response.json()["status"] in {"succeeded", "failed"}:
            return response.json()
        time.sleep(0.025)
    raise AssertionError("API job did not finish")


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _settings(self, **overrides) -> Settings:
        values = {
            "data_dir": self.data_dir,
            "max_upload_bytes": 2 * 1024 * 1024,
            "analysis_timeout_seconds": 3,
            "max_workers": 1,
            "max_queue": 2,
            "result_ttl_hours": 24,
        }
        values.update(overrides)
        return Settings(**values)

    def test_health_and_openapi_are_available(self) -> None:
        manager = JobManager(self.data_dir, runner=successful_runner)
        try:
            with TestClient(create_app(self._settings(), manager=manager)) as client:
                self.assertEqual(client.get("/healthz").json(), {"status": "ok"})
                self.assertEqual(client.get("/docs").status_code, 200)
                self.assertIn("/api/v1/analyses", client.get("/openapi.json").json()["paths"])
        finally:
            manager.close()

    def test_create_poll_result_and_allowlisted_artifact_download(self) -> None:
        manager = JobManager(self.data_dir, max_workers=1, max_queue=2, runner=successful_runner)
        try:
            with TestClient(create_app(self._settings(), manager=manager)) as client:
                response = client.post(
                    "/api/v1/analyses",
                    files={"walking": ("walk.txt", FIXTURE_BYTES, "text/plain")},
                )
                self.assertEqual(response.status_code, 202)
                created = response.json()
                self.assertEqual(created["status"], "queued")
                self.assertEqual(
                    (manager.repository.input_dir(created["run_id"]) / "walking.txt").read_bytes(),
                    FIXTURE_BYTES,
                )
                self.assertEqual(
                    created["status_url"], f"/api/v1/analyses/{created['run_id']}"
                )
                terminal = wait_for_api_terminal(client, created["status_url"])
                self.assertEqual(terminal["status"], "succeeded")

                result = client.get(f"/api/v1/analyses/{created['run_id']}/result")
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["summary"]["samples"], 20)
                artifact = client.get(
                    f"/api/v1/analyses/{created['run_id']}/artifacts/result.json"
                )
                self.assertEqual(artifact.status_code, 200)
                self.assertEqual(artifact.content, b"{}")
                self.assertEqual(
                    client.get(
                        f"/api/v1/analyses/{created['run_id']}/artifacts/manifest.json"
                    ).status_code,
                    404,
                )
        finally:
            manager.close()

    def test_real_pipeline_runs_through_multipart_worker_and_artifact_download(self) -> None:
        manager = JobManager(
            self.data_dir,
            max_workers=1,
            max_queue=1,
            timeout_seconds=20,
        )
        try:
            with TestClient(create_app(self._settings(), manager=manager)) as client:
                created = client.post(
                    "/api/v1/analyses",
                    files={"walking": ("walk.txt", FIXTURE_BYTES, "text/plain")},
                ).json()
                terminal = wait_for_api_terminal(client, created["status_url"], timeout=20)
                self.assertEqual(terminal["status"], "succeeded")
                result = client.get(created["result_url"]).json()
                self.assertEqual(result["summary"]["samples"], 20)
                self.assertEqual(len(result["artifacts"]), 12)
                report = client.get(
                    f"/api/v1/analyses/{created['run_id']}/artifacts/user_report.html"
                )
                self.assertEqual(report.status_code, 200)
                self.assertIn("StepWise gait screening report", report.text)
        finally:
            manager.close()

    def test_result_is_409_until_job_is_ready_and_unknown_job_is_404(self) -> None:
        manager = JobManager(
            self.data_dir,
            max_workers=1,
            max_queue=1,
            timeout_seconds=0.2,
            runner=slow_runner,
        )
        try:
            with TestClient(create_app(self._settings(), manager=manager)) as client:
                created = client.post(
                    "/api/v1/analyses",
                    files={"walking": ("walk.txt", FIXTURE_BYTES, "text/plain")},
                ).json()
                pending = client.get(f"/api/v1/analyses/{created['run_id']}/result")
                self.assertEqual(pending.status_code, 409)
                self.assertEqual(pending.json()["error"]["code"], "result_not_ready")
                self.assertEqual(
                    client.get(
                        "/api/v1/analyses/00000000-0000-0000-0000-000000000000"
                    ).status_code,
                    404,
                )
        finally:
            manager.close()

    def test_invalid_upload_mapping_size_and_queue_capacity_have_stable_errors(self) -> None:
        manager = JobManager(
            self.data_dir,
            max_workers=1,
            max_queue=1,
            timeout_seconds=3,
            runner=slow_runner,
        )
        try:
            with TestClient(create_app(self._settings(), manager=manager)) as client:
                binary = client.post(
                    "/api/v1/analyses",
                    files={"walking": ("walk.txt", b"\x00\x01", "text/plain")},
                )
                self.assertEqual(binary.status_code, 422)
                self.assertEqual(binary.json()["error"]["code"], "binary_input")
                self.assertEqual(list(manager.repository.staging_dir.iterdir()), [])

                duplicate_mapping = client.post(
                    "/api/v1/analyses",
                    files={"walking": ("walk.txt", FIXTURE_BYTES, "text/plain")},
                    data={
                        "sensor_mapping": json.dumps(
                            {
                                "heel": "P1",
                                "arch": "P1",
                                "medial_forefoot": "P3",
                                "lateral_forefoot": "P4",
                            }
                        )
                    },
                )
                self.assertEqual(duplicate_mapping.status_code, 422)
                self.assertEqual(duplicate_mapping.json()["error"]["code"], "invalid_mapping")

                first = client.post(
                    "/api/v1/analyses",
                    files={"walking": ("walk.txt", FIXTURE_BYTES, "text/plain")},
                ).json()
                deadline = time.monotonic() + 3
                while client.get(first["status_url"]).json()["status"] != "running":
                    if time.monotonic() >= deadline:
                        self.fail("first API job never started")
                    time.sleep(0.025)
                self.assertEqual(
                    client.post(
                        "/api/v1/analyses",
                        files={"walking": ("walk.txt", FIXTURE_BYTES, "text/plain")},
                    ).status_code,
                    202,
                )
                full = client.post(
                    "/api/v1/analyses",
                    files={"walking": ("walk.txt", FIXTURE_BYTES, "text/plain")},
                )
                self.assertEqual(full.status_code, 429)
                self.assertEqual(full.json()["error"]["code"], "queue_full")
                self.assertEqual(list(manager.repository.staging_dir.iterdir()), [])
                run_directories = [
                    path
                    for path in self.data_dir.iterdir()
                    if path.is_dir() and path.name != ".staging"
                ]
                self.assertEqual(len(run_directories), 2)
        finally:
            manager.close()

        small_root = self.data_dir / "small"
        small_manager = JobManager(
            small_root,
            max_upload_bytes=16,
            runner=successful_runner,
        )
        try:
            with TestClient(
                create_app(
                    self._settings(data_dir=small_root, max_upload_bytes=16),
                    manager=small_manager,
                )
            ) as client:
                too_large = client.post(
                    "/api/v1/analyses",
                    files={"walking": ("walk.txt", FIXTURE_BYTES, "text/plain")},
                )
                self.assertEqual(too_large.status_code, 413)
                self.assertEqual(too_large.json()["error"]["code"], "upload_too_large")
                self.assertEqual(list(small_manager.repository.staging_dir.iterdir()), [])
        finally:
            small_manager.close()


class StreamingUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_streamed_write_preserves_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "upload"
            upload = ChunkUpload([b"abc", b"def"])

            written = await _stream_upload(upload, destination, limit=6)

            self.assertEqual(written, 6)
            self.assertEqual(destination.read_bytes(), b"abcdef")

    async def test_stream_stops_at_cap_without_reading_remainder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "upload"
            upload = ChunkUpload([b"abcd", b"efgh", b"unread"])

            with self.assertRaisesRegex(SubmissionError, "upload_too_large"):
                await _stream_upload(upload, destination, limit=6)

            self.assertEqual(upload.reads, 2)
            self.assertEqual(destination.read_bytes(), b"abcd")

    async def test_health_remains_responsive_during_blocking_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            settings = Settings(
                data_dir=data_dir,
                max_upload_bytes=2 * 1024 * 1024,
                analysis_timeout_seconds=3,
                max_workers=1,
                max_queue=2,
                result_ttl_hours=24,
            )
            manager = JobManager(data_dir, runner=successful_runner)
            validation_started = threading.Event()
            release_validation = threading.Event()

            def slow_validation(_path: Path) -> None:
                validation_started.set()
                release_validation.wait(timeout=2)

            try:
                transport = httpx.ASGITransport(app=create_app(settings, manager=manager))
                with patch("stepwise.jobs.validate_stepwise_path", side_effect=slow_validation):
                    async with httpx.AsyncClient(
                        transport=transport, base_url="http://test"
                    ) as client:
                        upload_task = asyncio.create_task(
                            client.post(
                                "/api/v1/analyses",
                                files={"walking": ("walk.txt", FIXTURE_BYTES, "text/plain")},
                            )
                        )
                        started = await asyncio.to_thread(validation_started.wait, 1)
                        self.assertTrue(started)
                        start = time.monotonic()
                        health = await client.get("/healthz")
                        elapsed = time.monotonic() - start
                        release_validation.set()
                        created = await upload_task
                self.assertEqual(health.status_code, 200)
                self.assertLess(elapsed, 0.25)
                self.assertEqual(created.status_code, 202)
            finally:
                release_validation.set()
                manager.close()


if __name__ == "__main__":
    unittest.main()
