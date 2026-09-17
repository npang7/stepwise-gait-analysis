"""Bounded asynchronous job supervision with killable worker processes."""

from __future__ import annotations

import logging
import multiprocessing
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Self

from .models import AnalysisConfig, JobManifest, SensorMapping
from .parsing import InputValidationError, validate_stepwise_path
from .service import AnalysisService
from .storage import JobRepository

LOGGER = logging.getLogger("stepwise.jobs")
Runner = Callable[[str, str, dict[str, Any]], dict[str, Any]]
TerminalOutcome = tuple[Literal["succeeded", "failed"], dict[str, Any]]

_REPOSITORY_RETRY_BASE_SECONDS = 0.1
_REPOSITORY_RETRY_MAX_SECONDS = 2.0
_SHUTDOWN_RETRY_TIMEOUT_SECONDS = 2.0
_PROCESS_TERMINATE_WAIT_SECONDS = 2.0
_PROCESS_KILL_WAIT_SECONDS = 2.0
_RESULT_QUEUE_WAIT_SECONDS = 0.5
_SHUTDOWN_JOIN_MARGIN_SECONDS = 1.0


class QueueFullError(RuntimeError):
    pass


class SubmissionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _config_from_dict(payload: dict[str, Any]) -> AnalysisConfig:
    values = dict(payload)
    mapping_payload = values.pop("sensor_mapping", {})
    return AnalysisConfig(sensor_mapping=SensorMapping(**mapping_payload), **values)


def _run_analysis(root: str, run_id: str, config_payload: dict[str, Any]) -> dict[str, Any]:
    repository = JobRepository(root)
    walking_path = repository.input_dir(run_id) / "walking.txt"
    standing_candidate = repository.input_dir(run_id) / "standing.txt"
    standing_path = standing_candidate if standing_candidate.is_file() else None
    result = AnalysisService().analyze(
        walking_path,
        standing_path,
        repository.artifact_dir(run_id),
        _config_from_dict(config_payload),
    )
    return result.to_dict()


def _worker_entry(
    root: str,
    run_id: str,
    config_payload: dict[str, Any],
    runner: Runner,
    result_queue: Any,
) -> None:
    try:
        result_queue.put(("succeeded", runner(root, run_id, config_payload)))
    except InputValidationError as exc:
        result_queue.put(("failed", {"code": exc.code, "message": str(exc)}))
    except Exception as exc:  # noqa: BLE001 -- worker boundary returns a stable error model.
        LOGGER.error(
            "analysis_worker_failed",
            extra={"run_id": run_id, "error_type": type(exc).__name__},
        )
        result_queue.put(
            (
                "failed",
                {"code": "analysis_failed", "message": "Analysis failed unexpectedly."},
            )
        )


@dataclass
class _ActiveJob:
    process: Any
    result_queue: Any
    started_at: float
    # Once captured, this exact payload remains in the active slot until its manifest is durable.
    terminal_outcome: TerminalOutcome | None = None
    result_queue_closed: bool = False
    persistence_failures: int = 0
    next_persistence_attempt_at: float = 0.0


@dataclass
class _PendingShutdownJob:
    run_id: str
    config_payload: dict[str, Any]
    persistence_failures: int = 0
    next_persistence_attempt_at: float = 0.0


class JobManager:
    """Supervise a bounded queue of analyses in isolated child processes."""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        max_workers: int = 2,
        max_queue: int = 8,
        timeout_seconds: float = 120.0,
        max_upload_bytes: int = 64 * 1024 * 1024,
        result_ttl_hours: float = 24.0,
        runner: Runner = _run_analysis,
    ) -> None:
        if max_workers <= 0 or max_queue <= 0 or timeout_seconds <= 0:
            raise ValueError("worker, queue, and timeout limits must be positive")
        self.repository = JobRepository(data_dir)
        self.max_workers = max_workers
        self.timeout_seconds = timeout_seconds
        self.max_upload_bytes = max_upload_bytes
        self.result_ttl_hours = result_ttl_hours
        self.runner = runner
        self._context = multiprocessing.get_context("spawn")
        self._pending: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue(maxsize=max_queue)
        self._start_failures = 0
        self._next_start_attempt_at = 0.0
        self._active: dict[str, _ActiveJob] = {}
        self._lock = threading.RLock()
        self._submission_lock = threading.Lock()
        self._stop = threading.Event()
        self.repository.cleanup_staging()
        self.repository.recover_incomplete()
        self.repository.cleanup_expired(result_ttl_hours)
        self._thread = threading.Thread(
            target=self._supervise,
            name="stepwise-job-supervisor",
            daemon=True,
        )
        self._thread.start()

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def _validate_upload(self, path: Path) -> None:
        if path.stat().st_size > self.max_upload_bytes:
            raise SubmissionError(
                "upload_too_large",
                f"each recording must be at most {self.max_upload_bytes} bytes",
            )
        try:
            validate_stepwise_path(path)
        except InputValidationError as exc:
            raise SubmissionError(exc.code, str(exc)) from exc

    def submit_staged(
        self,
        walking: Path,
        standing: Path | None,
        config: AnalysisConfig,
    ) -> JobManifest:
        self._validate_upload(walking)
        if standing is not None:
            self._validate_upload(standing)
        with self._submission_lock:
            self.repository.cleanup_expired(self.result_ttl_hours)
            manifest = self.repository.create()
            try:
                self.repository.move_staged_input(manifest.run_id, "walking.txt", walking)
                if standing is not None:
                    self.repository.move_staged_input(manifest.run_id, "standing.txt", standing)
                self._pending.put_nowait((manifest.run_id, asdict(config)))
            except queue.Full as exc:
                self.repository.delete_run(manifest.run_id)
                raise QueueFullError("analysis queue is full") from exc
            except Exception:
                self.repository.delete_run(manifest.run_id)
                raise
            return manifest

    def submit(
        self,
        walking: bytes,
        standing: bytes | None,
        config: AnalysisConfig,
    ) -> JobManifest:
        walking_path = self.repository.new_staging_path()
        standing_path = self.repository.new_staging_path() if standing is not None else None
        try:
            walking_path.write_bytes(walking)
            if standing_path is not None and standing is not None:
                standing_path.write_bytes(standing)
            return self.submit_staged(walking_path, standing_path, config)
        finally:
            self.repository.remove_staged(walking_path)
            if standing_path is not None:
                self.repository.remove_staged(standing_path)

    def _start_pending(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                if len(self._active) >= self.max_workers:
                    return
            if time.monotonic() < self._next_start_attempt_at:
                return

            # Submission and dequeue/start share this lock so a failed start can put the
            # accepted item back before a submitter observes spare queue capacity.
            with self._submission_lock:
                if self._stop.is_set():
                    return
                with self._lock:
                    if len(self._active) >= self.max_workers:
                        return
                try:
                    run_id, config_payload = self._pending.get_nowait()
                except queue.Empty:
                    return
                result_queue: Any | None = None
                process: Any | None = None
                try:
                    self.repository.mark_running(run_id)
                    result_queue = self._context.Queue(maxsize=1)
                    process = self._context.Process(
                        target=_worker_entry,
                        args=(
                            str(self.repository.root),
                            run_id,
                            config_payload,
                            self.runner,
                            result_queue,
                        ),
                        name=f"stepwise-{run_id[:8]}",
                    )
                    process.start()
                except Exception as exc:
                    process_may_be_alive = False
                    if process is not None:
                        try:
                            if process.is_alive():
                                self._stop_process(
                                    _ActiveJob(
                                        process=process,
                                        result_queue=result_queue,
                                        started_at=time.monotonic(),
                                    )
                                )
                        except Exception:  # noqa: BLE001 -- preserve the accepted job.
                            LOGGER.warning(
                                "job_start_cleanup_failed",
                                extra={"run_id": run_id},
                            )
                        try:
                            process_may_be_alive = process.is_alive()
                        except Exception:  # noqa: BLE001 -- an unknown state is not safe to retry.
                            process_may_be_alive = True
                    if process_may_be_alive:
                        # Requeueing could start a second worker for the same run. Keep the
                        # uncertain child under supervisor ownership and fail the loop instead.
                        with self._lock:
                            self._active[run_id] = _ActiveJob(
                                process=process,
                                result_queue=result_queue,
                                started_at=time.monotonic(),
                            )
                        self._pending.task_done()
                        raise
                    if result_queue is not None:
                        try:
                            result_queue.close()
                        except Exception:  # noqa: BLE001 -- never mask the start failure.
                            LOGGER.warning(
                                "job_start_queue_close_failed",
                                extra={"run_id": run_id},
                            )
                    self._pending.put_nowait((run_id, config_payload))
                    self._pending.task_done()
                    if isinstance(exc, OSError):
                        self._start_failures += 1
                        self._next_start_attempt_at = (
                            time.monotonic()
                            + self._repository_retry_delay(self._start_failures)
                        )
                        LOGGER.warning(
                            "job_start_retry",
                            extra={"run_id": run_id, "error_type": type(exc).__name__},
                        )
                        return
                    raise
                with self._lock:
                    self._active[run_id] = _ActiveJob(
                        process=process,
                        result_queue=result_queue,
                        started_at=time.monotonic(),
                    )
                self._start_failures = 0
                self._next_start_attempt_at = 0.0
                self._pending.task_done()

    @staticmethod
    def _stop_process(active: _ActiveJob) -> None:
        if active.process.is_alive():
            active.process.terminate()
        active.process.join(timeout=_PROCESS_TERMINATE_WAIT_SECONDS)
        if active.process.is_alive() and hasattr(active.process, "kill"):
            active.process.kill()
            active.process.join(timeout=_PROCESS_KILL_WAIT_SECONDS)

    @staticmethod
    def _repository_retry_delay(failures: int) -> float:
        exponent = min(max(failures - 1, 0), 5)
        return min(
            _REPOSITORY_RETRY_BASE_SECONDS * (2**exponent),
            _REPOSITORY_RETRY_MAX_SECONDS,
        )

    def _capture_terminal_outcome(self, active: _ActiveJob) -> bool:
        if active.terminal_outcome is not None:
            return True

        elapsed = time.monotonic() - active.started_at
        if active.process.is_alive() and elapsed <= self.timeout_seconds:
            return False
        if active.process.is_alive():
            self._stop_process(active)
            active.terminal_outcome = (
                "failed",
                {
                    "code": "analysis_timeout",
                    "message": "Analysis exceeded the configured time limit.",
                },
            )
            return True

        active.process.join(timeout=1)
        try:
            outcome, payload = active.result_queue.get(timeout=_RESULT_QUEUE_WAIT_SECONDS)
        except queue.Empty:
            outcome, payload = (
                "failed",
                {"code": "worker_crashed", "message": "Analysis worker exited unexpectedly."},
            )
        active.terminal_outcome = (outcome, payload)
        return True

    def _persist_terminal_outcome(self, run_id: str, active: _ActiveJob) -> bool:
        terminal_outcome = active.terminal_outcome
        if terminal_outcome is None:
            raise RuntimeError("terminal outcome has not been captured")
        outcome, payload = terminal_outcome
        try:
            if outcome == "succeeded":
                self.repository.mark_succeeded(run_id, payload)
            else:
                self.repository.mark_failed(run_id, payload["code"], payload["message"])
        except OSError as exc:
            active.persistence_failures += 1
            active.next_persistence_attempt_at = (
                time.monotonic() + self._repository_retry_delay(active.persistence_failures)
            )
            LOGGER.warning(
                "job_terminal_manifest_persist_retry",
                extra={"run_id": run_id, "error_type": type(exc).__name__},
            )
            return False
        return True

    def _close_result_queue(self, run_id: str, active: _ActiveJob) -> None:
        if active.result_queue_closed:
            return
        try:
            active.result_queue.close()
        except (OSError, ValueError) as exc:
            LOGGER.warning(
                "job_result_queue_close_failed",
                extra={"run_id": run_id, "error_type": type(exc).__name__},
            )
        finally:
            # The result is already copied into terminal_outcome. A local queue cleanup
            # failure must not consume worker capacity after the manifest is durable.
            active.result_queue_closed = True

    def _release_active(self, run_id: str, active: _ActiveJob) -> None:
        with self._lock:
            if self._active.get(run_id) is active:
                del self._active[run_id]

    def _finish_active(self) -> None:
        with self._lock:
            snapshot = list(self._active.items())
        for run_id, active in snapshot:
            if not self._capture_terminal_outcome(active):
                continue
            if time.monotonic() < active.next_persistence_attempt_at:
                continue
            self._close_result_queue(run_id, active)
            if self._persist_terminal_outcome(run_id, active):
                self._release_active(run_id, active)

    def _supervise(self) -> None:
        try:
            while not self._stop.is_set():
                self._finish_active()
                self._start_pending()
                self._stop.wait(0.02)
        finally:
            self._shutdown_jobs()

    def _capture_shutdown_outcome(self, run_id: str, active: _ActiveJob) -> bool:
        if active.terminal_outcome is not None:
            return True
        try:
            self._stop_process(active)
            try:
                outcome, payload = active.result_queue.get(timeout=_RESULT_QUEUE_WAIT_SECONDS)
            except queue.Empty:
                outcome, payload = (
                    "failed",
                    {
                        "code": "service_stopped",
                        "message": "Analysis was interrupted because the service stopped.",
                    },
                )
        except (AssertionError, OSError, ValueError) as exc:
            active.persistence_failures += 1
            active.next_persistence_attempt_at = (
                time.monotonic() + self._repository_retry_delay(active.persistence_failures)
            )
            LOGGER.warning(
                "job_shutdown_capture_retry",
                extra={"run_id": run_id, "error_type": type(exc).__name__},
            )
            return False
        active.terminal_outcome = (outcome, payload)
        return True

    def _shutdown_jobs(self) -> None:
        # Serialize with submission mutation so failed pending items can be restored to
        # the bounded queue while shutdown performs its finite persistence retries.
        with self._submission_lock:
            with self._lock:
                active_jobs = dict(self._active)
            for run_id, active in active_jobs.items():
                # Shutdown gets its own bounded retry window instead of inheriting a
                # backoff deadline that may already extend beyond that window.
                active.next_persistence_attempt_at = 0.0
                try:
                    self._capture_shutdown_outcome(run_id, active)
                except Exception as exc:  # noqa: BLE001 -- isolate cleanup between jobs.
                    active.next_persistence_attempt_at = float("inf")
                    LOGGER.error(
                        "job_shutdown_capture_failed",
                        extra={"run_id": run_id, "error_type": type(exc).__name__},
                    )

            pending_jobs: list[_PendingShutdownJob] = []
            while True:
                try:
                    run_id, config_payload = self._pending.get_nowait()
                except queue.Empty:
                    break
                pending_jobs.append(
                    _PendingShutdownJob(run_id=run_id, config_payload=config_payload)
                )
                self._pending.task_done()

            # Process termination and queue capture have their own bounded waits. Start
            # the persistence retry budget only after every active job got a first turn.
            deadline = time.monotonic() + _SHUTDOWN_RETRY_TIMEOUT_SECONDS
            while active_jobs or pending_jobs:
                now = time.monotonic()
                for run_id, active in list(active_jobs.items()):
                    if now < active.next_persistence_attempt_at:
                        continue
                    try:
                        if not self._capture_shutdown_outcome(run_id, active):
                            continue
                        self._close_result_queue(run_id, active)
                        if self._persist_terminal_outcome(run_id, active):
                            self._release_active(run_id, active)
                            del active_jobs[run_id]
                    except Exception as exc:  # noqa: BLE001 -- isolate cleanup between jobs.
                        active.next_persistence_attempt_at = float("inf")
                        LOGGER.error(
                            "job_shutdown_active_cleanup_failed",
                            extra={"run_id": run_id, "error_type": type(exc).__name__},
                        )

                for pending in list(pending_jobs):
                    if now < pending.next_persistence_attempt_at:
                        continue
                    try:
                        self.repository.mark_failed(
                            pending.run_id,
                            "service_stopped",
                            "Analysis was cancelled because the service stopped.",
                        )
                    except OSError as exc:
                        pending.persistence_failures += 1
                        pending.next_persistence_attempt_at = (
                            time.monotonic()
                            + self._repository_retry_delay(pending.persistence_failures)
                        )
                        LOGGER.warning(
                            "pending_job_manifest_persist_retry",
                            extra={
                                "run_id": pending.run_id,
                                "error_type": type(exc).__name__,
                            },
                        )
                    except Exception as exc:  # noqa: BLE001 -- isolate cleanup between jobs.
                        pending.next_persistence_attempt_at = float("inf")
                        LOGGER.error(
                            "job_shutdown_pending_cleanup_failed",
                            extra={
                                "run_id": pending.run_id,
                                "error_type": type(exc).__name__,
                            },
                        )
                    else:
                        pending_jobs.remove(pending)

                if not active_jobs and not pending_jobs:
                    break
                now = time.monotonic()
                if now >= deadline:
                    break
                next_attempts = [
                    active.next_persistence_attempt_at for active in active_jobs.values()
                ] + [pending.next_persistence_attempt_at for pending in pending_jobs]
                next_attempts = [value for value in next_attempts if value != float("inf")]
                if not next_attempts:
                    break
                next_attempt = min(next_attempts, default=deadline)
                time.sleep(min(max(next_attempt - now, 0.001), deadline - now))

            for pending in pending_jobs:
                self._pending.put_nowait((pending.run_id, pending.config_payload))
            if active_jobs or pending_jobs:
                LOGGER.error(
                    "job_shutdown_persistence_deferred",
                    extra={
                        "active_jobs": len(active_jobs),
                        "pending_jobs": len(pending_jobs),
                    },
                )

    def close(self) -> None:
        self._stop.set()
        per_worker_timeout = (
            _PROCESS_TERMINATE_WAIT_SECONDS
            + _PROCESS_KILL_WAIT_SECONDS
            + _RESULT_QUEUE_WAIT_SECONDS
        )
        timeout = (
            self.max_workers * per_worker_timeout
            + _SHUTDOWN_RETRY_TIMEOUT_SECONDS
            + _SHUTDOWN_JOIN_MARGIN_SECONDS
        )
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            LOGGER.error("job_supervisor_shutdown_incomplete")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
