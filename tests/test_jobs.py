from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stepwise.jobs import JobManager, QueueFullError, _ActiveJob
from stepwise.models import AnalysisConfig, JobManifest
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


class StubProcess:
    def __init__(
        self,
        *,
        alive: bool,
        start_error: Exception | None = None,
        start_alive_on_error: bool = False,
        terminate_error: Exception | None = None,
    ) -> None:
        self.alive = alive
        self.start_error = start_error
        self.start_alive_on_error = start_alive_on_error
        self.terminate_error = terminate_error
        self.started = False
        self.terminate_calls = 0
        self.join_calls = 0

    def is_alive(self) -> bool:
        return self.alive

    def start(self) -> None:
        if self.start_error is not None:
            error = self.start_error
            self.start_error = None
            if self.start_alive_on_error:
                self.started = True
                self.alive = True
            raise error
        self.started = True
        self.alive = True

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.terminate_error is not None:
            error = self.terminate_error
            self.terminate_error = None
            raise error
        self.alive = False

    def join(self, timeout: float | None = None) -> None:
        del timeout
        self.join_calls += 1


class StubResultQueue:
    def __init__(
        self,
        item: tuple[str, dict[str, Any]] | None = None,
        *,
        close_error: Exception | None = None,
    ) -> None:
        self.item = item
        self.close_error = close_error
        self.get_calls = 0
        self.close_calls = 0
        self.closed = False

    def get(self, timeout: float) -> tuple[str, dict[str, Any]]:
        del timeout
        self.get_calls += 1
        if self.item is None:
            raise queue.Empty
        if self.get_calls > 1:
            raise AssertionError("terminal result queue must be consumed exactly once")
        return self.item

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            error = self.close_error
            self.close_error = None
            raise error
        self.closed = True


class StubContext:
    def __init__(
        self,
        start_errors: list[Exception] | None = None,
        queue_close_errors: list[Exception] | None = None,
        start_alive_on_errors: list[bool] | None = None,
        terminate_errors: list[Exception] | None = None,
    ) -> None:
        self.processes: list[StubProcess] = []
        self.queues: list[StubResultQueue] = []
        self.start_errors = [] if start_errors is None else list(start_errors)
        self.start_alive_on_errors = (
            [] if start_alive_on_errors is None else list(start_alive_on_errors)
        )
        self.terminate_errors = [] if terminate_errors is None else list(terminate_errors)
        self.queue_close_errors = (
            [] if queue_close_errors is None else list(queue_close_errors)
        )

    def Queue(self, *, maxsize: int) -> StubResultQueue:
        self.assert_queue_size(maxsize)
        close_error = self.queue_close_errors.pop(0) if self.queue_close_errors else None
        result_queue = StubResultQueue(close_error=close_error)
        self.queues.append(result_queue)
        return result_queue

    @staticmethod
    def assert_queue_size(maxsize: int) -> None:
        if maxsize != 1:
            raise AssertionError(f"unexpected result queue size: {maxsize}")

    def Process(self, **_kwargs: Any) -> StubProcess:
        error = self.start_errors.pop(0) if self.start_errors else None
        alive_on_error = self.start_alive_on_errors.pop(0) if self.start_alive_on_errors else False
        terminate_error = self.terminate_errors.pop(0) if self.terminate_errors else None
        process = StubProcess(
            alive=False,
            start_error=error,
            start_alive_on_error=alive_on_error,
            terminate_error=terminate_error,
        )
        self.processes.append(process)
        return process


class StubSupervisorThread:
    def __init__(self) -> None:
        self.join_timeouts: list[float | None] = []

    def join(self, timeout: float | None = None) -> None:
        self.join_timeouts.append(timeout)

    def is_alive(self) -> bool:
        return len(self.join_timeouts) < 2


class FlakyRepository:
    def __init__(self) -> None:
        self.root = Path(".")
        self.succeeded_attempts: list[tuple[str, dict[str, Any]]] = []
        self.failed_attempts: list[tuple[str, str, str]] = []
        self.running_attempts: list[str] = []
        self.fail_succeeded_once_for: set[str] = set()
        self.always_fail_succeeded_for: set[str] = set()
        self.fail_failed_once_for: set[str] = set()
        self.always_fail_failed_for: set[str] = set()
        self.fail_running_once_for: set[str] = set()

    def mark_succeeded(self, run_id: str, payload: dict[str, Any]) -> None:
        self.succeeded_attempts.append((run_id, payload))
        if run_id in self.always_fail_succeeded_for:
            raise OSError("synthetic persistent manifest write failure")
        if run_id in self.fail_succeeded_once_for:
            self.fail_succeeded_once_for.remove(run_id)
            raise OSError("synthetic manifest write failure")

    def mark_failed(self, run_id: str, code: str, message: str) -> None:
        self.failed_attempts.append((run_id, code, message))
        if run_id in self.always_fail_failed_for:
            raise OSError("synthetic persistent manifest write failure")
        if run_id in self.fail_failed_once_for:
            self.fail_failed_once_for.remove(run_id)
            raise OSError("synthetic manifest write failure")

    def mark_running(self, run_id: str) -> None:
        self.running_attempts.append(run_id)
        if run_id in self.fail_running_once_for:
            self.fail_running_once_for.remove(run_id)
            raise OSError("synthetic manifest write failure")


class JobManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def manager_with_active_jobs(
        repository: FlakyRepository,
        jobs: dict[str, _ActiveJob],
    ) -> JobManager:
        manager = JobManager.__new__(JobManager)
        manager.repository = repository  # type: ignore[assignment]
        manager.runner = successful_runner
        manager.timeout_seconds = 3
        manager._lock = threading.RLock()
        manager._submission_lock = threading.Lock()
        manager._active = jobs
        manager._pending = queue.Queue(maxsize=4)
        manager._start_failures = 0
        manager._next_start_attempt_at = 0.0
        return manager

    def test_terminal_success_is_cached_until_persisted_while_other_job_finishes(self) -> None:
        first_payload = {"summary": {"job": "first"}}
        second_payload = {"summary": {"job": "second"}}
        first_queue = StubResultQueue(("succeeded", first_payload))
        second_queue = StubResultQueue(("succeeded", second_payload))
        repository = FlakyRepository()
        repository.fail_succeeded_once_for.add("first")
        manager = self.manager_with_active_jobs(
            repository,
            {
                "first": _ActiveJob(
                    process=StubProcess(alive=False),
                    result_queue=first_queue,
                    started_at=time.monotonic(),
                ),
                "second": _ActiveJob(
                    process=StubProcess(alive=False),
                    result_queue=second_queue,
                    started_at=time.monotonic(),
                ),
            },
        )

        manager._finish_active()

        self.assertEqual(set(manager._active), {"first"})
        self.assertEqual(first_queue.get_calls, 1)
        self.assertTrue(first_queue.closed)
        self.assertEqual(first_queue.close_calls, 1)
        self.assertEqual(second_queue.get_calls, 1)
        self.assertTrue(second_queue.closed)
        self.assertEqual(
            repository.succeeded_attempts,
            [("first", first_payload), ("second", second_payload)],
        )

        manager._finish_active()
        self.assertEqual(len(repository.succeeded_attempts), 2, "retry must be throttled")
        manager._active["first"].next_persistence_attempt_at = 0.0
        manager._finish_active()

        self.assertEqual(manager.active_count, 0)
        self.assertEqual(first_queue.get_calls, 1)
        self.assertTrue(first_queue.closed)
        self.assertEqual(first_queue.close_calls, 1)
        self.assertEqual(repository.succeeded_attempts[-1], ("first", first_payload))

    def test_persistence_outage_keeps_worker_slot_until_cached_result_is_durable(self) -> None:
        payload = {"summary": {"job": "blocked"}}
        result_queue = StubResultQueue(("succeeded", payload))
        repository = FlakyRepository()
        repository.always_fail_succeeded_for.add("blocked")
        manager = self.manager_with_active_jobs(
            repository,
            {
                "blocked": _ActiveJob(
                    process=StubProcess(alive=False),
                    result_queue=result_queue,
                    started_at=time.monotonic(),
                )
            },
        )
        manager._stop = threading.Event()
        manager.max_workers = 1
        manager._context = StubContext()  # type: ignore[assignment]
        manager._pending.put_nowait(("next", {"smooth_window": 3}))

        manager._finish_active()
        manager._start_pending()

        self.assertEqual(manager.active_count, 1)
        self.assertEqual(manager._pending.qsize(), 1)
        self.assertEqual(result_queue.get_calls, 1)
        self.assertTrue(result_queue.closed)
        self.assertEqual(result_queue.close_calls, 1)

        manager._active["blocked"].next_persistence_attempt_at = 0.0
        manager._finish_active()
        manager._start_pending()
        self.assertEqual(manager.active_count, 1)
        self.assertEqual(manager._pending.qsize(), 1)
        self.assertEqual(result_queue.get_calls, 1)

        repository.always_fail_succeeded_for.clear()
        manager._active["blocked"].next_persistence_attempt_at = 0.0
        manager._finish_active()
        manager._start_pending()

        self.assertEqual(set(manager._active), {"next"})
        self.assertEqual(manager._pending.qsize(), 0)
        self.assertEqual(result_queue.get_calls, 1)
        self.assertTrue(result_queue.closed)
        blocked_attempts = [
            attempt_payload
            for run_id, attempt_payload in repository.succeeded_attempts
            if run_id == "blocked"
        ]
        self.assertEqual(blocked_attempts, [payload, payload, payload])

    def test_result_queue_close_failure_does_not_hold_a_durable_job_slot(self) -> None:
        first_payload = {"summary": {"job": "first"}}
        second_payload = {"summary": {"job": "second"}}
        first_queue = StubResultQueue(
            ("succeeded", first_payload),
            close_error=OSError("synthetic queue close failure"),
        )
        second_queue = StubResultQueue(("succeeded", second_payload))
        repository = FlakyRepository()
        manager = self.manager_with_active_jobs(
            repository,
            {
                "first": _ActiveJob(
                    process=StubProcess(alive=False),
                    result_queue=first_queue,
                    started_at=time.monotonic(),
                ),
                "second": _ActiveJob(
                    process=StubProcess(alive=False),
                    result_queue=second_queue,
                    started_at=time.monotonic(),
                ),
            },
        )

        manager._finish_active()

        self.assertEqual(manager.active_count, 0)
        self.assertTrue(second_queue.closed)
        self.assertEqual(
            repository.succeeded_attempts,
            [("first", first_payload), ("second", second_payload)],
        )

        manager._finish_active()

        self.assertEqual(manager.active_count, 0)
        self.assertFalse(first_queue.closed)
        self.assertEqual(first_queue.close_calls, 1)
        self.assertEqual(len(repository.succeeded_attempts), 2)

    def test_timeout_outcome_is_cached_until_exact_error_is_persisted(self) -> None:
        result_queue = StubResultQueue()
        process = StubProcess(alive=True)
        repository = FlakyRepository()
        repository.fail_failed_once_for.add("timed-out")
        manager = self.manager_with_active_jobs(
            repository,
            {
                "timed-out": _ActiveJob(
                    process=process,
                    result_queue=result_queue,
                    started_at=time.monotonic() - 10,
                )
            },
        )

        manager._finish_active()

        self.assertEqual(set(manager._active), {"timed-out"})
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(result_queue.get_calls, 0)
        self.assertTrue(result_queue.closed)
        self.assertEqual(
            repository.failed_attempts,
            [
                (
                    "timed-out",
                    "analysis_timeout",
                    "Analysis exceeded the configured time limit.",
                )
            ],
        )

        manager._active["timed-out"].next_persistence_attempt_at = 0.0
        manager._finish_active()

        self.assertEqual(manager.active_count, 0)
        self.assertEqual(process.terminate_calls, 1)
        self.assertTrue(result_queue.closed)
        self.assertEqual(result_queue.close_calls, 1)
        self.assertEqual(repository.failed_attempts[0], repository.failed_attempts[1])

    def test_mark_running_failure_restores_queue_before_submission_lock_is_released(self) -> None:
        repository = FlakyRepository()
        manager = self.manager_with_active_jobs(repository, {})
        manager._stop = threading.Event()
        manager.max_workers = 1
        manager._context = StubContext()  # type: ignore[assignment]
        manager._pending = queue.Queue(maxsize=1)
        manager._pending.put_nowait(("queued", {"smooth_window": 3}))
        entered = threading.Event()
        release = threading.Event()
        fail_once = True

        def blocking_mark_running(run_id: str) -> None:
            nonlocal fail_once
            repository.running_attempts.append(run_id)
            if fail_once:
                fail_once = False
                entered.set()
                if not release.wait(timeout=2):
                    raise AssertionError("test did not release mark_running")
                raise OSError("synthetic manifest write failure")

        repository.mark_running = blocking_mark_running  # type: ignore[method-assign]
        starter = threading.Thread(target=manager._start_pending)
        starter.start()
        self.assertTrue(entered.wait(timeout=2))
        submission_lock_acquired = manager._submission_lock.acquire(blocking=False)
        if submission_lock_acquired:
            manager._submission_lock.release()
        self.assertFalse(
            submission_lock_acquired,
            "submitters must not observe the temporarily vacant queue slot",
        )
        release.set()
        starter.join(timeout=2)
        self.assertFalse(starter.is_alive())

        self.assertEqual(manager.active_count, 0)
        self.assertEqual(manager._pending.qsize(), 1)
        self.assertEqual(manager._pending.unfinished_tasks, 1)
        self.assertEqual(repository.running_attempts, ["queued"])
        with self.assertRaises(queue.Full):
            manager._pending.put_nowait(("excess", {}))

        manager._start_pending()
        self.assertEqual(repository.running_attempts, ["queued"], "retry must be throttled")
        manager._next_start_attempt_at = 0.0
        manager._start_pending()

        self.assertEqual(repository.running_attempts, ["queued", "queued"])
        self.assertEqual(set(manager._active), {"queued"})
        self.assertEqual(manager._pending.unfinished_tasks, 0)
        self.assertTrue(manager._active["queued"].process.started)

    def test_process_start_oserror_restores_same_job_and_retries_once(self) -> None:
        repository = FlakyRepository()
        manager = self.manager_with_active_jobs(repository, {})
        manager._stop = threading.Event()
        manager.max_workers = 1
        context = StubContext(
            [OSError("synthetic spawn failure")],
            [OSError("synthetic queue close failure")],
        )
        manager._context = context  # type: ignore[assignment]
        manager._pending = queue.Queue(maxsize=1)
        manager._pending.put_nowait(("queued", {"smooth_window": 3}))

        manager._start_pending()

        self.assertEqual(manager.active_count, 0)
        self.assertEqual(manager._pending.qsize(), 1)
        self.assertEqual(manager._pending.unfinished_tasks, 1)
        self.assertEqual(repository.running_attempts, ["queued"])
        self.assertEqual(context.queues[0].close_calls, 1)
        self.assertFalse(context.queues[0].closed)
        with self.assertRaises(queue.Full):
            manager._pending.put_nowait(("excess", {}))

        manager._start_pending()
        self.assertEqual(len(context.processes), 1, "retry must be throttled")
        manager._next_start_attempt_at = 0.0
        manager._start_pending()

        self.assertEqual(repository.running_attempts, ["queued", "queued"])
        self.assertEqual(set(manager._active), {"queued"})
        self.assertEqual(manager._pending.unfinished_tasks, 0)
        self.assertEqual(len(context.processes), 2)
        self.assertTrue(context.processes[1].started)

    def test_unexpected_process_start_error_restores_job_before_propagating(self) -> None:
        repository = FlakyRepository()
        manager = self.manager_with_active_jobs(repository, {})
        manager._stop = threading.Event()
        manager.max_workers = 1
        context = StubContext([RuntimeError("synthetic programmer error")])
        manager._context = context  # type: ignore[assignment]
        manager._pending = queue.Queue(maxsize=1)
        manager._pending.put_nowait(("queued", {"smooth_window": 3}))

        with self.assertRaisesRegex(RuntimeError, "synthetic programmer error"):
            manager._start_pending()

        self.assertEqual(manager.active_count, 0)
        self.assertEqual(manager._pending.qsize(), 1)
        self.assertEqual(manager._pending.unfinished_tasks, 1)
        self.assertTrue(context.queues[0].closed)

    def test_uncertain_partially_started_process_is_tracked_instead_of_requeued(self) -> None:
        repository = FlakyRepository()
        manager = self.manager_with_active_jobs(repository, {})
        manager._stop = threading.Event()
        manager.max_workers = 1
        context = StubContext(
            [OSError("synthetic post-spawn failure")],
            start_alive_on_errors=[True],
            terminate_errors=[OSError("synthetic terminate failure")],
        )
        manager._context = context  # type: ignore[assignment]
        manager._pending = queue.Queue(maxsize=1)
        manager._pending.put_nowait(("queued", {"smooth_window": 3}))

        with self.assertRaisesRegex(OSError, "synthetic post-spawn failure"):
            manager._start_pending()

        self.assertEqual(set(manager._active), {"queued"})
        self.assertEqual(manager._pending.qsize(), 0)
        self.assertEqual(manager._pending.unfinished_tasks, 0)
        self.assertTrue(context.processes[0].is_alive())
        self.assertFalse(context.queues[0].closed)

    def test_shutdown_persistence_failure_does_not_skip_other_active_jobs(self) -> None:
        first_queue = StubResultQueue()
        second_queue = StubResultQueue()
        first_process = StubProcess(alive=True)
        second_process = StubProcess(alive=True)
        repository = FlakyRepository()
        repository.fail_failed_once_for.update({"first", "pending-first"})
        manager = self.manager_with_active_jobs(
            repository,
            {
                "first": _ActiveJob(
                    process=first_process,
                    result_queue=first_queue,
                    started_at=time.monotonic(),
                ),
                "second": _ActiveJob(
                    process=second_process,
                    result_queue=second_queue,
                    started_at=time.monotonic(),
                ),
            },
        )
        manager._active["first"].next_persistence_attempt_at = time.monotonic() + 60
        manager._pending.put_nowait(("pending-first", {"smooth_window": 3}))
        manager._pending.put_nowait(("pending-second", {"smooth_window": 3}))

        manager._shutdown_jobs()

        self.assertEqual(manager.active_count, 0)
        self.assertEqual(first_process.terminate_calls, 1)
        self.assertEqual(second_process.terminate_calls, 1)
        self.assertTrue(first_queue.closed)
        self.assertTrue(second_queue.closed)
        self.assertEqual(
            [attempt[0] for attempt in repository.failed_attempts],
            [
                "first",
                "second",
                "pending-first",
                "pending-second",
                "first",
                "pending-first",
            ],
        )
        self.assertEqual(repository.failed_attempts[0], repository.failed_attempts[-2])
        self.assertEqual(repository.failed_attempts[2], repository.failed_attempts[-1])
        self.assertEqual(manager._pending.qsize(), 0)
        self.assertEqual(manager._pending.unfinished_tasks, 0)

    def test_shutdown_preserves_completed_result_waiting_in_worker_queue(self) -> None:
        payload = {"summary": {"job": "completed-before-close"}}
        result_queue = StubResultQueue(("succeeded", payload))
        repository = FlakyRepository()
        manager = self.manager_with_active_jobs(
            repository,
            {
                "completed": _ActiveJob(
                    process=StubProcess(alive=False),
                    result_queue=result_queue,
                    started_at=time.monotonic(),
                )
            },
        )

        manager._shutdown_jobs()

        self.assertEqual(manager.active_count, 0)
        self.assertEqual(repository.succeeded_attempts, [("completed", payload)])
        self.assertEqual(repository.failed_attempts, [])
        self.assertEqual(result_queue.get_calls, 1)
        self.assertTrue(result_queue.closed)

    def test_shutdown_persistence_budget_starts_after_slow_process_capture(self) -> None:
        repository = FlakyRepository()
        repository.fail_failed_once_for.add("active")
        manager = self.manager_with_active_jobs(
            repository,
            {
                "active": _ActiveJob(
                    process=StubProcess(alive=True),
                    result_queue=StubResultQueue(),
                    started_at=0.0,
                )
            },
        )
        clock = [0.0]
        original_capture = manager._capture_shutdown_outcome
        first_capture = True

        def slow_first_capture(run_id: str, active: _ActiveJob) -> bool:
            nonlocal first_capture
            captured = original_capture(run_id, active)
            if first_capture:
                first_capture = False
                clock[0] += 10.0
            return captured

        def advance_clock(seconds: float) -> None:
            clock[0] += seconds

        manager._capture_shutdown_outcome = slow_first_capture  # type: ignore[method-assign]
        with (
            patch("stepwise.jobs.time.monotonic", side_effect=lambda: clock[0]),
            patch("stepwise.jobs.time.sleep", side_effect=advance_clock),
        ):
            manager._shutdown_jobs()

        self.assertEqual(manager.active_count, 0)
        self.assertEqual([item[0] for item in repository.failed_attempts], ["active", "active"])

    def test_shutdown_stop_failure_is_retried_without_skipping_other_job(self) -> None:
        first_process = StubProcess(
            alive=True,
            terminate_error=OSError("synthetic terminate failure"),
        )
        second_process = StubProcess(alive=True)
        repository = FlakyRepository()
        manager = self.manager_with_active_jobs(
            repository,
            {
                "first": _ActiveJob(
                    process=first_process,
                    result_queue=StubResultQueue(),
                    started_at=time.monotonic(),
                ),
                "second": _ActiveJob(
                    process=second_process,
                    result_queue=StubResultQueue(),
                    started_at=time.monotonic(),
                ),
            },
        )

        manager._shutdown_jobs()

        self.assertEqual(manager.active_count, 0)
        self.assertEqual(first_process.terminate_calls, 2)
        self.assertEqual(second_process.terminate_calls, 1)
        self.assertEqual(
            {run_id for run_id, _code, _message in repository.failed_attempts},
            {"first", "second"},
        )

    def test_shutdown_timeout_keeps_unpersisted_active_and_pending_bookkeeping(self) -> None:
        active_queue = StubResultQueue()
        repository = FlakyRepository()
        repository.always_fail_failed_for.update({"active", "pending"})
        manager = self.manager_with_active_jobs(
            repository,
            {
                "active": _ActiveJob(
                    process=StubProcess(alive=True),
                    result_queue=active_queue,
                    started_at=time.monotonic(),
                )
            },
        )
        manager._pending = queue.Queue(maxsize=1)
        manager._pending.put_nowait(("pending", {"smooth_window": 3}))

        with patch("stepwise.jobs._SHUTDOWN_RETRY_TIMEOUT_SECONDS", 0.0):
            manager._shutdown_jobs()

        self.assertEqual(set(manager._active), {"active"})
        self.assertTrue(active_queue.closed)
        self.assertEqual(manager._pending.qsize(), 1)
        self.assertEqual(manager._pending.unfinished_tasks, 1)
        self.assertEqual(
            {attempt[0] for attempt in repository.failed_attempts},
            {"active", "pending"},
        )

    def test_supervisor_exception_still_runs_shutdown_cleanup(self) -> None:
        repository = FlakyRepository()
        second_process = StubProcess(alive=True)
        manager = self.manager_with_active_jobs(
            repository,
            {
                "malformed": _ActiveJob(
                    process=StubProcess(alive=False),
                    result_queue=StubResultQueue(),
                    started_at=time.monotonic(),
                    terminal_outcome=("failed", {"message": "missing code"}),
                ),
                "second": _ActiveJob(
                    process=second_process,
                    result_queue=StubResultQueue(),
                    started_at=time.monotonic(),
                ),
            },
        )
        manager._stop = threading.Event()
        manager._pending.put_nowait(("pending", {"smooth_window": 3}))

        def fail_finish() -> None:
            raise RuntimeError("synthetic supervisor failure")

        manager._finish_active = fail_finish  # type: ignore[method-assign]
        manager._start_pending = lambda: None  # type: ignore[method-assign]

        with (
            patch("stepwise.jobs._SHUTDOWN_RETRY_TIMEOUT_SECONDS", 0.0),
            self.assertRaisesRegex(RuntimeError, "synthetic supervisor failure"),
        ):
            manager._supervise()

        self.assertEqual(set(manager._active), {"malformed"})
        self.assertEqual(second_process.terminate_calls, 1)
        self.assertEqual(manager._pending.qsize(), 0)
        self.assertEqual(
            {run_id for run_id, _code, _message in repository.failed_attempts},
            {"second", "pending"},
        )

    def test_close_can_wait_again_after_an_incomplete_first_join(self) -> None:
        manager = JobManager.__new__(JobManager)
        manager._stop = threading.Event()
        manager.max_workers = 2
        thread = StubSupervisorThread()
        manager._thread = thread  # type: ignore[assignment]

        manager.close()
        manager.close()

        self.assertTrue(manager._stop.is_set())
        self.assertEqual(len(thread.join_timeouts), 2)
        self.assertTrue(all(timeout is not None and timeout > 5 for timeout in thread.join_timeouts))

    def test_supervisor_survives_transient_terminal_manifest_failure(self) -> None:
        manager = JobManager(
            self.root,
            max_workers=1,
            max_queue=2,
            timeout_seconds=3,
            runner=successful_runner,
        )
        original_mark_succeeded = manager.repository.mark_succeeded
        attempts: list[tuple[str, dict[str, Any]]] = []
        failed_once = False
        persisted = threading.Event()

        def flaky_mark_succeeded(run_id: str, payload: dict[str, Any]) -> None:
            nonlocal failed_once
            attempts.append((run_id, json.loads(json.dumps(payload))))
            if not failed_once:
                failed_once = True
                raise PermissionError("synthetic manifest replacement conflict")
            original_mark_succeeded(run_id, payload)
            persisted.set()

        manager.repository.mark_succeeded = flaky_mark_succeeded  # type: ignore[method-assign]
        try:
            first = manager.submit(FIXTURE_BYTES, None, AnalysisConfig())
            self.assertTrue(persisted.wait(timeout=8))
            first_terminal = manager.repository.get(first.run_id)
            persisted.clear()
            second = manager.submit(FIXTURE_BYTES, None, AnalysisConfig())
            self.assertTrue(persisted.wait(timeout=8))
            second_terminal = manager.repository.get(second.run_id)
            self.assertTrue(manager._thread.is_alive())
        finally:
            manager.close()

        self.assertEqual(first_terminal.status, "succeeded")
        self.assertEqual(second_terminal.status, "succeeded")
        first_attempts = [payload for run_id, payload in attempts if run_id == first.run_id]
        self.assertEqual(len(first_attempts), 2)
        self.assertEqual(first_attempts[0], first_attempts[1])

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
            run_directories = [
                path for path in self.root.iterdir() if path.is_dir() and path.name != ".staging"
            ]
            self.assertEqual(len(run_directories), 2)
            self.assertEqual(list(manager.repository.staging_dir.iterdir()), [])
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

    def test_submit_cleans_expired_terminal_results(self) -> None:
        manager = JobManager(
            self.root,
            max_workers=1,
            max_queue=1,
            timeout_seconds=3,
            result_ttl_hours=24,
            runner=successful_runner,
        )
        try:
            expired = manager.repository.create()
            old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
            manager.repository.save(
                JobManifest(
                    run_id=expired.run_id,
                    status="failed",
                    created_at=old,
                    updated_at=old,
                    error_code="test",
                    error_message="expired",
                )
            )
            manager.submit(FIXTURE_BYTES, None, AnalysisConfig())
            self.assertFalse((self.root / expired.run_id).exists())
        finally:
            manager.close()

    def test_manager_startup_removes_stale_staging_files(self) -> None:
        repository = JobRepository(self.root)
        stale = repository.new_staging_path()
        stale.write_bytes(b"partial upload")

        manager = JobManager(self.root, runner=successful_runner)
        try:
            self.assertFalse(stale.exists())
            self.assertEqual(list(manager.repository.staging_dir.iterdir()), [])
        finally:
            manager.close()


if __name__ == "__main__":
    unittest.main()
