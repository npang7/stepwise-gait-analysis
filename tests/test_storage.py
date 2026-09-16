from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stepwise.models import JobManifest
from stepwise.storage import (
    ArtifactNotFoundError,
    JobNotFoundError,
    JobRepository,
)


class JobManifestTests(unittest.TestCase):
    def test_manifest_round_trip_uses_strict_json_values(self) -> None:
        created = "2026-08-01T00:00:00+00:00"
        manifest = JobManifest(
            run_id="3f509a00-f315-4e1f-86a7-d75ff3266df9",
            status="succeeded",
            created_at=created,
            updated_at=created,
            result={"metrics": {"missing": float("nan")}, "artifacts": []},
        )
        payload = manifest.to_dict()
        self.assertIsNone(payload["result"]["metrics"]["missing"])
        self.assertEqual(JobManifest.from_dict(payload), manifest)


class JobRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.repository = JobRepository(self.root)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_create_and_transitions_write_an_atomic_manifest(self) -> None:
        manifest = self.repository.create()
        self.assertEqual(manifest.status, "queued")
        self.repository.write_input(manifest.run_id, "walking.txt", b"sample")
        self.repository.mark_running(manifest.run_id)
        succeeded = self.repository.mark_succeeded(
            manifest.run_id,
            {
                "summary": {"samples": 1},
                "metrics": {},
                "risk_cards": [],
                "artifacts": [
                    {"name": "result.json", "media_type": "application/json", "size_bytes": 2}
                ],
            },
        )
        self.assertEqual(succeeded.status, "succeeded")
        manifest_path = self.root / manifest.run_id / "manifest.json"
        self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8"))["status"], "succeeded")
        self.assertFalse((manifest_path.parent / "manifest.json.tmp").exists())

    def test_staging_files_move_atomically_and_startup_cleanup_removes_stale_files(self) -> None:
        staged = self.repository.new_staging_path()
        staged.write_bytes(b"sample")
        manifest = self.repository.create()

        destination = self.repository.move_staged_input(manifest.run_id, "walking.txt", staged)

        self.assertEqual(destination.read_bytes(), b"sample")
        self.assertFalse(staged.exists())
        stale = self.repository.new_staging_path()
        stale.write_bytes(b"stale")
        self.assertEqual(self.repository.cleanup_staging(), [stale])
        self.assertEqual(list(self.repository.staging_dir.iterdir()), [])

    def test_delete_run_removes_an_unobservable_rejected_submission(self) -> None:
        manifest = self.repository.create()

        self.repository.delete_run(manifest.run_id)

        self.assertFalse(self.repository.run_dir(manifest.run_id).exists())

    def test_restart_recovery_marks_only_incomplete_jobs_failed(self) -> None:
        queued = self.repository.create()
        running = self.repository.create()
        succeeded = self.repository.create()
        self.repository.mark_running(running.run_id)
        self.repository.mark_succeeded(
            succeeded.run_id,
            {"summary": {}, "metrics": {}, "risk_cards": [], "artifacts": []},
        )

        recovered = self.repository.recover_incomplete()

        self.assertEqual(set(recovered), {queued.run_id, running.run_id})
        for run_id in recovered:
            manifest = self.repository.get(run_id)
            self.assertEqual(manifest.status, "failed")
            self.assertEqual(manifest.error_code, "service_restarted")
        self.assertEqual(self.repository.get(succeeded.run_id).status, "succeeded")

    def test_artifact_path_requires_a_manifest_allowlist_entry(self) -> None:
        manifest = self.repository.create()
        artifact_dir = self.repository.artifact_dir(manifest.run_id)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "result.json").write_text("{}", encoding="utf-8")
        self.repository.mark_succeeded(
            manifest.run_id,
            {
                "summary": {},
                "metrics": {},
                "risk_cards": [],
                "artifacts": [
                    {"name": "result.json", "media_type": "application/json", "size_bytes": 2}
                ],
            },
        )
        self.assertEqual(
            self.repository.artifact_path(manifest.run_id, "result.json").name,
            "result.json",
        )
        for name in ("../manifest.json", "secret.txt"):
            with self.subTest(name=name), self.assertRaises(ArtifactNotFoundError):
                self.repository.artifact_path(manifest.run_id, name)

    def test_invalid_run_ids_are_not_resolved_as_paths(self) -> None:
        with self.assertRaises(JobNotFoundError):
            self.repository.get("../outside")

    def test_cleanup_removes_only_expired_terminal_runs(self) -> None:
        expired = self.repository.create()
        active = self.repository.create()
        now = datetime(2026, 8, 1, tzinfo=UTC)
        old = (now - timedelta(hours=25)).isoformat()
        old_manifest = JobManifest(
            run_id=expired.run_id,
            status="failed",
            created_at=old,
            updated_at=old,
            error_code="test",
            error_message="test",
        )
        self.repository.save(old_manifest)

        removed = self.repository.cleanup_expired(24, now=now)

        self.assertEqual(removed, [expired.run_id])
        self.assertFalse((self.root / expired.run_id).exists())
        self.assertTrue((self.root / active.run_id).exists())

    def test_cleanup_skips_an_expired_run_until_its_artifact_lease_is_released(self) -> None:
        expired = self.repository.create()
        now = datetime(2026, 8, 1, tzinfo=UTC)
        old = (now - timedelta(hours=25)).isoformat()
        self.repository.save(
            JobManifest(
                run_id=expired.run_id,
                status="succeeded",
                created_at=old,
                updated_at=old,
                result={"summary": {}, "metrics": {}, "risk_cards": [], "artifacts": []},
            )
        )
        lease = self.repository.acquire_artifact_lease(expired.run_id)

        self.assertEqual(self.repository.cleanup_expired(24, now=now), [])
        self.assertTrue(self.repository.run_dir(expired.run_id).is_dir())

        lease.release()
        lease.release()
        self.assertEqual(self.repository.cleanup_expired(24, now=now), [expired.run_id])
        self.assertFalse((self.root / expired.run_id).exists())


if __name__ == "__main__":
    unittest.main()
