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
from typing import Any, Self

from .models import AnalysisConfig, JobManifest, SensorMapping
from .parsing import InputValidationError, validate_stepwise_path
from .service import AnalysisService
from .storage import JobRepository

LOGGER = logging.getLogger("stepwise.jobs")
Runner = Callable[[str, str, dict[str, Any]], dict[str, Any]]


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
            try:
                run_id, config_payload = self._pending.get_nowait()
            except queue.Empty:
                return
            self.repository.mark_running(run_id)
            result_queue = self._context.Queue(maxsize=1)
            process = self._context.Process(
                target=_worker_entry,
                args=(str(self.repository.root), run_id, config_payload, self.runner, result_queue),
                name=f"stepwise-{run_id[:8]}",
            )
            process.start()
            with self._lock:
                self._active[run_id] = _ActiveJob(
                    process=process,
                    result_queue=result_queue,
                    started_at=time.monotonic(),
                )
            self._pending.task_done()

    @staticmethod
    def _stop_process(active: _ActiveJob) -> None:
        if active.process.is_alive():
            active.process.terminate()
        active.process.join(timeout=2)
        if active.process.is_alive() and hasattr(active.process, "kill"):
            active.process.kill()
            active.process.join(timeout=2)

    def _finish_active(self) -> None:
        with self._lock:
            snapshot = list(self._active.items())
        for run_id, active in snapshot:
            elapsed = time.monotonic() - active.started_at
            if active.process.is_alive() and elapsed <= self.timeout_seconds:
                continue

            if active.process.is_alive():
                self._stop_process(active)
                with self._lock:
                    self._active.pop(run_id, None)
                self.repository.mark_failed(
                    run_id,
                    "analysis_timeout",
                    "Analysis exceeded the configured time limit.",
                )
                active.result_queue.close()
                continue

            active.process.join(timeout=1)
            try:
                outcome, payload = active.result_queue.get(timeout=0.5)
            except queue.Empty:
                outcome, payload = (
                    "failed",
                    {"code": "worker_crashed", "message": "Analysis worker exited unexpectedly."},
                )
            with self._lock:
                self._active.pop(run_id, None)
            if outcome == "succeeded":
                self.repository.mark_succeeded(run_id, payload)
            else:
                self.repository.mark_failed(run_id, payload["code"], payload["message"])
            active.result_queue.close()

    def _supervise(self) -> None:
        while not self._stop.is_set():
            self._finish_active()
            self._start_pending()
            self._stop.wait(0.02)
        self._shutdown_jobs()

    def _shutdown_jobs(self) -> None:
        with self._lock:
            active_jobs = list(self._active.items())
            self._active.clear()
        for run_id, active in active_jobs:
            self._stop_process(active)
            self.repository.mark_failed(
                run_id,
                "service_stopped",
                "Analysis was interrupted because the service stopped.",
            )
            active.result_queue.close()
        while True:
            try:
                run_id, _config = self._pending.get_nowait()
            except queue.Empty:
                break
            self.repository.mark_failed(
                run_id,
                "service_stopped",
                "Analysis was cancelled because the service stopped.",
            )
            self._pending.task_done()

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self._thread.join(timeout=5)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
