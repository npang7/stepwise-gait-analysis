from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

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

    def assert_run_lock_is_held_by_another_thread(self, run_id: str) -> None:
        lock = self.repository._manifest_lock_for(run_id)
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
        self.assertFalse(acquired, "the run's manifest stripe was not held")

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

    def test_get_holds_the_run_lock_while_the_manifest_file_is_open(self) -> None:
        manifest = self.repository.create()
        manifest_path = self.repository.run_dir(manifest.run_id) / "manifest.json"
        reader_open = threading.Event()
        release_reader = threading.Event()
        errors: list[Exception] = []
        original_read_text = Path.read_text

        def controlled_read_text(
            path: Path,
            encoding: str | None = None,
            errors_mode: str | None = None,
        ) -> str:
            if path == manifest_path and threading.current_thread().name == "manifest-reader":
                with path.open("r", encoding=encoding, errors=errors_mode) as handle:
                    reader_open.set()
                    if not release_reader.wait(timeout=2):
                        raise AssertionError("reader release was not signalled")
                    return handle.read()
            return original_read_text(path, encoding=encoding, errors=errors_mode)

        def read_manifest() -> None:
            try:
                self.repository.get(manifest.run_id)
            except Exception as exc:  # noqa: BLE001 - thread failures are asserted below.
                errors.append(exc)

        with patch.object(Path, "read_text", controlled_read_text):
            reader = threading.Thread(target=read_manifest, name="manifest-reader")
            reader.start()
            self.assertTrue(reader_open.wait(timeout=1))
            try:
                self.assert_run_lock_is_held_by_another_thread(manifest.run_id)
            finally:
                release_reader.set()
                reader.join(timeout=2)

        self.assertFalse(reader.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.repository.get(manifest.run_id).status, "queued")

    def test_save_holds_the_run_lock_through_manifest_replacement(self) -> None:
        manifest = self.repository.create()
        manifest_path = self.repository.run_dir(manifest.run_id) / "manifest.json"
        temporary_path = manifest_path.with_suffix(".json.tmp")
        replacement_started = threading.Event()
        release_replacement = threading.Event()
        errors: list[Exception] = []
        original_replace = Path.replace

        def blocked_replace(path: Path, target: str | Path) -> Path:
            if (
                path == temporary_path
                and Path(target) == manifest_path
                and threading.current_thread().name == "manifest-writer"
            ):
                replacement_started.set()
                if not release_replacement.wait(timeout=2):
                    raise AssertionError("manifest replacement release was not signalled")
            return original_replace(path, target)

        def write_manifest() -> None:
            try:
                self.repository.save(manifest)
            except Exception as exc:  # noqa: BLE001 - thread failures are asserted below.
                errors.append(exc)

        with patch.object(Path, "replace", blocked_replace):
            writer = threading.Thread(target=write_manifest, name="manifest-writer")
            writer.start()
            self.assertTrue(replacement_started.wait(timeout=1))
            try:
                self.assert_run_lock_is_held_by_another_thread(manifest.run_id)
            finally:
                release_replacement.set()
                writer.join(timeout=2)

        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.repository.get(manifest.run_id).status, "queued")

    def test_transition_serializes_the_full_read_modify_write(self) -> None:
        manifest = self.repository.create()
        first_before_save = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        errors: list[Exception] = []
        original_save = self.repository.save
        result = {"summary": {}, "metrics": {}, "risk_cards": [], "artifacts": []}

        def controlled_save(candidate: JobManifest) -> JobManifest:
            if (
                threading.current_thread().name == "first-transition"
                and candidate.run_id == manifest.run_id
                and candidate.status == "running"
            ):
                first_before_save.set()
                if not release_first.wait(timeout=2):
                    raise AssertionError("first transition release was not signalled")
            return original_save(candidate)

        def mark_running() -> None:
            try:
                self.repository.mark_running(manifest.run_id)
            except Exception as exc:  # noqa: BLE001 - thread failures are asserted below.
                errors.append(exc)

        def mark_succeeded() -> None:
            second_started.set()
            try:
                self.repository.mark_succeeded(manifest.run_id, result)
            except Exception as exc:  # noqa: BLE001 - thread failures are asserted below.
                errors.append(exc)

        with patch.object(self.repository, "save", side_effect=controlled_save):
            first = threading.Thread(target=mark_running, name="first-transition")
            second = threading.Thread(target=mark_succeeded, name="second-transition")
            first.start()
            self.assertTrue(first_before_save.wait(timeout=1))
            try:
                self.assert_run_lock_is_held_by_another_thread(manifest.run_id)
                second.start()
                self.assertTrue(second_started.wait(timeout=1))
            finally:
                release_first.set()
                first.join(timeout=2)
                if second.ident is not None:
                    second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        final = self.repository.get(manifest.run_id)
        self.assertEqual(final.status, "succeeded")
        self.assertEqual(final.result, result)

    def test_different_manifest_lock_stripes_remain_parallel(self) -> None:
        first_uuid = uuid.UUID(int=0, version=4)
        second_uuid = uuid.UUID(int=1, version=4)
        with patch("stepwise.storage.uuid.uuid4", side_effect=[first_uuid, second_uuid]):
            first = self.repository.create()
            second = self.repository.create()

        lock_count = len(self.repository._manifest_locks)
        self.assertEqual(first_uuid.int % lock_count, 0)
        self.assertEqual(second_uuid.int % lock_count, 1)
        first_path = self.repository.run_dir(first.run_id) / "manifest.json"
        first_read = threading.Event()
        release_first = threading.Event()
        second_done = threading.Event()
        errors: list[Exception] = []
        original_read_text = Path.read_text

        def controlled_read_text(
            path: Path,
            encoding: str | None = None,
            errors_mode: str | None = None,
        ) -> str:
            if path == first_path and threading.current_thread().name == "first-stripe-reader":
                first_read.set()
                if not release_first.wait(timeout=2):
                    raise AssertionError("first stripe release was not signalled")
            return original_read_text(path, encoding=encoding, errors=errors_mode)

        def read_first() -> None:
            try:
                self.repository.get(first.run_id)
            except Exception as exc:  # noqa: BLE001 - thread failures are asserted below.
                errors.append(exc)

        def write_second() -> None:
            try:
                self.repository.mark_running(second.run_id)
            except Exception as exc:  # noqa: BLE001 - thread failures are asserted below.
                errors.append(exc)
            finally:
                second_done.set()

        with patch.object(Path, "read_text", controlled_read_text):
            reader = threading.Thread(target=read_first, name="first-stripe-reader")
            writer = threading.Thread(target=write_second, name="second-stripe-writer")
            reader.start()
            self.assertTrue(first_read.wait(timeout=1))
            writer.start()
            try:
                self.assertTrue(second_done.wait(timeout=1))
            finally:
                release_first.set()
                reader.join(timeout=2)
                writer.join(timeout=2)

        self.assertFalse(reader.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.repository.get(second.run_id).status, "running")

    def test_manifest_lock_is_reentrant(self) -> None:
        manifest = self.repository.create()
        lock = self.repository._manifest_lock_for(manifest.run_id)

        self.assertTrue(lock.acquire(blocking=False))
        try:
            self.assertTrue(lock.acquire(blocking=False))
            lock.release()
        finally:
            lock.release()

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

    def test_delete_run_holds_the_run_lock_through_recursive_removal(self) -> None:
        manifest = self.repository.create()
        removal_started = threading.Event()
        release_removal = threading.Event()
        errors: list[Exception] = []
        original_rmtree = shutil.rmtree

        def blocked_rmtree(path: str | Path, *args: object, **kwargs: object) -> None:
            if Path(path) == self.repository.run_dir(manifest.run_id):
                removal_started.set()
                if not release_removal.wait(timeout=2):
                    raise AssertionError("delete release was not signalled")
            original_rmtree(path, *args, **kwargs)

        def delete_manifest() -> None:
            try:
                self.repository.delete_run(manifest.run_id)
            except Exception as exc:  # noqa: BLE001 - thread failures are asserted below.
                errors.append(exc)

        with patch("stepwise.storage.shutil.rmtree", side_effect=blocked_rmtree):
            deleter = threading.Thread(target=delete_manifest, name="delete-writer")
            deleter.start()
            self.assertTrue(removal_started.wait(timeout=1))
            try:
                self.assert_run_lock_is_held_by_another_thread(manifest.run_id)
            finally:
                release_removal.set()
                deleter.join(timeout=2)

        self.assertFalse(deleter.is_alive())
        self.assertEqual(errors, [])
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

    def test_restart_recovery_cannot_overwrite_a_concurrent_success(self) -> None:
        queued = self.repository.create()
        recovery_ready = threading.Event()
        release_recovery = threading.Event()
        success_started = threading.Event()
        errors: list[Exception] = []
        original_mark_failed = self.repository.mark_failed

        def controlled_mark_failed(run_id: str, code: str, message: str) -> JobManifest:
            if threading.current_thread().name == "restart-recovery":
                recovery_ready.set()
                if not release_recovery.wait(timeout=2):
                    raise AssertionError("recovery release was not signalled")
            return original_mark_failed(run_id, code, message)

        def recover() -> None:
            try:
                self.repository.recover_incomplete()
            except Exception as exc:  # noqa: BLE001 - thread failures are asserted below.
                errors.append(exc)

        def mark_succeeded() -> None:
            success_started.set()
            try:
                self.repository.mark_succeeded(
                    queued.run_id,
                    {"summary": {}, "metrics": {}, "risk_cards": [], "artifacts": []},
                )
            except Exception as exc:  # noqa: BLE001 - thread failures are asserted below.
                errors.append(exc)

        with patch.object(
            self.repository,
            "mark_failed",
            side_effect=controlled_mark_failed,
        ):
            recovery = threading.Thread(target=recover, name="restart-recovery")
            success = threading.Thread(target=mark_succeeded, name="concurrent-success")
            recovery.start()
            self.assertTrue(recovery_ready.wait(timeout=1))
            try:
                self.assert_run_lock_is_held_by_another_thread(queued.run_id)
                success.start()
                self.assertTrue(success_started.wait(timeout=1))
            finally:
                release_recovery.set()
                recovery.join(timeout=2)
                if success.ident is not None:
                    success.join(timeout=2)

        self.assertFalse(recovery.is_alive())
        self.assertFalse(success.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.repository.get(queued.run_id).status, "succeeded")

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

    def test_cleanup_holds_the_run_lock_through_recursive_removal(self) -> None:
        expired = self.repository.create()
        now = datetime(2026, 8, 1, tzinfo=UTC)
        old = (now - timedelta(hours=25)).isoformat()
        self.repository.save(
            JobManifest(
                run_id=expired.run_id,
                status="failed",
                created_at=old,
                updated_at=old,
                error_code="test",
                error_message="expired",
            )
        )
        removal_started = threading.Event()
        release_removal = threading.Event()
        status_started = threading.Event()
        cleanup_result: list[str] = []
        status_result: list[str] = []
        errors: list[Exception] = []
        original_rmtree = shutil.rmtree

        def blocked_rmtree(path: str | Path, *args: object, **kwargs: object) -> None:
            if Path(path) == self.repository.run_dir(expired.run_id):
                removal_started.set()
                if not release_removal.wait(timeout=2):
                    raise AssertionError("recursive removal release was not signalled")
            original_rmtree(path, *args, **kwargs)

        def cleanup() -> None:
            try:
                cleanup_result.extend(self.repository.cleanup_expired(24, now=now))
            except Exception as exc:  # noqa: BLE001 - thread failures are asserted below.
                errors.append(exc)

        def get_status() -> None:
            status_started.set()
            try:
                status_result.append(self.repository.get(expired.run_id).status)
            except JobNotFoundError:
                status_result.append("not-found")
            except Exception as exc:  # noqa: BLE001 - thread failures are asserted below.
                errors.append(exc)

        with patch("stepwise.storage.shutil.rmtree", side_effect=blocked_rmtree):
            cleanup_thread = threading.Thread(target=cleanup, name="ttl-cleanup")
            status_thread = threading.Thread(target=get_status, name="cleanup-status-reader")
            cleanup_thread.start()
            self.assertTrue(removal_started.wait(timeout=1))
            try:
                self.assert_run_lock_is_held_by_another_thread(expired.run_id)
                status_thread.start()
                self.assertTrue(status_started.wait(timeout=1))
            finally:
                release_removal.set()
                cleanup_thread.join(timeout=2)
                if status_thread.ident is not None:
                    status_thread.join(timeout=2)

        self.assertFalse(cleanup_thread.is_alive())
        self.assertFalse(status_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(cleanup_result, [expired.run_id])
        self.assertEqual(status_result, ["not-found"])


if __name__ == "__main__":
    unittest.main()
