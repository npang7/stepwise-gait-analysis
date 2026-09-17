"""Closed-loop concurrent load test for the StepWise HTTP service.

This module is intentionally outside ``src/stepwise``.  It drives the public HTTP
contract, uses the existing ``JobManager(runner=...)`` test injection point for
fault scenarios, and never changes product behaviour.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import ctypes
import hashlib
import importlib.metadata
import itertools
import json
import math
import os
import platform
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Self

import httpx
import numpy as np
import psutil

GiB = 1 << 30
MiB = 1 << 20

MIN_MEASUREMENT_AVAILABLE_BYTES = 2 * GiB
MAX_PAGEFILE_GROWTH_BYTES = 200 * MiB
MIN_DISK_FREE_BYTES = 20 * GiB
POLL_INTERVAL_SECONDS = 0.050
RETRY_429_SECONDS = 0.500
RESOURCE_INTERVAL_SECONDS = 1.0
CALIBRATION_INTERVAL_SECONDS = 0.050
CALIBRATION_UPLOAD_INTERVAL_SECONDS = 0.010
MEMORY_GATE_HEADROOM_BYTES = 2 * GiB
WARMUP_COMPLETIONS = 10
TARGET_COMPLETIONS = 200
MATRIX = ((30_000, 1, 300.0), (30_000, 2, 300.0), (30_000, 10, 120.0),
          (30_000, 12, 120.0), (30_000, 20, 120.0), (360_000, 2, 120.0))
SOURCE_ATTACHMENT_SHA256 = "A8A0D5C6B0B509516421FC18555E09332C7202F20C5D94EF4DBBA2A33E690798"
MAX_SERVICE_PATH_CHARS = 220
MAX_TRANSIENT_STATUS_500_PER_JOB = 20
_PATH_BUDGET_SESSION_ID = "s-12345678"
_PATH_BUDGET_SCENARIO_ID = "c360-2-3-12345678"
_PATH_BUDGET_RUN_ID = "00000000-0000-0000-0000-000000000000"
_LONGEST_ARTIFACT_NAME = "reference_screening_result.json"

PDH_COUNTERS = {
    "pages_input_per_second": r"\Memory\Pages Input/sec",
    "pages_output_per_second": r"\Memory\Pages Output/sec",
    "page_reads_per_second": r"\Memory\Page Reads/sec",
    "page_writes_per_second": r"\Memory\Page Writes/sec",
}


class UnsafeStoragePath(RuntimeError):
    """Raised before a filesystem operation crosses a measured-session boundary."""


@dataclass(frozen=True)
class WorkRootSelection:
    work_root: Path
    rejected_root: Path
    attempts: list[dict[str, Any]]
    path_budget: dict[str, Any]


def _strict_resolve(path: Path) -> Path:
    return path.resolve(strict=True)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def validate_storage_root(root: Path, repository_root: Path) -> Path:
    """Return a resolved safe external root or raise before any recursive operation."""

    home = Path.home().resolve(strict=False)
    if root.resolve(strict=False) == home:
        raise UnsafeStoragePath("storage root must not be the user home")
    try:
        resolved = _strict_resolve(root)
        repository = _strict_resolve(repository_root)
    except (OSError, RuntimeError) as exc:
        raise UnsafeStoragePath(f"storage root resolve failed: {exc}") from exc
    # On some managed Windows profiles strict resolution of the home directory itself
    # is denied even though descendants are usable. The equality guard needs a canonical
    # absolute spelling, while every destructive root/target still uses strict resolution.
    drive_root = Path(resolved.anchor)
    try:
        drive_root = _strict_resolve(drive_root)
    except (OSError, RuntimeError) as exc:
        raise UnsafeStoragePath(f"drive root resolve failed: {exc}") from exc
    if resolved == drive_root:
        raise UnsafeStoragePath("storage root must not be a drive root")
    if resolved == home:
        raise UnsafeStoragePath("storage root must not be the user home")
    if _is_relative_to(resolved, repository) or _is_relative_to(repository, resolved):
        raise UnsafeStoragePath("storage root and repository paths must be disjoint")
    return resolved


def _strict_descendant(target: Path, root: Path, repository_root: Path) -> tuple[Path, Path]:
    try:
        resolved_root = validate_storage_root(root, repository_root)
        resolved_target = _strict_resolve(target)
    except UnsafeStoragePath:
        raise
    except (OSError, RuntimeError) as exc:
        raise UnsafeStoragePath(f"target resolve failed: {exc}") from exc
    if resolved_target == resolved_root:
        raise UnsafeStoragePath("recursive operation may not target the work root itself")
    if not _is_relative_to(resolved_target, resolved_root):
        raise UnsafeStoragePath("recursive operation target must be a strict descendant")
    return resolved_target, resolved_root


def safe_rmtree(target: Path, work_root: Path, repository_root: Path) -> None:
    """Recursively delete only a resolved strict descendant of the selected work root."""

    try:
        resolved_target, _ = _strict_descendant(target, work_root, repository_root)
    except UnsafeStoragePath:
        raise
    except Exception as exc:
        raise UnsafeStoragePath(f"path composition/resolve failed: {exc}") from exc
    shutil.rmtree(resolved_target)


def _safe_new_destination(destination: Path, root: Path, repository_root: Path) -> tuple[Path, Path]:
    resolved_root = validate_storage_root(root, repository_root)
    if destination.exists():
        raise UnsafeStoragePath(f"destination already exists: {destination}")
    try:
        resolved_parent = _strict_resolve(destination.parent)
    except (OSError, RuntimeError) as exc:
        raise UnsafeStoragePath(f"destination parent resolve failed: {exc}") from exc
    if not _is_relative_to(resolved_parent, resolved_root):
        raise UnsafeStoragePath("move destination must be below the rejected root")
    candidate = resolved_parent / destination.name
    if candidate == resolved_root or not _is_relative_to(candidate, resolved_root):
        raise UnsafeStoragePath("move destination must be a strict descendant")
    return candidate, resolved_root


def safe_move_tree(
    source: Path,
    destination: Path,
    work_root: Path,
    rejected_root: Path,
    repository_root: Path,
) -> Path:
    """Move one measured tree after validating both component-level boundaries."""

    resolved_source, _ = _strict_descendant(source, work_root, repository_root)
    resolved_destination, _ = _safe_new_destination(
        destination, rejected_root, repository_root
    )
    shutil.move(str(resolved_source), str(resolved_destination))
    return _strict_resolve(resolved_destination)


def _probe_root(path: Path) -> None:
    probe = path / f".swlt-probe-{uuid.uuid4().hex}"
    payload = b"stepwise-load-test-root-probe\n"
    try:
        with probe.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if probe.read_bytes() != payload:
            raise OSError("root probe read-back mismatch")
    finally:
        probe.unlink(missing_ok=True)


def _prepare_root_pair(
    work_root: Path, rejected_root: Path, repository_root: Path
) -> tuple[Path, Path]:
    work_root.mkdir(parents=True, exist_ok=True)
    rejected_root.mkdir(parents=True, exist_ok=True)
    resolved_work = validate_storage_root(work_root, repository_root)
    resolved_rejected = validate_storage_root(rejected_root, repository_root)
    if _is_relative_to(resolved_work, resolved_rejected) or _is_relative_to(
        resolved_rejected, resolved_work
    ):
        raise UnsafeStoragePath("work and rejected roots must be component-level siblings")
    _probe_root(resolved_work)
    _probe_root(resolved_rejected)
    return resolved_work, resolved_rejected


def _service_path_budget(data_root: Path) -> dict[str, Any]:
    candidates = {
        "run_artifact": data_root
        / _PATH_BUDGET_RUN_ID
        / "artifacts"
        / _LONGEST_ARTIFACT_NAME,
        "staged_upload": data_root / ".staging" / _PATH_BUDGET_RUN_ID / "walking.txt",
    }
    lengths = {name: len(str(path.absolute())) for name, path in candidates.items()}
    longest_name = max(lengths, key=lengths.__getitem__)
    return {
        "limit_chars": MAX_SERVICE_PATH_CHARS,
        "max_chars": lengths[longest_name],
        "longest_kind": longest_name,
        "longest_path": str(candidates[longest_name].absolute()),
        "candidate_lengths": lengths,
        "passed": lengths[longest_name] <= MAX_SERVICE_PATH_CHARS,
    }


def _default_root_candidates() -> list[tuple[Path, Path]]:
    temp_root = Path(os.environ.get("TEMP", tempfile.gettempdir()))
    if os.name == "nt":
        return [
            (Path(r"C:\swlt"), Path(r"C:\swlt-rejected")),
            (Path.home() / "swlt", Path.home() / "swlt-rejected"),
            (temp_root / "swlt", temp_root / "swlt-rejected"),
        ]
    return [(temp_root / "swlt", temp_root / "swlt-rejected")]


def select_work_roots(
    repository_root: Path,
    *,
    candidates: list[tuple[Path, Path]] | None = None,
) -> WorkRootSelection:
    attempts: list[dict[str, Any]] = []
    for work_candidate, rejected_candidate in candidates or _default_root_candidates():
        attempt: dict[str, Any] = {
            "work_candidate": str(work_candidate),
            "rejected_candidate": str(rejected_candidate),
            "usable": False,
        }
        try:
            work_root, rejected_root = _prepare_root_pair(
                work_candidate, rejected_candidate, repository_root
            )
            data_root = (
                work_root
                / _PATH_BUDGET_SESSION_ID
                / _PATH_BUDGET_SCENARIO_ID
                / "data"
            )
            budget = _service_path_budget(data_root)
            attempt.update(
                {
                    "resolved_work_root": str(work_root),
                    "resolved_rejected_root": str(rejected_root),
                    "path_budget": budget,
                }
            )
            if not budget["passed"]:
                raise UnsafeStoragePath(
                    f"service path budget {budget['max_chars']} exceeds "
                    f"{MAX_SERVICE_PATH_CHARS} characters"
                )
            attempt["usable"] = True
            attempts.append(attempt)
            return WorkRootSelection(
                work_root=work_root,
                rejected_root=rejected_root,
                attempts=attempts,
                path_budget=budget,
            )
        except (OSError, RuntimeError) as exc:
            attempt["failure"] = f"{type(exc).__name__}: {exc}"
            attempts.append(attempt)
    raise UnsafeStoragePath(
        "no safe writable load-test root pair: "
        + json.dumps(attempts, ensure_ascii=False, sort_keys=True)
    )


@dataclass(frozen=True)
class ResourceSample:
    """One system/process observation; rates use one-core CPU semantics."""

    monotonic_s: float
    available_memory_bytes: int
    pagefile_used_bytes: int
    disk_free_bytes: int
    generator_cpu_percent: float | None = None
    generator_rss_bytes: int | None = None
    generator_page_faults: int | None = None
    service_pid: int | None = None
    service_cpu_percent: float | None = None
    service_rss_bytes: int | None = None
    service_page_faults: int | None = None
    worker_pids: tuple[int, ...] = ()
    worker_cpu_percent: float | None = None
    worker_rss_bytes: int | None = None
    worker_page_faults: int | None = None
    pages_input_per_second: float | None = None
    pages_output_per_second: float | None = None
    page_reads_per_second: float | None = None
    page_writes_per_second: float | None = None
    memcompression_pids: tuple[int, ...] = ()
    memcompression_rss_bytes: int | None = None
    memcompression_error: str | None = None


class MemCompressionSampler:
    """Cache and recover Windows' in-memory compression-store process handles."""

    PROCESS_NAME = "memcompression"

    def __init__(self) -> None:
        self._processes: dict[int, psutil.Process] = {}
        self.last_discovery_error: str | None = None

    def discover(self) -> None:
        found: dict[int, psutil.Process] = {}
        errors: list[str] = []
        try:
            processes = psutil.process_iter(["name"])
            for process in processes:
                try:
                    name = str(process.info.get("name") or "").lower()
                except (psutil.AccessDenied, psutil.NoSuchProcess) as exc:
                    errors.append(f"PID {process.pid}: {type(exc).__name__}: {exc}")
                    continue
                if name == self.PROCESS_NAME:
                    found[process.pid] = process
        except psutil.Error as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        self._processes = found
        if found:
            self.last_discovery_error = "; ".join(errors) or None
        else:
            errors.append("MemCompression process not found")
            self.last_discovery_error = "; ".join(errors)

    def collect(self) -> tuple[tuple[int, ...], int | None, str | None]:
        if not self._processes:
            self.discover()
        rss_by_pid: dict[int, int] = {}
        errors: list[str] = []
        for pid, process in tuple(self._processes.items()):
            try:
                rss_by_pid[pid] = int(process.memory_info().rss)
            except (psutil.AccessDenied, psutil.NoSuchProcess) as exc:
                self._processes.pop(pid, None)
                errors.append(f"PID {pid}: {type(exc).__name__}: {exc}")
        if not rss_by_pid and self._processes == {}:
            self.discover()
            for pid, process in tuple(self._processes.items()):
                try:
                    rss_by_pid[pid] = int(process.memory_info().rss)
                except (psutil.AccessDenied, psutil.NoSuchProcess) as exc:
                    self._processes.pop(pid, None)
                    errors.append(f"PID {pid}: {type(exc).__name__}: {exc}")
        if not rss_by_pid:
            if self.last_discovery_error:
                errors.append(self.last_discovery_error)
            return (), None, "; ".join(dict.fromkeys(errors))
        return (
            tuple(sorted(rss_by_pid)),
            sum(rss_by_pid.values()),
            "; ".join(dict.fromkeys(errors)) or self.last_discovery_error,
        )


class _PdhValueUnion(ctypes.Union):
    _fields_ = [("long_value", ctypes.c_long), ("double_value", ctypes.c_double)]


class _PdhFormattedValue(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("status", ctypes.c_ulong), ("value", _PdhValueUnion)]


class PdhSampler:
    """Collect English Windows PDH rate counters without spawning a process per sample."""

    PDH_FMT_DOUBLE = 0x00000200

    def __init__(self) -> None:
        self.available = False
        self.error: str | None = None
        self._query = ctypes.c_void_p()
        self._counters: dict[str, ctypes.c_void_p] = {}
        self._pdh: Any | None = None
        if os.name != "nt":
            self.error = "PDH is available only on Windows"
            return
        try:
            pdh = ctypes.WinDLL("pdh")
            self._pdh = pdh
            status = pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query))
            if status != 0:
                raise OSError(f"PdhOpenQueryW failed with status 0x{status:08x}")
            for name, path in PDH_COUNTERS.items():
                handle = ctypes.c_void_p()
                status = pdh.PdhAddEnglishCounterW(
                    self._query, ctypes.c_wchar_p(path), 0, ctypes.byref(handle)
                )
                if status != 0:
                    raise OSError(
                        f"PdhAddEnglishCounterW({path}) failed with status 0x{status:08x}"
                    )
                self._counters[name] = handle
            status = pdh.PdhCollectQueryData(self._query)
            if status != 0:
                raise OSError(f"PdhCollectQueryData failed with status 0x{status:08x}")
            self.available = True
        except (AttributeError, OSError) as exc:
            self.error = str(exc)
            self.close()

    def collect(self) -> dict[str, float | None]:
        values: dict[str, float | None] = {name: None for name in PDH_COUNTERS}
        if not self.available or self._pdh is None:
            return values
        status = self._pdh.PdhCollectQueryData(self._query)
        if status != 0:
            self.error = f"PdhCollectQueryData failed with status 0x{status:08x}"
            return values
        for name, handle in self._counters.items():
            value = _PdhFormattedValue()
            counter_type = ctypes.c_ulong()
            status = self._pdh.PdhGetFormattedCounterValue(
                handle,
                self.PDH_FMT_DOUBLE,
                ctypes.byref(counter_type),
                ctypes.byref(value),
            )
            if status == 0 and value.status == 0 and math.isfinite(value.double_value):
                values[name] = max(0.0, float(value.double_value))
        return values

    def close(self) -> None:
        if self._pdh is not None and self._query:
            self._pdh.PdhCloseQuery(self._query)
        self._query = ctypes.c_void_p()
        self._counters.clear()
        self.available = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()


def _process_memory_row(process: psutil.Process) -> dict[str, Any]:
    try:
        full = process.memory_full_info()
        uss_bytes: int | None = int(getattr(full, "uss", 0))
        access = "full"
    except psutil.AccessDenied:
        full = process.memory_info()
        uss_bytes = None
        access = "basic-no-uss"
    return {
        "pid": process.pid,
        "name": process.name(),
        "rss_bytes": int(full.rss),
        "uss_bytes": uss_bytes,
        "private_bytes": int(getattr(full, "private", 0)),
        "access": access,
    }


def memory_inventory(limit: int = 20) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    denied_basic: list[int] = []
    for process in psutil.process_iter():
        try:
            rows.append(_process_memory_row(process))
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            denied_basic.append(process.pid)
    rows_by_private = sorted(rows, key=lambda row: row["private_bytes"], reverse=True)
    rows_by_rss = sorted(rows, key=lambda row: row["rss_bytes"], reverse=True)
    rows_with_uss = [row for row in rows if row["uss_bytes"] is not None]
    rows_by_uss = sorted(rows_with_uss, key=lambda row: row["uss_bytes"], reverse=True)
    virtual = psutil.virtual_memory()
    process_uss = sum(int(row["uss_bytes"]) for row in rows_with_uss)
    process_private = sum(row["private_bytes"] for row in rows)
    process_rss = sum(row["rss_bytes"] for row in rows)
    used_by_available = int(virtual.total - virtual.available)
    return {
        "top_processes": rows_by_private[:limit],
        "top_process_sort": "private_bytes descending; USS is null where access was denied",
        "top_pid_order_by_uss": [row["pid"] for row in rows_by_uss[:limit]],
        "top_pid_order_by_private": [row["pid"] for row in rows_by_private[:limit]],
        "top_pid_order_by_rss": [row["pid"] for row in rows_by_rss[:limit]],
        "other_processes": {
            "count": max(0, len(rows_by_private) - limit),
            "known_uss_bytes": sum(
                int(row["uss_bytes"])
                for row in rows_by_private[limit:]
                if row["uss_bytes"] is not None
            ),
            "private_bytes": sum(row["private_bytes"] for row in rows_by_private[limit:]),
            "rss_bytes": sum(row["rss_bytes"] for row in rows_by_private[limit:]),
        },
        "process_count": len(rows),
        "processes_with_uss_count": len(rows_with_uss),
        "all_process_uss_bytes": process_uss,
        "all_process_private_bytes": process_private,
        "all_process_rss_bytes": process_rss,
        "system_used_by_available_bytes": used_by_available,
        "unattributed_vs_known_uss_bytes": used_by_available - process_uss,
        "unattributed_vs_private_bytes": used_by_available - process_private,
        "access_denied_basic_pids": denied_basic,
        "note": (
            "USS is available only for processes permitting memory_full_info. RSS double-counts "
            "shared pages; private bytes are committed private memory, not resident physical "
            "memory. System/cache/kernel attribution is therefore the displayed residual estimate."
        ),
    }


def _power_status() -> dict[str, Any]:
    if os.name != "nt":
        return {"available": False, "reason": "Windows power status unavailable"}

    class SystemPowerStatus(ctypes.Structure):
        _fields_ = [
            ("ac_line_status", ctypes.c_byte),
            ("battery_flag", ctypes.c_byte),
            ("battery_life_percent", ctypes.c_byte),
            ("system_status_flag", ctypes.c_byte),
            ("battery_life_time", ctypes.c_ulong),
            ("battery_full_life_time", ctypes.c_ulong),
        ]

    value = SystemPowerStatus()
    ok = ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(value))
    if not ok:
        return {"available": False, "reason": "GetSystemPowerStatus failed"}
    return {
        "available": True,
        "ac_online": value.ac_line_status == 1,
        "ac_line_status": int(value.ac_line_status),
        "battery_flag": int(value.battery_flag),
        "battery_percent": (
            None if value.battery_life_percent == -1 else int(value.battery_life_percent)
        ),
    }


def _dependency_versions() -> dict[str, str | None]:
    names = (
        "stepwise-gait",
        "fastapi",
        "uvicorn",
        "python-multipart",
        "numpy",
        "pandas",
        "pyarrow",
        "matplotlib",
        "httpx",
        "psutil",
        "mypy",
        "pandas-stubs",
        "pytest",
        "pytest-cov",
        "ruff",
    )
    versions: dict[str, str | None] = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def preflight_snapshot(work_root: Path) -> dict[str, Any]:
    virtual = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage(str(work_root.resolve()))
    snapshot: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "machine": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "physical_cores": psutil.cpu_count(logical=False),
            "logical_processors": psutil.cpu_count(logical=True),
            "python": platform.python_version(),
            "python_executable": sys.executable,
        },
        "memory": {
            "total_bytes": int(virtual.total),
            "available_bytes": int(virtual.available),
        },
        "pagefile": {
            "total_bytes": int(swap.total),
            "used_bytes": int(swap.used),
            "free_bytes": int(swap.free),
            "note": "psutil sin/sout are not used on Windows.",
        },
        "disk": {
            "path": str(work_root.resolve()),
            "total_bytes": int(disk.total),
            "free_bytes": int(disk.free),
            "required_free_bytes": MIN_DISK_FREE_BYTES,
        },
        "power": _power_status(),
        "dependencies": _dependency_versions(),
    }
    failures: list[str] = []
    snapshot["memory_inventory"] = memory_inventory()
    if disk.free < MIN_DISK_FREE_BYTES:
        failures.append("disk_free_below_20_gib")
    if snapshot["power"].get("available") and not snapshot["power"].get("ac_online"):
        failures.append("ac_power_offline")
    snapshot["passed"] = not failures
    snapshot["failures"] = failures
    return snapshot


def instrumented_runner(root: str, run_id: str, config_payload: dict[str, Any]) -> dict[str, Any]:
    """Delegate to the product runner while preserving a failure traceback in stdout."""

    from stepwise.jobs import _run_analysis

    try:
        return _run_analysis(root, run_id, config_payload)
    except Exception:
        print(f"BENCH_RUNNER_EXCEPTION run_id={run_id}", file=sys.stderr, flush=True)
        traceback.print_exc()
        raise


def selective_slow_runner(
    root: str, run_id: str, config_payload: dict[str, Any]
) -> dict[str, Any]:
    """Top-level, Windows-spawn-picklable timeout runner using the existing injection point."""

    mapping = config_payload.get("sensor_mapping", {})
    if mapping.get("pitch_eversion_sign") == "negative":
        time.sleep(float(os.environ.get("STEPWISE_BENCH_SLOW_SECONDS", "240")))
        return {"summary": {}, "metrics": {}, "risk_cards": [], "artifacts": []}
    return instrumented_runner(root, run_id, config_payload)


def _ttl_cleanup_loop(manager: Any, stop: threading.Event, interval_seconds: float) -> None:
    while not stop.wait(interval_seconds):
        try:
            manager.repository.cleanup_expired(manager.result_ttl_hours)
        except Exception:  # noqa: BLE001 - evidence must survive a fault-injection race.
            print("BENCH_TTL_CLEANUP_EXCEPTION", file=sys.stderr, flush=True)
            traceback.print_exc()


def create_benchmark_app() -> Any:
    """Uvicorn factory for normal and fault scenarios without modifying product source."""

    from stepwise.api import create_app
    from stepwise.jobs import JobManager
    from stepwise.settings import Settings

    settings = Settings.from_env()
    mode = os.environ.get("STEPWISE_BENCH_RUNNER_MODE", "normal")
    runner = selective_slow_runner if mode == "slow" else instrumented_runner
    manager = JobManager(
        settings.data_dir,
        max_workers=settings.max_workers,
        max_queue=settings.max_queue,
        timeout_seconds=settings.analysis_timeout_seconds,
        max_upload_bytes=settings.max_upload_bytes,
        result_ttl_hours=settings.result_ttl_hours,
        runner=runner,
    )
    cleanup_stop = threading.Event()
    cleanup_thread: threading.Thread | None = None
    if mode == "ttl":
        cleanup_thread = threading.Thread(
            target=_ttl_cleanup_loop,
            args=(manager, cleanup_stop, 0.02),
            name="stepwise-bench-ttl-cleaner",
            daemon=True,
        )
        cleanup_thread.start()
    app = create_app(settings, manager=manager)

    @asynccontextmanager
    async def benchmark_lifespan(_app: Any) -> Any:
        try:
            yield
        finally:
            cleanup_stop.set()
            if cleanup_thread is not None:
                cleanup_thread.join(timeout=2)
            manager.close()

    app.router.lifespan_context = benchmark_lifespan
    return app


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class ServiceHandle:
    process: subprocess.Popen[bytes]
    log_handle: BinaryIO
    log_path: Path
    data_root: Path
    base_url: str
    pid: int
    create_time: float
    started_at: str
    launcher_pid: int | None = None
    health_ready_at: str | None = None
    stopped_at: str | None = None
    exit_code: int | None = None
    forced_stop: bool = False
    observed_worker_pids: set[int] = field(default_factory=set)

    def metadata(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "launcher_pid": self.launcher_pid,
            "process_create_time": self.create_time,
            "started_at": self.started_at,
            "health_ready_at": self.health_ready_at,
            "stopped_at": self.stopped_at,
            "exit_code": self.exit_code,
            "forced_stop": self.forced_stop,
            "observed_worker_pids": sorted(self.observed_worker_pids),
            "log_path": str(self.log_path),
            "data_root": str(self.data_root),
        }


async def start_service(
    *, data_root: Path, log_path: Path, runner_mode: str = "normal", ttl_hours: float = 24.0
) -> ServiceHandle:
    budget = _service_path_budget(data_root)
    if not budget["passed"]:
        raise UnsafeStoragePath(
            f"service path budget {budget['max_chars']} exceeds "
            f"{MAX_SERVICE_PATH_CHARS} characters: {budget['longest_path']}"
        )
    data_root.mkdir(parents=True, exist_ok=False)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("wb")
    port = _free_local_port()
    env = os.environ.copy()
    env.update(
        {
            "MPLBACKEND": "Agg",
            "PYTHONUNBUFFERED": "1",
            "STEPWISE_DATA_DIR": str(data_root),
            "STEPWISE_MAX_UPLOAD_BYTES": str(64 * MiB),
            "STEPWISE_ANALYSIS_TIMEOUT_SECONDS": "120",
            "STEPWISE_MAX_WORKERS": "2",
            "STEPWISE_MAX_QUEUE": "8",
            "STEPWISE_RESULT_TTL_HOURS": str(ttl_hours),
            "STEPWISE_BENCH_RUNNER_MODE": runner_mode,
            "STEPWISE_BENCH_SLOW_SECONDS": "240",
        }
    )
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "bench.load_test:create_benchmark_app",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--workers",
        "1",
        "--log-level",
        "info",
    ]
    process: subprocess.Popen[bytes] | None = None
    try:
        process = await asyncio.to_thread(
            subprocess.Popen,
            command,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            creationflags=creationflags,
        )
        ps_process = psutil.Process(process.pid)
        handle = ServiceHandle(
            process=process,
            log_handle=log_handle,
            log_path=log_path,
            data_root=data_root,
            base_url=f"http://127.0.0.1:{port}",
            pid=process.pid,
            create_time=ps_process.create_time(),
            started_at=datetime.now(UTC).isoformat(),
            launcher_pid=process.pid,
        )
        deadline = time.monotonic() + 30.0
        async with httpx.AsyncClient(timeout=2.0) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"service exited during startup with {process.returncode}")
                try:
                    response = await client.get(f"{handle.base_url}/healthz")
                    payload = response.json()
                    if response.status_code == 200 and payload.get("status") == "ok":
                        try:
                            descendants = ps_process.children(recursive=False)
                            uvicorn_children = [
                                child
                                for child in descendants
                                if "uvicorn" in " ".join(child.cmdline()).lower()
                            ]
                            if uvicorn_children:
                                actual = uvicorn_children[0]
                                handle.pid = actual.pid
                                handle.create_time = actual.create_time()
                        except (psutil.AccessDenied, psutil.NoSuchProcess):
                            pass
                        handle.health_ready_at = datetime.now(UTC).isoformat()
                        return handle
                except (httpx.HTTPError, ValueError):
                    pass
                await asyncio.sleep(0.1)
        raise TimeoutError("service did not become healthy within 30 seconds")
    except Exception:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log_handle.close()
        raise


async def stop_service(handle: ServiceHandle) -> None:
    if handle.process.poll() is None:
        try:
            if os.name == "nt":
                handle.process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                handle.process.send_signal(signal.SIGINT)
            await asyncio.to_thread(handle.process.wait, 10)
        except (OSError, subprocess.TimeoutExpired):
            handle.forced_stop = True
            handle.process.terminate()
            try:
                await asyncio.to_thread(handle.process.wait, 5)
            except subprocess.TimeoutExpired:
                handle.process.kill()
                await asyncio.to_thread(handle.process.wait, 5)
    handle.exit_code = handle.process.returncode
    handle.stopped_at = datetime.now(UTC).isoformat()
    handle.log_handle.flush()
    handle.log_handle.close()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        survivors = [
            pid
            for pid in ({handle.pid} | handle.observed_worker_pids)
            if pid != handle.launcher_pid and psutil.pid_exists(pid)
        ]
        if not survivors:
            return
        await asyncio.sleep(0.1)
    handle.forced_stop = True


def _cpu_time(process: psutil.Process) -> float:
    times = process.cpu_times()
    return float(times.user + times.system)


class ResourceMonitor:
    """Sample the generator, service tree, memory, pagefile, disk, and PDH at 1 Hz."""

    def __init__(
        self,
        service: ServiceHandle,
        disk_path: Path,
        memcompression: MemCompressionSampler | None = None,
    ) -> None:
        self.service = service
        self.disk_path = disk_path
        self.samples: list[ResourceSample] = []
        self.errors: list[str] = []
        self._stop = asyncio.Event()
        self._previous_cpu: dict[int, tuple[float, float]] = {}
        self._pdh = PdhSampler()
        self._memcompression = memcompression or MemCompressionSampler()

    def _cpu_percent(self, process: psutil.Process, now: float) -> float | None:
        try:
            current = _cpu_time(process)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            return None
        previous = self._previous_cpu.get(process.pid)
        self._previous_cpu[process.pid] = (now, current)
        if previous is None or now <= previous[0]:
            return None
        return max(0.0, (current - previous[1]) / (now - previous[0]) * 100.0)

    @staticmethod
    def _memory(process: psutil.Process) -> tuple[int, int]:
        info = process.memory_info()
        return int(info.rss), int(getattr(info, "num_page_faults", 0))

    def collect(self) -> ResourceSample:
        now = time.monotonic()
        virtual = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_usage(str(self.disk_path.resolve()))
        generator = psutil.Process(os.getpid())
        generator_rss, generator_faults = self._memory(generator)
        generator_cpu = self._cpu_percent(generator, now)
        service_cpu: float | None = None
        service_rss: int | None = None
        service_faults: int | None = None
        worker_pids: tuple[int, ...] = ()
        worker_cpu_values: list[float] = []
        worker_rss = 0
        worker_faults = 0
        try:
            service = psutil.Process(self.service.pid)
            service_rss, service_faults = self._memory(service)
            service_cpu = self._cpu_percent(service, now)
            workers = service.children(recursive=True)
            worker_pids = tuple(sorted(process.pid for process in workers))
            self.service.observed_worker_pids.update(worker_pids)
            for worker in workers:
                try:
                    rss, faults = self._memory(worker)
                    cpu = self._cpu_percent(worker, now)
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    continue
                worker_rss += rss
                worker_faults += faults
                if cpu is not None:
                    worker_cpu_values.append(cpu)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
        pdh_values = self._pdh.collect()
        memcompression_pids, memcompression_rss, memcompression_error = (
            self._memcompression.collect()
        )
        sample = ResourceSample(
            monotonic_s=now,
            available_memory_bytes=int(virtual.available),
            pagefile_used_bytes=int(swap.used),
            disk_free_bytes=int(disk.free),
            generator_cpu_percent=generator_cpu,
            generator_rss_bytes=generator_rss,
            generator_page_faults=generator_faults,
            service_pid=self.service.pid,
            service_cpu_percent=service_cpu,
            service_rss_bytes=service_rss,
            service_page_faults=service_faults,
            worker_pids=worker_pids,
            worker_cpu_percent=(sum(worker_cpu_values) if worker_cpu_values else None),
            worker_rss_bytes=worker_rss,
            worker_page_faults=worker_faults,
            pages_input_per_second=pdh_values["pages_input_per_second"],
            pages_output_per_second=pdh_values["pages_output_per_second"],
            page_reads_per_second=pdh_values["page_reads_per_second"],
            page_writes_per_second=pdh_values["page_writes_per_second"],
            memcompression_pids=memcompression_pids,
            memcompression_rss_bytes=memcompression_rss,
            memcompression_error=memcompression_error,
        )
        self.samples.append(sample)
        return sample

    async def run(self) -> None:
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    self.collect()
                except (OSError, psutil.Error) as exc:
                    self.errors.append(f"{type(exc).__name__}: {exc}")
                remaining = RESOURCE_INTERVAL_SECONDS - (time.monotonic() - started)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=max(0.0, remaining))
                except TimeoutError:
                    pass
        finally:
            self._pdh.close()

    def stop(self) -> None:
        self._stop.set()


@dataclass(frozen=True)
class CalibrationSample:
    monotonic_s: float
    phase: str
    generator_rss_bytes: int
    service_parent_rss_bytes: int
    service_helper_rss_bytes: int
    worker_rss_bytes: int
    helper_pids: tuple[int, ...]
    worker_pids: tuple[int, ...]
    available_memory_bytes: int
    pagefile_used_bytes: int
    disk_free_bytes: int
    memcompression_pids: tuple[int, ...]
    memcompression_rss_bytes: int | None
    memcompression_error: str | None


class CalibrationMonitor:
    """High-frequency RSS sampler used only by the two single-job calibrations."""

    def __init__(
        self,
        service: ServiceHandle,
        disk_path: Path,
        memcompression: MemCompressionSampler | None = None,
    ) -> None:
        self.service = service
        self.disk_path = disk_path
        self.phase = "idle"
        self.samples: list[CalibrationSample] = []
        self.errors: list[str] = []
        self._stop = asyncio.Event()
        self._memcompression = memcompression or MemCompressionSampler()

    @property
    def interval_seconds(self) -> float:
        return (
            CALIBRATION_UPLOAD_INTERVAL_SECONDS
            if self.phase == "upload"
            else CALIBRATION_INTERVAL_SECONDS
        )

    @staticmethod
    def _role(process: psutil.Process) -> str:
        try:
            command = " ".join(process.cmdline()).lower()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            return "worker"
        return "helper" if "resource_tracker" in command else "worker"

    def collect(self) -> CalibrationSample:
        parent_rss = 0
        helper_rss = 0
        worker_rss = 0
        helper_pids: list[int] = []
        worker_pids: list[int] = []
        try:
            service = psutil.Process(self.service.pid)
            parent_rss = int(service.memory_info().rss)
            for descendant in service.children(recursive=True):
                try:
                    rss = int(descendant.memory_info().rss)
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    continue
                self.service.observed_worker_pids.add(descendant.pid)
                if self._role(descendant) == "helper":
                    helper_rss += rss
                    helper_pids.append(descendant.pid)
                else:
                    worker_rss += rss
                    worker_pids.append(descendant.pid)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
        memcompression_pids, memcompression_rss, memcompression_error = (
            self._memcompression.collect()
        )
        sample = CalibrationSample(
            monotonic_s=time.monotonic(),
            phase=self.phase,
            generator_rss_bytes=int(psutil.Process(os.getpid()).memory_info().rss),
            service_parent_rss_bytes=parent_rss,
            service_helper_rss_bytes=helper_rss,
            worker_rss_bytes=worker_rss,
            helper_pids=tuple(sorted(helper_pids)),
            worker_pids=tuple(sorted(worker_pids)),
            available_memory_bytes=int(psutil.virtual_memory().available),
            pagefile_used_bytes=int(psutil.swap_memory().used),
            disk_free_bytes=int(psutil.disk_usage(str(self.disk_path.resolve())).free),
            memcompression_pids=memcompression_pids,
            memcompression_rss_bytes=memcompression_rss,
            memcompression_error=memcompression_error,
        )
        self.samples.append(sample)
        return sample

    async def run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.collect()
            except (OSError, psutil.Error) as exc:
                self.errors.append(f"{type(exc).__name__}: {exc}")
            remaining = self.interval_seconds - (time.monotonic() - started)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(0.0, remaining))
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


def calibration_threshold(calibration: dict[str, Any], concurrency: int) -> dict[str, Any]:
    baseline = int(calibration["generator_baseline_rss_bytes"])
    observed_increment = int(calibration["generator_upload_increment_peak_bytes"])
    input_bytes = int(calibration["input_bytes"])
    per_request = max(input_bytes, observed_increment)
    generator_scenario = baseline + concurrency * per_request
    worker_peak = int(calibration["worker_peak_rss_bytes"])
    parent_peak = int(calibration["parent_component_peak_rss_bytes"])
    threshold = (
        worker_peak * 2
        + parent_peak
        + generator_scenario
        + MEMORY_GATE_HEADROOM_BYTES
    )
    return {
        "concurrency": concurrency,
        "worker_peak_rss_bytes": worker_peak,
        "worker_count": 2,
        "parent_peak_rss_bytes": parent_peak,
        "generator_baseline_rss_bytes": baseline,
        "generator_upload_increment_peak_bytes": observed_increment,
        "input_bytes": input_bytes,
        "per_request_memory_bytes": per_request,
        "generator_scenario_peak_rss_bytes": generator_scenario,
        "runtime_floor_bytes": MEMORY_GATE_HEADROOM_BYTES,
        "threshold_bytes": threshold,
        "formula": (
            f"{worker_peak} * 2 + {parent_peak} + {generator_scenario} + "
            f"{MEMORY_GATE_HEADROOM_BYTES} = {threshold}"
        ),
    }


def decide_memory_subsets(
    available_bytes: int,
    threshold_30k: dict[str, Any],
    threshold_360k: dict[str, Any],
) -> dict[str, Any]:
    run_30k = available_bytes >= int(threshold_30k["threshold_bytes"])
    run_360k = available_bytes >= int(threshold_360k["threshold_bytes"])
    return {
        "available_bytes": available_bytes,
        "run_30k": run_30k,
        "run_360k": run_360k,
        "any_subset": run_30k or run_360k,
    }


async def read_available_memory_samples() -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for index in range(3):
        samples.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "available_bytes": int(psutil.virtual_memory().available),
            }
        )
        if index < 2:
            await asyncio.sleep(1.0)
    return samples


def _integral(samples: list[ResourceSample], field_name: str) -> float | None:
    usable = [sample for sample in samples if getattr(sample, field_name) is not None]
    if len(usable) < 2:
        return None
    total = 0.0
    for previous, current in itertools.pairwise(usable):
        elapsed = current.monotonic_s - previous.monotonic_s
        total += float(getattr(previous, field_name)) * max(0.0, elapsed)
    return total


def resource_statistics(samples: list[ResourceSample]) -> dict[str, Any]:
    gate = classify_resource_gate(samples)
    generator_cpu = [
        float(sample.generator_cpu_percent)
        for sample in samples
        if sample.generator_cpu_percent is not None
    ]
    result: dict[str, Any] = {
        **gate,
        "sample_count": len(samples),
        "generator_cpu": generator_cpu_statistics(generator_cpu),
        "generator_cpu_single_core": True,
        "generator_limited": generator_is_limited(generator_cpu),
        "process_page_fault_note": (
            "num_page_faults contains soft and hard faults and is not reported as hard faults"
        ),
    }
    for field_name in (
        "pages_input_per_second",
        "pages_output_per_second",
        "page_reads_per_second",
        "page_writes_per_second",
    ):
        values = [
            float(getattr(sample, field_name))
            for sample in samples
            if getattr(sample, field_name) is not None
        ]
        result[field_name] = (
            {"n": len(values), "median": statistics.median(values), "max": max(values)}
            if values
            else {"n": 0, "not_collected": True}
        )
    result["hard_fault_pages_input_total"] = _integral(samples, "pages_input_per_second")
    result["hard_fault_disk_read_operations_total"] = _integral(
        samples, "page_reads_per_second"
    )
    result["page_write_operations_total"] = _integral(samples, "page_writes_per_second")
    parent_peak = max((sample.service_rss_bytes or 0 for sample in samples), default=0)
    workers_peak = max((sample.worker_rss_bytes or 0 for sample in samples), default=0)
    result["service_parent_peak_rss_bytes"] = parent_peak
    result["worker_simultaneous_peak_rss_bytes"] = workers_peak
    result["required_peak_rss_sum_bytes"] = parent_peak + workers_peak
    return result


def _canonical_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("correctness summaries require finite floats")
        return value.hex()
    raise TypeError(f"unsupported correctness value: {type(value).__name__}")


def _canonical_steps(csv_text: str) -> dict[str, Any]:
    reader = csv.reader(StringIO(csv_text))
    rows = list(reader)
    if not rows:
        raise ValueError("steps CSV is empty")
    columns = rows[0]
    if not columns or len(set(columns)) != len(columns):
        raise ValueError("steps CSV requires unique columns")
    normalized_rows: list[list[Any]] = []
    for row in rows[1:]:
        if len(row) != len(columns):
            raise ValueError("steps CSV row width does not match its header")
        normalized: list[Any] = []
        for column, raw in zip(columns, row, strict=True):
            if column == "Pattern":
                normalized.append(raw)
            elif raw == "":
                normalized.append(None)
            else:
                try:
                    number = float(raw)
                except ValueError as exc:
                    raise ValueError(f"steps column {column} is not numeric") from exc
                if not math.isfinite(number):
                    raise ValueError("correctness summaries require finite floats")
                normalized.append(number.hex())
        normalized_rows.append(normalized)
    return {"columns": columns, "rows": normalized_rows}


def stable_result_digest(result: dict[str, Any], steps_csv: str) -> str:
    """Hash result semantics without rounding any finite floating-point value."""

    required = {"summary", "metrics", "risk_cards", "artifacts"}
    missing = required - set(result)
    if missing:
        raise ValueError(f"result is missing required fields: {', '.join(sorted(missing))}")
    selected = {key: result[key] for key in sorted(required)}
    payload = {
        "result": _canonical_json_value(selected),
        "steps": _canonical_steps(steps_csv),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _nearest_rank(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("at least one value is required")
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return float(ordered[rank - 1])


def latency_statistics(values: list[float]) -> dict[str, Any]:
    """Return the owner-approved small-sample policy; p99 is deliberately absent."""

    if not values:
        return {"n": 0, "quantile": "none"}
    result: dict[str, Any] = {
        "n": len(values),
        "p50": _nearest_rank(values, 0.50),
    }
    if len(values) >= 100:
        result.update({"p95": _nearest_rank(values, 0.95), "quantile": "p95"})
    else:
        result.update(
            {
                "p90": _nearest_rank(values, 0.90),
                "max": float(max(values)),
                "quantile": "p90+max",
            }
        )
    return result


def summarize_repetition_quantiles(statistics_rows: list[dict[str, Any]]) -> str:
    policies = {str(row.get("quantile", "none")) for row in statistics_rows}
    if len(policies) != 1:
        return "mixed; see repetitions"
    return policies.pop() if policies else "none"


def effective_concurrency(
    accepted_intervals: list[tuple[float, float]], *, window_start: float, window_end: float
) -> float:
    if window_end <= window_start:
        raise ValueError("measurement window must have positive duration")
    occupied = 0.0
    for accepted_at, terminal_at in accepted_intervals:
        start = max(window_start, accepted_at)
        end = min(window_end, terminal_at)
        if end > start:
            occupied += end - start
    return occupied / (window_end - window_start)


def generator_is_limited(cpu_percent_samples: list[float]) -> bool:
    """Flag a sustained >80% share of one logical CPU for ten consecutive samples."""

    window = 10
    return any(
        statistics.median(cpu_percent_samples[index - window : index]) > 80.0
        for index in range(window, len(cpu_percent_samples) + 1)
    )


def generator_cpu_statistics(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    rolling = [
        statistics.median(values[index - 10 : index])
        for index in range(10, len(values) + 1)
    ]
    return {
        "n": len(values),
        "p50": _nearest_rank(values, 0.50),
        "p95": _nearest_rank(values, 0.95),
        "max": max(values),
        "rolling_10_second_median_max": max(rolling) if rolling else None,
    }


def _status_poll_retry_allowed(status_code: int, anomaly_count: int) -> bool:
    return status_code == 500 and anomaly_count <= MAX_TRANSIENT_STATUS_500_PER_JOB


def classify_resource_gate(samples: list[ResourceSample]) -> dict[str, Any]:
    if not samples:
        return {
            "memory_pressure": False,
            "available_memory_below_2_gib": False,
            "pagefile_growth_over_200_mib": False,
            "memory_displacement_over_200_mib": False,
            "memory_displacement_bytes": 0,
            "memcompression_measured": False,
            "memcompression_growth_bytes": None,
            "memcompression_measurement_note": "MemCompression was not measurable",
            "low_available_without_displacement_evidence": False,
            "disk_below_20_gib": False,
            "abort_suite": False,
        }
    available_low = min(sample.available_memory_bytes for sample in samples)
    pagefile_start = samples[0].pagefile_used_bytes
    pagefile_max = max(sample.pagefile_used_bytes for sample in samples)
    disk_low = min(sample.disk_free_bytes for sample in samples)
    available_gate = available_low < MIN_MEASUREMENT_AVAILABLE_BYTES
    pagefile_growth = max(0, pagefile_max - pagefile_start)
    pagefile_gate = pagefile_growth > MAX_PAGEFILE_GROWTH_BYTES
    memcompression_start = samples[0].memcompression_rss_bytes
    memcompression_values = [
        int(sample.memcompression_rss_bytes)
        for sample in samples
        if sample.memcompression_rss_bytes is not None
    ]
    memcompression_measured = memcompression_start is not None
    memcompression_max = max(memcompression_values) if memcompression_values else None
    memcompression_end = samples[-1].memcompression_rss_bytes
    memcompression_growth = (
        max(0, int(memcompression_max) - int(memcompression_start))
        if memcompression_start is not None and memcompression_max is not None
        else None
    )
    displacement = pagefile_growth + (memcompression_growth or 0)
    displacement_gate = displacement > MAX_PAGEFILE_GROWTH_BYTES
    disk_gate = disk_low < MIN_DISK_FREE_BYTES
    memcompression_errors = sorted(
        {
            str(sample.memcompression_error)
            for sample in samples
            if sample.memcompression_error
        }
    )
    memcompression_pids = sorted(
        {pid for sample in samples for pid in sample.memcompression_pids}
    )
    return {
        "memory_pressure": displacement_gate,
        "available_memory_below_2_gib": available_gate,
        "pagefile_growth_over_200_mib": pagefile_gate,
        "pagefile_growth_bytes": pagefile_growth,
        "memcompression_measured": memcompression_measured,
        "memcompression_pids": memcompression_pids,
        "memcompression_rss_start_bytes": memcompression_start,
        "memcompression_rss_end_bytes": memcompression_end,
        "memcompression_rss_max_bytes": memcompression_max,
        "memcompression_growth_bytes": memcompression_growth,
        "memcompression_sample_count": len(memcompression_values),
        "memcompression_missing_sample_count": len(samples) - len(memcompression_values),
        "memcompression_errors": memcompression_errors,
        "memcompression_measurement_note": (
            "MemCompression RSS measured"
            if memcompression_measured
            else "MemCompression was not measurable"
        ),
        "memory_displacement_bytes": displacement,
        "memory_displacement_over_200_mib": displacement_gate,
        "low_available_without_displacement_evidence": (
            available_gate and not displacement_gate
        ),
        "disk_below_20_gib": disk_gate,
        "abort_suite": disk_gate,
        "available_memory_min_bytes": available_low,
        "pagefile_used_start_bytes": pagefile_start,
        "pagefile_used_end_bytes": samples[-1].pagefile_used_bytes,
        "pagefile_used_max_bytes": pagefile_max,
        "disk_free_min_bytes": disk_low,
    }


def calibration_failure_reason(
    *,
    job_failure: str | None,
    upload_sample_count: int,
    parent_peak_rss_bytes: int,
    worker_peak_rss_bytes: int,
) -> str | None:
    """Validate structural calibration output without applying timing hygiene gates."""

    if job_failure is not None:
        return job_failure
    missing: list[str] = []
    if upload_sample_count <= 0:
        missing.append("generator upload RSS peak")
    if parent_peak_rss_bytes <= 0:
        missing.append("service parent RSS peak")
    if worker_peak_rss_bytes <= 0:
        missing.append("worker RSS peak")
    if missing:
        return "calibration could not obtain required peaks: " + ", ".join(missing)
    return None


def compare_drift(
    a1_completed_counts: list[int],
    a2_completed_count: int,
    comparison_targets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Describe an A1/A2 level shift without treating a zero-width range as noise."""

    if len(a1_completed_counts) != 3 or any(value <= 0 for value in a1_completed_counts):
        raise ValueError("A1 drift control requires three positive completed-job counts")
    if a2_completed_count <= 0:
        raise ValueError("A2 drift control requires a positive completed-job count")

    median_count = int(statistics.median(a1_completed_counts))
    delta = a2_completed_count - median_count
    shift_percent = delta / median_count * 100.0
    comparisons: list[dict[str, Any]] = []
    labels: set[str] = set()
    for target in comparison_targets:
        label = str(target["label"])
        if label in labels:
            raise ValueError(f"duplicate drift comparison label: {label}")
        labels.add(label)
        completed_counts_value = target.get("completed_counts")
        completed_counts: list[int] | None
        if completed_counts_value is None:
            completed_counts = None
        else:
            completed_counts = [int(value) for value in completed_counts_value]
            if len(completed_counts) < 2 or any(value <= 0 for value in completed_counts):
                raise ValueError(
                    "drift comparison requires at least two positive completed-job counts"
                )
        window_limited_value = target.get("window_limited")
        window_limited = (
            None if window_limited_value is None else bool(window_limited_value)
        )
        spread_value = target.get("repetition_spread_percent")
        if completed_counts is None or window_limited is None or spread_value is None:
            comparison = "not-comparable-no-repetition-spread"
            exceeds: bool | None = None
            spread: float | None = None
        else:
            spread = float(spread_value)
            if spread < 0:
                raise ValueError("repetition spread must not be negative")
            if (window_limited and len(set(completed_counts)) == 1) or spread == 0:
                comparison = "indeterminate-zero-spread"
                exceeds = None
            else:
                exceeds = abs(shift_percent) > spread
                comparison = (
                    "exceeds-observed-spread" if exceeds else "within-observed-spread"
                )
        comparisons.append(
            {
                "label": label,
                "completed_counts": completed_counts,
                "window_limited": window_limited,
                "repetition_spread_percent": spread,
                "session_shift_exceeds_repetition_spread": exceeds,
                "comparison": comparison,
            }
        )

    return {
        "a1_completed_counts": list(a1_completed_counts),
        "a1_median_completed_count": median_count,
        "a2_completed_count": a2_completed_count,
        "completed_count_delta": delta,
        "signed_count_shift_percent": shift_percent,
        "level_shift_observed": delta != 0,
        "comparison_basis": (
            "formal completed-job counts compared with each measurement's observed "
            "repetition throughput spread"
        ),
        "point_spread_comparisons": comparisons,
    }


@dataclass
class JobObservation:
    client_id: int
    attempt: int
    request_started_s: float
    accepted_at_s: float
    terminal_at_s: float
    run_id: str
    status: str
    first_running_at_s: float | None = None
    error_code: str | None = None
    digest: str | None = None
    warmup: bool = False
    formal: bool = False
    correctness_checked: bool = False

    @property
    def end_to_end_seconds(self) -> float:
        return self.terminal_at_s - self.request_started_s

    @property
    def queue_seconds(self) -> float | None:
        if self.first_running_at_s is None:
            return None
        return self.first_running_at_s - self.accepted_at_s

    @property
    def compute_seconds(self) -> float | None:
        if self.first_running_at_s is None:
            return None
        return self.terminal_at_s - self.first_running_at_s

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.update(
            {
                "end_to_end_seconds": self.end_to_end_seconds,
                "queue_seconds": self.queue_seconds,
                "compute_seconds": self.compute_seconds,
            }
        )
        return payload


@dataclass(frozen=True)
class RejectionObservation:
    client_id: int
    attempted_at_s: float
    responded_at_s: float
    latency_seconds: float
    status_code: int
    error_code: str | None


@dataclass(frozen=True)
class HealthObservation:
    monotonic_s: float
    latency_seconds: float
    status_code: int | None
    error: str | None = None
    error_code: str | None = None
    pending_count: int | None = None
    oldest_wait_seconds: float | None = None
    response_body: Any | None = None


class CorrectnessMismatch(RuntimeError):
    """Raised after durable evidence is written for the first divergent result."""


class CorrectnessOracle:
    def __init__(self, evidence_dir: Path, input_sha256: str, config: str = "{}") -> None:
        self.evidence_dir = evidence_dir
        self.input_sha256 = input_sha256
        self.config = config
        self.reference_digest: str | None = None
        self.reference_run_id: str | None = None
        self._reference_result: dict[str, Any] | None = None
        self._reference_steps: str | None = None
        self.success_count = 0
        self.mismatch: dict[str, Any] | None = None

    def observe(self, run_id: str, result: dict[str, Any], steps: str) -> str:
        digest = stable_result_digest(result, steps)
        self.success_count += 1
        if self.reference_digest is None:
            self.reference_digest = digest
            self.reference_run_id = run_id
            self._reference_result = result
            self._reference_steps = steps
            return digest
        if digest == self.reference_digest:
            return digest

        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        assert self.reference_run_id is not None
        assert self._reference_result is not None
        assert self._reference_steps is not None
        reference = self.evidence_dir / f"reference-{self.reference_run_id}"
        offending = self.evidence_dir / f"offending-{run_id}"
        reference.mkdir(exist_ok=True)
        offending.mkdir(exist_ok=True)
        _atomic_json(reference / "result.json", self._reference_result)
        (reference / "gait_steps_analysis.csv").write_text(
            self._reference_steps, encoding="utf-8"
        )
        _atomic_json(offending / "result.json", result)
        (offending / "gait_steps_analysis.csv").write_text(steps, encoding="utf-8")
        self.mismatch = {
            "input_sha256": self.input_sha256,
            "config": self.config,
            "reference_run_id": self.reference_run_id,
            "reference_digest": self.reference_digest,
            "offending_run_id": run_id,
            "offending_digest": digest,
            "evidence_dir": str(self.evidence_dir),
        }
        _atomic_json(self.evidence_dir / "mismatch.json", self.mismatch)
        raise CorrectnessMismatch(
            f"result digest mismatch: {self.reference_run_id} != {run_id}"
        )

    def summary(self) -> dict[str, Any]:
        return {
            "input_sha256": self.input_sha256,
            "config": self.config,
            "successful_jobs_checked": self.success_count,
            "reference_digest": self.reference_digest,
            "all_equal": self.mismatch is None,
            "mismatch": self.mismatch,
        }


class MeasurementState:
    def __init__(self, *, window_seconds: float, target: int) -> None:
        self.window_seconds = window_seconds
        self.target = target
        self.all_jobs: list[JobObservation] = []
        self.formal_jobs: list[JobObservation] = []
        self.warmup_jobs: list[JobObservation] = []
        self.measurement_start_s: float | None = None
        self.measurement_end_s: float | None = None
        self.stop = asyncio.Event()
        self.fatal_error: str | None = None
        self.fatal_evidence: dict[str, Any] | None = None
        self.status_poll_anomalies: list[dict[str, Any]] = []

    def record(self, observation: JobObservation) -> None:
        self.all_jobs.append(observation)
        if self.measurement_start_s is None:
            observation.warmup = True
            self.warmup_jobs.append(observation)
            if len(self.warmup_jobs) == WARMUP_COMPLETIONS:
                self.measurement_start_s = observation.terminal_at_s
            return

        deadline = self.measurement_start_s + self.window_seconds
        if observation.terminal_at_s <= deadline and len(self.formal_jobs) < self.target:
            observation.formal = True
            self.formal_jobs.append(observation)
            if len(self.formal_jobs) == self.target:
                self.measurement_end_s = observation.terminal_at_s
                self.stop.set()
        elif observation.terminal_at_s > deadline:
            self.measurement_end_s = deadline
            self.stop.set()

    def expire_if_needed(self, now: float) -> None:
        if self.measurement_start_s is None:
            return
        deadline = self.measurement_start_s + self.window_seconds
        if now >= deadline and not self.stop.is_set():
            self.measurement_end_s = deadline
            self.stop.set()

    def fail(self, message: str) -> None:
        self.fatal_error = message
        if self.measurement_start_s is not None:
            self.measurement_end_s = min(
                time.monotonic(), self.measurement_start_s + self.window_seconds
            )
        self.stop.set()


class RunLogger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._handle = path.open("a", encoding="utf-8", buffering=1)

    def emit(self, event: str, **values: Any) -> None:
        payload = {"timestamp": datetime.now(UTC).isoformat(), "event": event, **values}
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        print(line, flush=True)
        self._handle.write(line + "\n")

    def close(self) -> None:
        self._handle.close()

    def flush(self) -> None:
        self._handle.flush()


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _tracebacks_from_logs(paths: list[Path]) -> str:
    sections: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        marker = text.find("Traceback")
        if marker >= 0:
            sections.append(f"===== {path.name} =====\n{text[marker:].rstrip()}\n")
    return "\n".join(sections) if sections else "No traceback recorded.\n"


def write_rejection_evidence(
    *,
    short_id: str,
    full_label: str,
    aggregate: dict[str, Any],
    repository_rejected_root: Path,
    external_artifact_root: Path,
    stdout_path: Path,
    service_log_paths: list[Path],
) -> dict[str, Any]:
    """Persist the small, reviewable half of one rejected measurement."""

    evidence_dir = repository_rejected_root / short_id
    evidence_dir.mkdir(parents=True, exist_ok=False)
    _atomic_json(evidence_dir / "aggregate.json", aggregate)
    if stdout_path.is_file():
        shutil.copy2(stdout_path, evidence_dir / "stdout.log")
    else:
        (evidence_dir / "stdout.log").write_text("", encoding="utf-8")
    combined_logs: list[str] = []
    for service_log in service_log_paths:
        if service_log.is_file():
            combined_logs.append(
                f"===== {service_log.name} =====\n"
                + service_log.read_text(encoding="utf-8", errors="replace").rstrip()
                + "\n"
            )
    (evidence_dir / "service.log").write_text(
        "\n".join(combined_logs) if combined_logs else "No service log recorded.\n",
        encoding="utf-8",
    )
    (evidence_dir / "traceback.txt").write_text(
        _tracebacks_from_logs(service_log_paths), encoding="utf-8"
    )
    file_rows: dict[str, dict[str, Any]] = {}
    for name in ("aggregate.json", "stdout.log", "service.log", "traceback.txt"):
        path = evidence_dir / name
        file_rows[name] = {"bytes": path.stat().st_size, "sha256": _sha256_path(path)}
    index = {
        "short_id": short_id,
        "full_label": full_label,
        "external_artifact_root": str(external_artifact_root.resolve(strict=True)),
        "repository_evidence_path": str(evidence_dir.resolve()),
        "files": file_rows,
    }
    _atomic_json(evidence_dir / "index.json", index)
    return {
        "repository_evidence_path": str(evidence_dir.resolve()),
        "external_artifact_root": index["external_artifact_root"],
    }


def _refresh_rejection_stdout(evidence_dirs: list[str], stdout_path: Path) -> None:
    if not stdout_path.is_file():
        return
    for raw_path in evidence_dirs:
        evidence_dir = Path(raw_path)
        if not evidence_dir.is_dir():
            continue
        shutil.copy2(stdout_path, evidence_dir / "stdout.log")
        index_path = evidence_dir / "index.json"
        if not index_path.is_file():
            continue
        index = json.loads(index_path.read_text(encoding="utf-8"))
        copied = evidence_dir / "stdout.log"
        index["files"]["stdout.log"] = {
            "bytes": copied.stat().st_size,
            "sha256": _sha256_path(copied),
        }
        _atomic_json(index_path, index)


def generate_fixture(n_samples: int, path: Path, *, seed: int = 7) -> Path:
    """Generate once before measurement using the established benchmark waveform."""

    rng = np.random.default_rng(seed)
    frequency = 100.0
    t = np.arange(n_samples) / frequency
    phase = np.mod(t, 1.1)
    profile = np.where(phase < 0.65, np.sin(np.pi * np.clip(phase / 0.65, 0, 1)), 0.0)
    channels = [
        np.clip(amplitude * profile + rng.normal(0, 2.0, n_samples), 0, None)
        for amplitude in (140.0, 260.0, 120.0, 180.0)
    ]
    acceleration = [
        rng.normal(0, 0.4, n_samples),
        rng.normal(0, 0.4, n_samples),
        9.81 + rng.normal(0, 0.5, n_samples),
    ]
    gyroscope = [rng.normal(0, 8, n_samples) for _ in range(3)]
    pitch = 6 * np.sin(2 * np.pi * t / 1.1) + rng.normal(0, 0.4, n_samples)
    roll = 2.5 * np.sin(2 * np.pi * t / 1.1 + 0.6) + rng.normal(0, 0.3, n_samples)
    yaw = np.cumsum(rng.normal(0, 0.01, n_samples))
    milliseconds = np.round(t * 1000).astype(np.int64)
    hours, remainder = np.divmod(milliseconds, 3_600_000)
    minutes, remainder = np.divmod(remainder, 60_000)
    seconds, millis = np.divmod(remainder, 1000)
    series = channels + acceleration + gyroscope + [pitch, roll, yaw]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("StepWise synthetic benchmark recording\n")
        handle.write(
            "Sample SystemTime P1 P2 P3 P4 AccX AccY AccZ GyrX GyrY GyrZ Pitch Roll Yaw\n"
        )
        for index in range(n_samples):
            values = " ".join(f"{column[index]:.2f}" for column in series)
            handle.write(
                f"{index} {hours[index]:02d}:{minutes[index]:02d}:"
                f"{seconds[index]:02d}.{millis[index]:03d} {values}\n"
            )
    return path


async def run_memory_calibration(
    *,
    sample_count: int,
    input_path: Path,
    session_dir: Path,
    session_work_root: Path,
    work_root: Path,
    repository_root: Path,
    logger: RunLogger,
    memcompression: MemCompressionSampler,
) -> dict[str, Any]:
    label = f"calibration-n{sample_count}"
    identifier = f"c{sample_count // 1000}-cal-{uuid.uuid4().hex[:4]}"
    data_root = session_work_root / identifier / "data"
    service = await start_service(
        data_root=data_root,
        log_path=session_dir / "service-logs" / f"{identifier}.log",
    )
    monitor = CalibrationMonitor(service, data_root, memcompression)
    monitor_task = asyncio.create_task(monitor.run())
    baseline = int(psutil.Process(os.getpid()).memory_info().rss)
    upload_started: float | None = None
    upload_ended: float | None = None
    terminal: dict[str, Any] | None = None
    failure: str | None = None
    run_id: str | None = None
    result_extraction: dict[str, Any] = {"attempted": False}
    health_state: MeasurementState | None = None
    health_observations: list[HealthObservation] = []
    try:
        monitor.phase = "upload"
        upload_started = time.monotonic()
        content = await asyncio.to_thread(input_path.read_bytes)
        async with (
            httpx.AsyncClient(base_url=service.base_url, timeout=180.0) as client,
            _health_watch(client) as (health_state, health_observations),
        ):
                response = await client.post(
                    "/api/v1/analyses",
                    files={"walking": ("walk.txt", content, "text/plain")},
                )
                upload_ended = time.monotonic()
                monitor.phase = "analysis"
                if response.status_code != 202:
                    failure = f"submission returned {response.status_code}: {response.text}"
                else:
                    run_id = str(response.json()["run_id"])
                    terminal = await _wait_terminal(
                        client, run_id, 180.0, health_state=health_state
                    )
                    if terminal.get("status") != "succeeded":
                        failure = f"calibration job did not succeed: {terminal}"
                    else:
                        monitor.phase = "result-extraction"
                        try:
                            result_response = await client.get(
                                f"/api/v1/analyses/{run_id}/result"
                            )
                            steps_response = await client.get(
                                f"/api/v1/analyses/{run_id}/artifacts/"
                                "gait_steps_analysis.csv"
                            )
                            result_extraction = {
                                "attempted": True,
                                "result_status": result_response.status_code,
                                "steps_status": steps_response.status_code,
                                "passed": (
                                    result_response.status_code == 200
                                    and steps_response.status_code == 200
                                ),
                            }
                        except httpx.HTTPError as exc:
                            result_extraction = {
                                "attempted": True,
                                "passed": False,
                                "error": f"{type(exc).__name__}: {exc}",
                            }
    except (OSError, httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        monitor.stop()
        await monitor_task
        await stop_service(service)

    upload_samples = [sample for sample in monitor.samples if sample.phase == "upload"]
    upload_peak = max(
        (sample.generator_rss_bytes for sample in upload_samples), default=baseline
    )
    parent_peak = max(
        (
            sample.service_parent_rss_bytes + sample.service_helper_rss_bytes
            for sample in monitor.samples
        ),
        default=0,
    )
    worker_peak = max(
        (sample.worker_rss_bytes for sample in monitor.samples), default=0
    )
    gate_samples = [
        ResourceSample(
            monotonic_s=sample.monotonic_s,
            available_memory_bytes=sample.available_memory_bytes,
            pagefile_used_bytes=sample.pagefile_used_bytes,
            disk_free_bytes=sample.disk_free_bytes,
            memcompression_pids=sample.memcompression_pids,
            memcompression_rss_bytes=sample.memcompression_rss_bytes,
            memcompression_error=sample.memcompression_error,
        )
        for sample in monitor.samples
    ]
    gate = classify_resource_gate(gate_samples)
    failure = calibration_failure_reason(
        job_failure=failure,
        upload_sample_count=len(upload_samples),
        parent_peak_rss_bytes=parent_peak,
        worker_peak_rss_bytes=worker_peak,
    )
    health_result = (
        _health_watch_result(health_state, health_observations)
        if health_state is not None
        else {
            "samples": [],
            "terminal_persistence": health_backlog_statistics([]),
            "fatal_503": None,
            "fatal_error": None,
            "abort_suite": False,
        }
    )
    abort_suite = bool(gate["disk_below_20_gib"] or health_result["abort_suite"])
    abort_reason = (
        str(health_result["fatal_error"])
        if health_result["fatal_error"]
        else ("disk-free-below-20-gib" if gate["disk_below_20_gib"] else None)
    )
    result: dict[str, Any] = {
        "sample_count": sample_count,
        "label": label,
        "path_id": identifier,
        "input_path": str(input_path),
        "input_bytes": input_path.stat().st_size,
        "run_id": run_id,
        "terminal": terminal,
        "result_extraction": result_extraction,
        "service": service.metadata(),
        "generator_baseline_rss_bytes": baseline,
        "generator_upload_peak_rss_bytes": upload_peak,
        "generator_upload_increment_peak_bytes": max(0, upload_peak - baseline),
        "upload_started_monotonic_s": upload_started,
        "upload_ended_monotonic_s": upload_ended,
        "upload_sample_interval_seconds": CALIBRATION_UPLOAD_INTERVAL_SECONDS,
        "other_sample_interval_seconds": CALIBRATION_INTERVAL_SECONDS,
        "upload_sample_count": len(upload_samples),
        "worker_peak_rss_bytes": worker_peak,
        "parent_component_peak_rss_bytes": parent_peak,
        "resource_gate": gate,
        "timing_memory_gate_applicable": False,
        "monitor_errors": monitor.errors,
        "samples": [asdict(sample) for sample in monitor.samples],
        "health": health_result,
        "failure": failure,
        "passed": failure is None,
        "abort_suite": abort_suite,
        "abort_reason": abort_reason,
    }
    logger.emit(
        "memory_calibration_end",
        sample_count=sample_count,
        passed=result["passed"],
        worker_peak_rss_bytes=worker_peak,
        parent_peak_rss_bytes=parent_peak,
        generator_upload_increment_peak_bytes=result[
            "generator_upload_increment_peak_bytes"
        ],
    )
    if result["passed"]:
        safe_rmtree(data_root.parent, work_root, repository_root)
    return result


async def _health_probe(
    client: httpx.AsyncClient,
    state: MeasurementState,
    output: list[HealthObservation],
) -> None:
    while not state.stop.is_set():
        started = time.monotonic()
        status_code: int | None = None
        error: str | None = None
        error_code: str | None = None
        pending_count: int | None = None
        oldest_wait_seconds: float | None = None
        response_body: Any | None = None
        try:
            response = await client.get("/healthz", timeout=2.0)
            status_code = response.status_code
            try:
                response_body = response.json()
            except ValueError:
                response_body = response.text
            if isinstance(response_body, dict):
                error_payload = response_body.get("error")
                if isinstance(error_payload, dict):
                    code = error_payload.get("code")
                    error_code = code if isinstance(code, str) else None
                persistence = response_body.get("terminal_persistence")
                if isinstance(persistence, dict):
                    count = persistence.get("pending_count")
                    wait = persistence.get("oldest_wait_seconds")
                    pending_count = count if isinstance(count, int) else None
                    oldest_wait_seconds = (
                        float(wait) if isinstance(wait, (int, float)) else None
                    )
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"
        ended = time.monotonic()
        observation = HealthObservation(
            started,
            ended - started,
            status_code,
            error,
            error_code,
            pending_count,
            oldest_wait_seconds,
            response_body,
        )
        output.append(observation)
        if status_code == 503:
            fatal_reason = (
                error_code
                if error_code
                in {"job_supervisor_unavailable", "terminal_persistence_saturated"}
                else "unexpected-health-503"
            )
            state.fatal_evidence = {
                "reason": fatal_reason,
                "observed_monotonic_s": started,
                "status_code": status_code,
                "error_code": error_code,
                "response_body": response_body,
            }
            state.fail(fatal_reason)
            return
        try:
            await asyncio.wait_for(state.stop.wait(), timeout=max(0.0, 1.0 - (ended - started)))
        except TimeoutError:
            pass


@asynccontextmanager
async def _health_watch(
    client: httpx.AsyncClient,
) -> AsyncIterator[tuple[MeasurementState, list[HealthObservation]]]:
    state = MeasurementState(window_seconds=24 * 60 * 60, target=sys.maxsize)
    observations: list[HealthObservation] = []
    task = asyncio.create_task(_health_probe(client, state, observations))
    try:
        yield state, observations
    finally:
        state.stop.set()
        await task


def _health_watch_result(
    state: MeasurementState, observations: list[HealthObservation]
) -> dict[str, Any]:
    return {
        "samples": [asdict(row) for row in observations],
        "terminal_persistence": health_backlog_statistics(observations),
        "fatal_503": state.fatal_evidence,
        "fatal_error": state.fatal_error,
        "abort_suite": state.fatal_error is not None,
    }


@dataclass
class RuntimeHealthWatch:
    client: httpx.AsyncClient
    state: MeasurementState
    observations: list[HealthObservation]
    task: asyncio.Task[None]

    @classmethod
    async def start(cls, base_url: str) -> Self:
        client = httpx.AsyncClient(base_url=base_url, timeout=2.0)
        state = MeasurementState(window_seconds=24 * 60 * 60, target=sys.maxsize)
        observations: list[HealthObservation] = []
        task = asyncio.create_task(_health_probe(client, state, observations))
        return cls(client, state, observations, task)

    async def stop(self) -> None:
        self.state.stop.set()
        await self.task
        await self.client.aclose()

    def result(self) -> dict[str, Any]:
        return _health_watch_result(self.state, self.observations)


async def _download_and_check(
    client: httpx.AsyncClient,
    observation: JobObservation,
    oracle: CorrectnessOracle,
) -> None:
    result_response = await client.get(f"/api/v1/analyses/{observation.run_id}/result")
    result_response.raise_for_status()
    steps_response = await client.get(
        f"/api/v1/analyses/{observation.run_id}/artifacts/gait_steps_analysis.csv"
    )
    steps_response.raise_for_status()
    result = result_response.json()
    if not isinstance(result, dict):
        raise TypeError("result endpoint did not return an object")
    observation.digest = oracle.observe(observation.run_id, result, steps_response.text)
    observation.correctness_checked = True


async def _load_client(
    *,
    client_id: int,
    client: httpx.AsyncClient,
    input_bytes: bytes,
    state: MeasurementState,
    oracle: CorrectnessOracle,
    rejections: list[RejectionObservation],
) -> None:
    attempt = 0
    while not state.stop.is_set():
        attempt += 1
        request_started = time.monotonic()
        try:
            response = await client.post(
                "/api/v1/analyses",
                files={"walking": ("walk.txt", input_bytes, "text/plain")},
                timeout=180.0,
            )
        except httpx.HTTPError as exc:
            state.fail(f"submission transport error: {type(exc).__name__}: {exc}")
            return
        accepted_at = time.monotonic()
        if response.status_code == 429:
            payload = response.json()
            rejections.append(
                RejectionObservation(
                    client_id,
                    request_started,
                    accepted_at,
                    accepted_at - request_started,
                    429,
                    payload.get("error", {}).get("code"),
                )
            )
            try:
                await asyncio.wait_for(state.stop.wait(), timeout=RETRY_429_SECONDS)
            except TimeoutError:
                pass
            continue
        if response.status_code == 503:
            try:
                payload = response.json()
            except ValueError:
                payload = response.text
            code = (
                payload.get("error", {}).get("code")
                if isinstance(payload, dict)
                and isinstance(payload.get("error"), dict)
                else None
            )
            fatal_reason = (
                code
                if code
                in {"job_supervisor_unavailable", "terminal_persistence_saturated"}
                else "unexpected-submission-503"
            )
            state.fatal_evidence = {
                "reason": fatal_reason,
                "source": "submission",
                "observed_monotonic_s": accepted_at,
                "status_code": 503,
                "error_code": code,
                "response_body": payload,
            }
            state.fail(str(fatal_reason))
            return
        if response.status_code != 202:
            state.fail(f"unexpected submission status {response.status_code}: {response.text}")
            return
        created = response.json()
        run_id = str(created["run_id"])
        first_running: float | None = None
        terminal_payload: dict[str, Any] | None = None
        terminal_at = accepted_at
        status_anomaly_count = 0
        while not state.stop.is_set() or terminal_payload is None:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            try:
                status_response = await client.get(f"/api/v1/analyses/{run_id}", timeout=5.0)
            except httpx.HTTPError as exc:
                state.fail(f"poll transport error: {type(exc).__name__}: {exc}")
                return
            observed = time.monotonic()
            if status_response.status_code != 200:
                status_anomaly_count += 1
                state.status_poll_anomalies.append(
                    {
                        "run_id": run_id,
                        "client_id": client_id,
                        "attempt": attempt,
                        "observed_monotonic_s": observed,
                        "status_code": status_response.status_code,
                        "response": status_response.text,
                        "retry_number_for_job": status_anomaly_count,
                    }
                )
                if _status_poll_retry_allowed(
                    status_response.status_code, status_anomaly_count
                ):
                    continue
                state.fail(f"status endpoint returned {status_response.status_code} for {run_id}")
                return
            payload = status_response.json()
            status = payload.get("status")
            if status == "running" and first_running is None:
                first_running = observed
            if status in {"succeeded", "failed"}:
                terminal_payload = payload
                terminal_at = observed
                break
        assert terminal_payload is not None
        observation = JobObservation(
            client_id=client_id,
            attempt=attempt,
            request_started_s=request_started,
            accepted_at_s=accepted_at,
            terminal_at_s=terminal_at,
            run_id=run_id,
            status=str(terminal_payload["status"]),
            first_running_at_s=first_running,
            error_code=(terminal_payload.get("error") or {}).get("code"),
        )
        if observation.status == "succeeded":
            try:
                await _download_and_check(client, observation, oracle)
            except CorrectnessMismatch as exc:
                state.fail(str(exc))
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                state.fail(f"correctness extraction failed for {run_id}: {type(exc).__name__}: {exc}")
        state.record(observation)
        if state.fatal_error is not None:
            return


async def _measurement_watchdog(state: MeasurementState, monitor: ResourceMonitor) -> None:
    while not state.stop.is_set():
        state.expire_if_needed(time.monotonic())
        if monitor.samples and monitor.samples[-1].disk_free_bytes < MIN_DISK_FREE_BYTES:
            state.fail("disk free space fell below 20 GiB")
            return
        await asyncio.sleep(0.05)


async def _cancel_clients_on_fatal(
    state: MeasurementState, clients: list[asyncio.Task[None]]
) -> None:
    await state.stop.wait()
    if state.fatal_error is not None:
        for task in clients:
            if not task.done():
                task.cancel()


def _disk_probe(directory: Path) -> dict[str, Any]:
    path = directory / f"disk-probe-{uuid.uuid4()}.bin"
    try:
        content = os.urandom(MiB)
        with path.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        matched = path.read_bytes() == content
        return {"passed": matched, "error": None if matched else "content_mismatch"}
    except OSError as exc:
        return {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        path.unlink(missing_ok=True)


def _stats_for_jobs(jobs: list[JobObservation], *, raw_only: bool) -> dict[str, Any]:
    end_to_end = [job.end_to_end_seconds for job in jobs]
    queue_values = [value for job in jobs if (value := job.queue_seconds) is not None]
    compute_values = [value for job in jobs if (value := job.compute_seconds) is not None]
    errors: dict[str, int] = {}
    for job in jobs:
        if job.status != "succeeded":
            key = job.error_code or job.status
            errors[key] = errors.get(key, 0) + 1
    if raw_only:
        return {
            "n": len(jobs),
            "end_to_end_raw_seconds": end_to_end,
            "end_to_end_median_seconds": statistics.median(end_to_end) if end_to_end else None,
            "queue_raw_seconds": queue_values,
            "compute_raw_seconds": compute_values,
            "split_missing_n": len(jobs) - len(queue_values),
            "errors": errors,
        }
    return {
        "n": len(jobs),
        "end_to_end_seconds": latency_statistics(end_to_end),
        "queue_seconds": latency_statistics(queue_values),
        "compute_seconds": latency_statistics(compute_values),
        "split_missing_n": len(jobs) - len(queue_values),
        "errors": errors,
    }


def health_backlog_statistics(rows: list[HealthObservation]) -> dict[str, Any]:
    pending = [float(row.pending_count) for row in rows if row.pending_count is not None]
    waits = [
        0.0 if row.oldest_wait_seconds is None else float(row.oldest_wait_seconds)
        for row in rows
        if row.pending_count is not None
    ]

    def field(values: list[float]) -> dict[str, float | int | None]:
        if not values:
            return {"n": 0, "median": None, "p95": None, "max": None}
        return {
            "n": len(values),
            "median": statistics.median(values),
            "p95": _nearest_rank(values, 0.95),
            "max": max(values),
        }

    ge_one = sum(value >= 1 for value in pending)
    ge_three = sum(value >= 3 for value in pending)
    crossings = sum(
        previous < 3 <= current for previous, current in itertools.pairwise(pending)
    )
    return {
        "n": len(rows),
        "telemetry_n": len(pending),
        "missing_telemetry_n": len(rows) - len(pending),
        "pending_count": field(pending),
        "oldest_wait_seconds": field(waits),
        "pending_count_ge_1_samples": ge_one,
        "pending_count_ge_3_samples": ge_three,
        "upward_crossings_to_ge_3": crossings,
        "terminal_persistence_growth_observed": ge_three >= 2,
    }


def _repetition_summary(
    *,
    label: str,
    sample_count: int,
    concurrency: int,
    window_seconds: float,
    state: MeasurementState,
    rejections: list[RejectionObservation],
    health: list[HealthObservation],
    resources: list[ResourceSample],
    service: ServiceHandle,
    disk_before: int,
    disk_after: int,
    oracle: CorrectnessOracle,
) -> dict[str, Any]:
    start = state.measurement_start_s
    end = state.measurement_end_s
    if start is not None and end is None:
        end = min(time.monotonic(), start + window_seconds)
    duration = (end - start) if start is not None and end is not None else 0.0
    formal = state.formal_jobs
    intervals = [(job.accepted_at_s, job.terminal_at_s) for job in state.all_jobs]
    rejection_window = [
        row
        for row in rejections
        if start is not None and end is not None and start <= row.responded_at_s <= end
    ]
    health_window = [
        row for row in health if start is not None and end is not None and start <= row.monotonic_s <= end
    ]
    resource_window = [
        row for row in resources if start is not None and end is not None and start <= row.monotonic_s <= end
    ]
    resource_stats = resource_statistics(resource_window)
    backlog_stats = health_backlog_statistics(health_window)
    failures = [job for job in formal if job.status == "failed"]
    disk_probe = _disk_probe(service.data_root) if any(
        job.error_code == "analysis_failed" for job in failures
    ) else None
    log_text = service.log_path.read_text(encoding="utf-8", errors="replace")
    disk_evidence = bool(
        resource_stats.get("disk_below_20_gib")
        or (disk_probe is not None and not disk_probe["passed"])
        or any(token in log_text.lower() for token in ("enospc", "no space left", "disk i/o"))
    )
    invalid_reasons: list[str] = []
    if resource_stats.get("memory_pressure"):
        invalid_reasons.append("memory-displacement-pressure")
    if resource_stats.get("generator_limited"):
        invalid_reasons.append("generator-limited")
    if disk_evidence:
        invalid_reasons.append("environmental-disk-failure")
    if state.fatal_error:
        invalid_reasons.append("fatal-error")
    if backlog_stats["terminal_persistence_growth_observed"]:
        invalid_reasons.append("terminal-persistence-growth-observed")
    warnings: list[str] = []
    if resource_stats.get("low_available_without_displacement_evidence"):
        warnings.append("low-available-memory-without-displacement-evidence")
    accepted_run_dirs = len(
        [path for path in service.data_root.iterdir() if path.is_dir() and path.name != ".staging"]
    )
    return {
        "label": label,
        "sample_count": sample_count,
        "concurrency": concurrency,
        "window_limit_seconds": window_seconds,
        "window_actual_seconds": duration,
        "warmup_n": len(state.warmup_jobs),
        "formal": _stats_for_jobs(formal, raw_only=sample_count == 360_000),
        "throughput_jobs_per_minute": len(formal) / duration * 60.0 if duration > 0 else 0.0,
        "rejections_429": {
            "count": len(rejection_window),
            "latency_seconds": latency_statistics(
                [row.latency_seconds for row in rejection_window]
            ),
            "retry_seconds": RETRY_429_SECONDS,
            "strategy_dependent": True,
            "not_a_rate": True,
            "server_residual_check": {
                "accepted_jobs": len(state.all_jobs),
                "run_directories": accepted_run_dirs,
                "passed": accepted_run_dirs == len(state.all_jobs),
            },
        },
        "nominal_concurrency": concurrency,
        "average_effective_concurrency": (
            effective_concurrency(intervals, window_start=start, window_end=end)
            if start is not None and end is not None and end > start
            else None
        ),
        "throughput_context": "measured under fixed 500 ms retry and stated effective concurrency",
        "health_seconds": latency_statistics(
            [row.latency_seconds for row in health_window if row.status_code == 200]
        ),
        "health_errors": [asdict(row) for row in health_window if row.status_code != 200],
        "terminal_persistence": backlog_stats,
        "fatal_503": state.fatal_evidence,
        "status_poll_anomalies": state.status_poll_anomalies,
        "status_poll_500_count": sum(
            row["status_code"] == 500 for row in state.status_poll_anomalies
        ),
        "resources": resource_stats,
        "resource_samples": [asdict(row) for row in resource_window],
        "disk_free_before_bytes": disk_before,
        "disk_free_after_bytes": disk_after,
        "disk_probe": disk_probe,
        "disk_failure_evidence": disk_evidence,
        "service": service.metadata(),
        "jobs": [job.to_dict() for job in state.all_jobs],
        "rejection_observations": [asdict(row) for row in rejection_window],
        "correctness": oracle.summary(),
        "fatal_error": state.fatal_error,
        "invalid_reasons": invalid_reasons,
        "warnings": warnings,
        "citable": not invalid_reasons,
        "abort_suite": (
            bool(resource_stats.get("abort_suite"))
            or oracle.mismatch is not None
            or state.fatal_error is not None
        ),
    }


async def run_repetition(
    *,
    label: str,
    sample_count: int,
    concurrency: int,
    window_seconds: float,
    target: int,
    input_path: Path,
    session_dir: Path,
    session_work_root: Path,
    path_id_prefix: str,
    oracle: CorrectnessOracle,
    logger: RunLogger,
    memcompression: MemCompressionSampler,
) -> dict[str, Any]:
    repetition_id = f"{path_id_prefix}-{uuid.uuid4().hex[:4]}"
    session_work_root.mkdir(parents=True, exist_ok=True)
    data_root = session_work_root / repetition_id / "data"
    log_path = session_dir / "service-logs" / f"{repetition_id}.log"
    disk_before = int(psutil.disk_usage(str(session_work_root)).free)
    logger.emit(
        "repetition_start",
        label=label,
        sample_count=sample_count,
        concurrency=concurrency,
        disk_free_bytes=disk_before,
    )
    service = await start_service(data_root=data_root, log_path=log_path)
    monitor = ResourceMonitor(service, data_root, memcompression)
    state = MeasurementState(window_seconds=window_seconds, target=target)
    health: list[HealthObservation] = []
    rejections: list[RejectionObservation] = []
    monitor_task = asyncio.create_task(monitor.run())
    limits = httpx.Limits(max_connections=concurrency + 4, max_keepalive_connections=concurrency + 2)
    try:
        input_bytes = input_path.read_bytes()
        async with httpx.AsyncClient(base_url=service.base_url, limits=limits) as client:
            health_task = asyncio.create_task(_health_probe(client, state, health))
            watchdog = asyncio.create_task(_measurement_watchdog(state, monitor))
            clients = [
                asyncio.create_task(
                    _load_client(
                        client_id=client_id,
                        client=client,
                        input_bytes=input_bytes,
                        state=state,
                        oracle=oracle,
                        rejections=rejections,
                    )
                )
                for client_id in range(concurrency)
            ]
            fatal_canceller = asyncio.create_task(_cancel_clients_on_fatal(state, clients))
            await asyncio.gather(*clients, return_exceptions=True)
            state.stop.set()
            await asyncio.gather(health_task, watchdog, fatal_canceller)
    finally:
        monitor.stop()
        await monitor_task
        await stop_service(service)
    disk_after = int(psutil.disk_usage(str(data_root)).free)
    result = _repetition_summary(
        label=label,
        sample_count=sample_count,
        concurrency=concurrency,
        window_seconds=window_seconds,
        state=state,
        rejections=rejections,
        health=health,
        resources=monitor.samples,
        service=service,
        disk_before=disk_before,
        disk_after=disk_after,
        oracle=oracle,
    )
    logger.emit(
        "repetition_end",
        label=label,
        pid=service.pid,
        exit_code=service.exit_code,
        formal_n=len(state.formal_jobs),
        throughput_jobs_per_minute=result["throughput_jobs_per_minute"],
        citable=result["citable"],
    )
    return result


def aggregate_point(repetitions: list[dict[str, Any]]) -> dict[str, Any]:
    if not repetitions:
        raise ValueError("a point requires at least one repetition")
    throughputs = [float(row["throughput_jobs_per_minute"]) for row in repetitions]
    median = statistics.median(throughputs)
    spread = (max(throughputs) - min(throughputs)) / median * 100.0 if median > 0 else math.inf
    invalid_reasons = sorted(
        {reason for row in repetitions for reason in row.get("invalid_reasons", [])}
    )
    if spread > 20.0:
        invalid_reasons.append("throughput-spread-over-20-percent")
    policies = [
        row["formal"].get("end_to_end_seconds", {}).get("quantile", "raw-values")
        for row in repetitions
    ]
    return {
        "sample_count": repetitions[0]["sample_count"],
        "concurrency": repetitions[0]["concurrency"],
        "repetition_count": len(repetitions),
        "throughputs_jobs_per_minute": throughputs,
        "throughput_median_jobs_per_minute": median,
        "throughput_spread_percent": spread,
        "latency_quantile_summary": (
            policies[0] if len(set(policies)) == 1 else "mixed; see repetitions"
        ),
        "terminal_persistence_growth_observed": any(
            bool(row.get("terminal_persistence", {}).get("terminal_persistence_growth_observed"))
            for row in repetitions
        ),
        "invalid_reasons": invalid_reasons,
        "citable": not invalid_reasons,
        "repetitions": repetitions,
    }


def _archive_or_clean_result(
    result: dict[str, Any],
    *,
    citable: bool,
    full_label: str,
    aggregate: dict[str, Any],
    work_root: Path,
    external_rejected_root: Path,
    repository_rejected_root: Path,
    repository_root: Path,
    stdout_path: Path,
) -> str | None:
    source = Path(result["service"]["data_root"])
    if not source.exists():
        return None
    tree = source.parent
    short_id = tree.name
    if citable:
        safe_rmtree(tree, work_root, repository_root)
        result["service"]["data_retained"] = False
        return None
    destination = external_rejected_root / short_id
    retained = safe_move_tree(
        tree,
        destination,
        work_root,
        external_rejected_root,
        repository_root,
    )
    result["service"]["data_retained"] = True
    result["service"]["retained_path"] = str(retained)
    evidence = write_rejection_evidence(
        short_id=short_id,
        full_label=full_label,
        aggregate=aggregate,
        repository_rejected_root=repository_rejected_root,
        external_artifact_root=retained,
        stdout_path=stdout_path,
        service_log_paths=[Path(result["service"]["log_path"])],
    )
    result["service"]["repository_evidence_path"] = evidence[
        "repository_evidence_path"
    ]
    return str(evidence["repository_evidence_path"])


def _archive_or_clean_point(
    point: dict[str, Any],
    *,
    work_root: Path,
    external_rejected_root: Path,
    repository_rejected_root: Path,
    repository_root: Path,
    stdout_path: Path,
) -> list[str]:
    evidence_dirs: list[str] = []
    for repetition in point["repetitions"]:
        evidence = _archive_or_clean_result(
            repetition,
            citable=bool(point["citable"]),
            full_label=str(repetition["label"]),
            aggregate=point,
            work_root=work_root,
            external_rejected_root=external_rejected_root,
            repository_rejected_root=repository_rejected_root,
            repository_root=repository_root,
            stdout_path=stdout_path,
        )
        if evidence is not None:
            evidence_dirs.append(evidence)
    return evidence_dirs


def _remove_empty_parents(root: Path, work_root: Path, repository_root: Path) -> None:
    if not root.exists():
        return
    resolved_root, _ = _strict_descendant(root, work_root, repository_root)
    for path in sorted(resolved_root.rglob("*"), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass


async def _wait_terminal(
    client: httpx.AsyncClient,
    run_id: str,
    timeout: float,
    *,
    health_state: MeasurementState | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if health_state is not None and health_state.fatal_error is not None:
            return {
                "run_id": run_id,
                "status": "service_unavailable",
                "error": health_state.fatal_evidence,
            }
        response = await client.get(f"/api/v1/analyses/{run_id}")
        if response.status_code == 404:
            return {"run_id": run_id, "status": "not_found"}
        response.raise_for_status()
        payload = response.json()
        if payload["status"] in {"succeeded", "failed"}:
            return payload
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    return {"run_id": run_id, "status": "client_timeout"}


def _fault_measurement_context(
    monitor: ResourceMonitor, health: dict[str, Any]
) -> dict[str, Any]:
    resources = resource_statistics(monitor.samples)
    invalid_reasons: list[str] = []
    if resources["memory_pressure"]:
        invalid_reasons.append("memory-displacement-pressure")
    if resources["generator_limited"]:
        invalid_reasons.append("generator-limited")
    if resources["disk_below_20_gib"]:
        invalid_reasons.append("environmental-disk-failure")
    backlog = health.get("terminal_persistence", {})
    if backlog.get("terminal_persistence_growth_observed"):
        invalid_reasons.append("terminal-persistence-growth-observed")
    warnings = (
        ["low-available-memory-without-displacement-evidence"]
        if resources["low_available_without_displacement_evidence"]
        else []
    )
    return {
        "resources": resources,
        "resource_samples": [asdict(sample) for sample in monitor.samples],
        "invalid_reasons": invalid_reasons,
        "warnings": warnings,
        "citable": not invalid_reasons,
    }


async def run_timeout_fault(
    input_path: Path,
    session_dir: Path,
    session_work_root: Path,
    logger: RunLogger,
    memcompression: MemCompressionSampler,
) -> dict[str, Any]:
    identifier = f"f-time-{uuid.uuid4().hex[:4]}"
    service = await start_service(
        data_root=session_work_root / identifier / "data",
        log_path=session_dir / "service-logs" / f"{identifier}.log",
        runner_mode="slow",
    )
    health_watch = await RuntimeHealthWatch.start(service.base_url)
    monitor = ResourceMonitor(service, service.data_root, memcompression)
    monitor_task = asyncio.create_task(monitor.run())
    process_children_before = set(service.observed_worker_pids)
    try:
        content = input_path.read_bytes()
        async with httpx.AsyncClient(base_url=service.base_url, timeout=180.0) as client:
            slow_response = await client.post(
                "/api/v1/analyses",
                files={"walking": ("walk.txt", content, "text/plain")},
                data={"sensor_mapping": json.dumps({"pitch_eversion_sign": "negative"})},
            )
            slow_response.raise_for_status()
            slow_id = str(slow_response.json()["run_id"])
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                status = (await client.get(f"/api/v1/analyses/{slow_id}")).json()["status"]
                if status == "running":
                    break
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
            normal_ids: list[str] = []
            for _ in range(9):
                response = await client.post(
                    "/api/v1/analyses",
                    files={"walking": ("walk.txt", content, "text/plain")},
                )
                if response.status_code == 202:
                    normal_ids.append(str(response.json()["run_id"]))
            slow_terminal, *normal_terminal = await asyncio.gather(
                _wait_terminal(
                    client, slow_id, 150.0, health_state=health_watch.state
                ),
                *(
                    _wait_terminal(
                        client, run_id, 150.0, health_state=health_watch.state
                    )
                    for run_id in normal_ids
                ),
            )
    finally:
        await health_watch.stop()
        monitor.stop()
        await monitor_task
        await stop_service(service)
    survivors = [pid for pid in service.observed_worker_pids if psutil.pid_exists(pid)]
    result: dict[str, Any] = {
        "service": service.metadata(),
        "slow": slow_terminal,
        "normal": normal_terminal,
        "normal_accepted_n": len(normal_ids),
        "normal_succeeded_n": sum(row.get("status") == "succeeded" for row in normal_terminal),
        "timeout_isolated": (
            slow_terminal.get("error", {}).get("code") == "analysis_timeout"
            and all(row.get("status") == "succeeded" for row in normal_terminal)
        ),
        "residual_worker_pids": survivors,
        "process_children_before": sorted(process_children_before),
        "health": health_watch.result(),
    }
    result["abort_suite"] = bool(result["health"]["abort_suite"])
    result["fatal_error"] = result["health"]["fatal_error"]
    context = _fault_measurement_context(monitor, result["health"])
    result.update(context)
    result["abort_suite"] = bool(
        result["abort_suite"] or result["resources"]["abort_suite"]
    )
    result["passed"] = bool(result["timeout_isolated"] and not survivors)
    result["citable"] = bool(
        result["passed"] and not result["invalid_reasons"] and not result["abort_suite"]
    )
    logger.emit("timeout_fault_end", passed=result["passed"])
    return result


async def run_ttl_fault(
    input_path: Path,
    session_dir: Path,
    session_work_root: Path,
    logger: RunLogger,
    memcompression: MemCompressionSampler,
) -> dict[str, Any]:
    identifier = f"f-ttl-{uuid.uuid4().hex[:4]}"
    service = await start_service(
        data_root=session_work_root / identifier / "data",
        log_path=session_dir / "service-logs" / f"{identifier}.log",
        runner_mode="ttl",
        ttl_hours=0.001,
    )
    health_watch = await RuntimeHealthWatch.start(service.base_url)
    monitor = ResourceMonitor(service, service.data_root, memcompression)
    monitor_task = asyncio.create_task(monitor.run())
    expected_404 = 0
    preterminal_404 = 0
    terminal_rows: list[dict[str, Any]] = []
    started = time.monotonic()
    try:
        content = input_path.read_bytes()
        async with httpx.AsyncClient(base_url=service.base_url, timeout=30.0) as client:
            for _ in range(100):
                if (
                    time.monotonic() - started >= 120
                    or health_watch.state.fatal_error is not None
                ):
                    break
                response = await client.post(
                    "/api/v1/analyses",
                    files={"walking": ("walk.txt", content, "text/plain")},
                )
                if response.status_code == 429:
                    await asyncio.sleep(RETRY_429_SECONDS)
                    continue
                response.raise_for_status()
                run_id = str(response.json()["run_id"])
                terminal = await _wait_terminal(
                    client, run_id, 30.0, health_state=health_watch.state
                )
                if terminal["status"] == "not_found":
                    preterminal_404 += 1
                    continue
                terminal_rows.append(terminal)
                await asyncio.sleep(3.8)
                expired = await client.get(f"/api/v1/analyses/{run_id}")
                if expired.status_code == 404:
                    expected_404 += 1
    finally:
        await health_watch.stop()
        monitor.stop()
        await monitor_task
        await stop_service(service)
    log_text = service.log_path.read_text(encoding="utf-8", errors="replace")
    race_markers = [
        line
        for line in log_text.splitlines()
        if "BENCH_TTL_CLEANUP_EXCEPTION" in line or "Traceback" in line
    ]
    result: dict[str, Any] = {
        "service": service.metadata(),
        "submitted_terminal_n": len(terminal_rows),
        "expected_expiry_404_n": expected_404,
        "preterminal_manifest_404_n": preterminal_404,
        "manifest_race_log_markers": race_markers,
        "expected_404_separate_from_manifest_race": True,
        "health": health_watch.result(),
    }
    result["abort_suite"] = bool(result["health"]["abort_suite"])
    result["fatal_error"] = result["health"]["fatal_error"]
    context = _fault_measurement_context(monitor, result["health"])
    result.update(context)
    result["abort_suite"] = bool(
        result["abort_suite"] or result["resources"]["abort_suite"]
    )
    result["passed"] = bool(preterminal_404 == 0 and not race_markers)
    result["citable"] = bool(
        result["passed"] and not result["invalid_reasons"] and not result["abort_suite"]
    )
    logger.emit("ttl_fault_end", passed=result["passed"], expected_404=expected_404)
    return result


async def run_contract_faults(
    input_path: Path,
    session_dir: Path,
    session_work_root: Path,
    logger: RunLogger,
    memcompression: MemCompressionSampler,
) -> dict[str, Any]:
    identifier = f"f-http-{uuid.uuid4().hex[:4]}"
    service = await start_service(
        data_root=session_work_root / identifier / "data",
        log_path=session_dir / "service-logs" / f"{identifier}.log",
    )
    health_watch = await RuntimeHealthWatch.start(service.base_url)
    monitor = ResourceMonitor(service, service.data_root, memcompression)
    monitor_task = asyncio.create_task(monitor.run())
    try:
        content = input_path.read_bytes()
        async with httpx.AsyncClient(base_url=service.base_url, timeout=30.0) as client:
            invalid = await client.post(
                "/api/v1/analyses",
                files={"walking": ("walk.txt", content, "text/plain")},
                data={"sensor_mapping": "not-json"},
            )
            missing = await client.get(
                "/api/v1/analyses/00000000-0000-0000-0000-000000000000"
            )
            created = await client.post(
                "/api/v1/analyses", files={"walking": ("walk.txt", content, "text/plain")}
            )
            created.raise_for_status()
            run_id = str(created.json()["run_id"])
            pending_result = await client.get(f"/api/v1/analyses/{run_id}/result")
            oversized = await client.post(
                "/api/v1/analyses",
                files={"walking": ("oversized.txt", b"x" * (64 * MiB + 1), "text/plain")},
                timeout=60.0,
            )
            terminal = await _wait_terminal(
                client, run_id, 30.0, health_state=health_watch.state
            )
    finally:
        await health_watch.stop()
        monitor.stop()
        await monitor_task
        await stop_service(service)
    result: dict[str, Any] = {
        "service": service.metadata(),
        "422": {"status": invalid.status_code, "body": invalid.json()},
        "404": {"status": missing.status_code, "body": missing.json()},
        "409": {"status": pending_result.status_code, "body": pending_result.json()},
        "413": {"status": oversized.status_code, "body": oversized.json()},
        "control_terminal": terminal,
        "health": health_watch.result(),
    }
    result["abort_suite"] = bool(result["health"]["abort_suite"])
    result["fatal_error"] = result["health"]["fatal_error"]
    context = _fault_measurement_context(monitor, result["health"])
    result.update(context)
    result["abort_suite"] = bool(
        result["abort_suite"] or result["resources"]["abort_suite"]
    )
    result["passed"] = bool(
        all(result[code]["status"] == int(code) for code in ("422", "404", "409", "413"))
    )
    result["citable"] = bool(
        result["passed"] and not result["invalid_reasons"] and not result["abort_suite"]
    )
    logger.emit("contract_faults_end", passed=result["passed"])
    return result


def _format_latency_summary(stats: dict[str, Any]) -> str:
    if "end_to_end_raw_seconds" in stats:
        raw = stats["end_to_end_raw_seconds"]
        return (
            f"raw={json.dumps(raw, separators=(',', ':'))}; "
            f"median={stats['end_to_end_median_seconds']}; n={stats['n']}"
        )
    if stats.get("n", 0) == 0:
        return "n=0"
    if stats.get("quantile") == "p95":
        return f"n={stats['n']}; p50={stats['p50']}; p95={stats['p95']}"
    return (
        f"n={stats['n']}; p50={stats['p50']}; p90={stats['p90']}; "
        f"max={stats['max']}"
    )


def _format_memory_evidence(resources: dict[str, Any]) -> tuple[str, str, str, str, str]:
    memcompression_growth = resources.get("memcompression_growth_bytes")
    memcompression_text = (
        str(memcompression_growth)
        if memcompression_growth is not None
        else "unmeasured"
    )
    note = (
        "低可用内存，无置换证据"
        if resources.get("low_available_without_displacement_evidence")
        else str(resources.get("memcompression_measurement_note", ""))
    )
    return (
        str(resources.get("available_memory_min_bytes")),
        str(resources.get("pagefile_growth_bytes")),
        memcompression_text,
        str(resources.get("memory_displacement_bytes")),
        note,
    )


def _report_markdown(results: dict[str, Any]) -> str:
    lines = [
        "# StepWise Concurrent Load Test",
        "",
        f"- Session: `{results['session_id']}`",
        f"- Source commit: `{results['source_commit']}`",
        "- Model: closed loop; 50 ms polling; client and service share the recorded machine.",
        "- 429 counts are functions of concurrency, upload/validation time, and the fixed 500 ms retry. They are not rejection rates.",
        "- No p99 is computed or reported.",
        "",
        "## Environment",
        "",
        f"Preflight passed: **{results['preflight']['passed']}**",
        "",
    ]
    storage = results.get("storage", {})
    if storage.get("work_root"):
        lines.extend(
            [
                f"- Actual work root: `{storage['work_root']}`",
                f"- Actual rejected root: `{storage['rejected_root']}`",
                f"- Session work root: `{storage['session_work_root']}`",
                (
                    f"- Path budget: `{storage['path_budget']['max_chars']}` / "
                    f"`{storage['path_budget']['limit_chars']}` characters"
                ),
                f"- Root selection attempts: `{json.dumps(storage['selection_attempts'], ensure_ascii=False, sort_keys=True)}`",
                "",
            ]
        )
    elif storage.get("selection_failure"):
        lines.extend([f"- Work-root selection failure: `{storage['selection_failure']}`", ""])
    if not results["preflight"]["passed"]:
        lines.extend(
            [
                "The load suite did not start because the preflight gate failed.",
                "",
                f"Failures: `{', '.join(results['preflight']['failures'])}`",
                "",
            ]
        )
        return "\n".join(lines)
    calibrations = results.get("memory_calibrations", {})
    thresholds = results.get("memory_thresholds", {})
    decision = results.get("memory_gate_decision", {})
    lines.extend(["## Measured memory gates", ""])
    for key in ("30000", "360000"):
        calibration = calibrations.get(key)
        threshold = thresholds.get(key)
        if calibration is None or threshold is None:
            continue
        lines.extend(
            [
                f"### {key} samples",
                "",
                f"- Input bytes: `{calibration['input_bytes']}`",
                (
                    f"- Single-request generator RSS increment peak: "
                    f"`{calibration['generator_upload_increment_peak_bytes']}` bytes"
                ),
                (
                    "- Calibration memory observation (not a timing gate): "
                    f"available min `{calibration['resource_gate'].get('available_memory_min_bytes')}`, "
                    f"pagefile delta `{calibration['resource_gate'].get('pagefile_growth_bytes')}`, "
                    f"MemCompression delta `{calibration['resource_gate'].get('memcompression_growth_bytes')}`, "
                    f"displacement `{calibration['resource_gate'].get('memory_displacement_bytes')}` bytes"
                ),
                f"- Formula: `{threshold['formula']}` bytes",
                "",
            ]
        )
    if decision:
        lines.extend(
            [
                (
                    f"Available samples (one second apart): "
                    f"`{[row['available_bytes'] for row in decision['available_samples']]}`"
                ),
                f"; gate value (minimum): `{decision['available_bytes']}` bytes.",
                "",
            ]
        )
    if results.get("skipped_points"):
        lines.extend(["Skipped subsets:", ""])
        for row in results["skipped_points"]:
            lines.append(
                f"- `{row['subset']}`: available `{row['available_bytes']}`, "
                f"threshold `{row['threshold_bytes']}`, deficit `{row['deficit_bytes']}` bytes; "
                f"formula `{row['formula']}`."
            )
        lines.append("")
    lines.extend(
        [
            "## Matrix",
            "",
            "| samples | C | n by repetition | throughput median/min | spread | A2 shift vs spread | citable | quantile policy |",
            "|---:|---:|---|---:|---:|---|---|---|",
        ]
    )
    for point in results.get("points", []):
        counts = ", ".join(str(rep["formal"]["n"]) for rep in point["repetitions"])
        lines.append(
            f"| {point['sample_count']} | {point['concurrency']} | {counts} | "
            f"{point['throughput_median_jobs_per_minute']:.6g} | "
            f"{point['throughput_spread_percent']:.3f}% | "
            f"{point.get('session_shift_comparison', 'not-evaluated')} | "
            f"{point['citable']} | "
            f"{point['latency_quantile_summary']} |"
        )
    lines.extend(["", "## Repetition details", ""])
    for point in results.get("points", []):
        lines.extend(
            [
                f"### {point['sample_count']} samples, C={point['concurrency']}",
                "",
                "| repetition | service PID | n | throughput jobs/min | end-to-end | queue | compute | 429 count | 429 latency | status-poll 500 | effective C | available min | Δpagefile | ΔMemCompression | displacement | memory note | Page Reads median/max | Pages Input median/max | generator CPU p50/p95/max | citable |",
                "|---|---:|---:|---:|---|---|---|---:|---|---:|---:|---:|---:|---:|---:|---|---|---|---|---|",
            ]
        )
        for repetition in point["repetitions"]:
            formal = repetition["formal"]
            if "end_to_end_raw_seconds" in formal:
                end_to_end = _format_latency_summary(formal)
                queue = (
                    f"raw={json.dumps(formal['queue_raw_seconds'], separators=(',', ':'))}; "
                    f"missing={formal['split_missing_n']}"
                )
                compute = (
                    f"raw={json.dumps(formal['compute_raw_seconds'], separators=(',', ':'))}; "
                    f"missing={formal['split_missing_n']}"
                )
            else:
                end_to_end = _format_latency_summary(formal["end_to_end_seconds"])
                queue = _format_latency_summary(formal["queue_seconds"])
                compute = _format_latency_summary(formal["compute_seconds"])
            rejected = repetition["rejections_429"]
            resources = repetition["resources"]
            memory_cells = _format_memory_evidence(resources)
            page_reads = resources.get("page_reads_per_second", {})
            pages_input = resources.get("pages_input_per_second", {})
            generator_cpu = resources.get("generator_cpu", {})
            generator_cpu_text = (
                f"{generator_cpu.get('p50')}/{generator_cpu.get('p95')}/"
                f"{generator_cpu.get('max')}"
            )
            lines.append(
                f"| {repetition['label']} | {repetition['service']['pid']} | "
                f"{formal['n']} | {repetition['throughput_jobs_per_minute']:.6g} | "
                f"{end_to_end} | {queue} | {compute} | {rejected['count']} | "
                f"{_format_latency_summary(rejected['latency_seconds'])} | "
                f"{repetition.get('status_poll_500_count', 0)} | "
                f"{repetition['average_effective_concurrency']} | "
                f"{' | '.join(memory_cells)} | "
                f"{page_reads.get('median')}/{page_reads.get('max')} | "
                f"{pages_input.get('median')}/{pages_input.get('max')} | "
                f"{generator_cpu_text} | {repetition['citable']} |"
            )
    lines.extend(
        [
            "",
            "## Terminal persistence pressure",
            "",
            "| point | repetition | health n | pending median/p95/max | oldest wait median/p95/max | >=1 | >=3 | crossings to >=3 | growth observed | fatal 503 |",
            "|---|---|---:|---|---|---:|---:|---:|---|---|",
        ]
    )
    for point in results.get("points", []):
        for repetition in point["repetitions"]:
            backlog = repetition["terminal_persistence"]
            pending = backlog["pending_count"]
            waits = backlog["oldest_wait_seconds"]
            lines.append(
                f"| {point['sample_count']}/C={point['concurrency']} | "
                f"{repetition['label']} | {backlog['n']} | "
                f"{pending['median']}/{pending['p95']}/{pending['max']} | "
                f"{waits['median']}/{waits['p95']}/{waits['max']} | "
                f"{backlog['pending_count_ge_1_samples']} | "
                f"{backlog['pending_count_ge_3_samples']} | "
                f"{backlog['upward_crossings_to_ge_3']} | "
                f"{backlog['terminal_persistence_growth_observed']} | "
                f"{json.dumps(repetition.get('fatal_503'), ensure_ascii=False)} |"
            )
    lines.extend(
        [
            "",
            "All repetition-level n, latency fields, raw 360k values, service PIDs, effective concurrency, memory/pagefile/PDH/disk and generator CPU evidence are in `raw-results.json`.",
            "",
            "## Correctness",
            "",
            f"Suite stopped for mismatch: **{results.get('correctness_mismatch') is not None}**",
            "",
            f"Correctness summaries: `{json.dumps(results.get('correctness', {}), ensure_ascii=False, sort_keys=True)}`",
            "",
            "## Fault tests and A2",
            "",
            "| scenario | available min | Δpagefile | ΔMemCompression | displacement | memory note | citable |",
            "|---|---:|---:|---:|---:|---|---|",
        ]
    )
    for name in (
        "contract_faults",
        "timeout_fault",
        "ttl_fault",
        "fallback_429_burst",
        "a2",
    ):
        scenario = results.get(name)
        if not isinstance(scenario, dict) or not isinstance(scenario.get("resources"), dict):
            continue
        cells = _format_memory_evidence(scenario["resources"])
        lines.append(
            f"| {name} | {' | '.join(cells)} | {scenario.get('citable')} |"
        )
    lines.extend(
        [
            "",
            f"Contract faults: `{json.dumps(results.get('contract_faults'), ensure_ascii=False, sort_keys=True)}`",
            "",
            f"Timeout isolation: `{json.dumps(results.get('timeout_fault'), ensure_ascii=False, sort_keys=True)}`",
            "",
            f"TTL race: `{json.dumps(results.get('ttl_fault'), ensure_ascii=False, sort_keys=True)}`",
            "",
            f"A2: `{json.dumps(results.get('a2'), ensure_ascii=False, sort_keys=True)}`",
            "",
            f"Drift: `{json.dumps(results.get('drift'), ensure_ascii=False, sort_keys=True)}`",
        ]
    )
    drift = results.get("drift")
    if isinstance(drift, dict) and "a1_completed_counts" in drift:
        lines.extend(
            [
                "",
                "## A2 session-level shift analysis",
                "",
                (
                    f"- A1 formal completed counts: `{drift['a1_completed_counts']}`; "
                    f"A2: `{drift['a2_completed_count']}`; delta: "
                    f"`{drift['completed_count_delta']}` jobs."
                ),
                f"- Signed count shift: `{drift['signed_count_shift_percent']:.3f}%`.",
                (
                    "- This level shift is compared with each measurement's own observed "
                    "repetition spread; a zero spread is indeterminate rather than a "
                    "zero-width gate."
                ),
                "",
                "| measurement | completed counts | repetition spread | comparison | exceeds spread |",
                "|---|---|---:|---|---|",
            ]
        )
        for comparison in drift["point_spread_comparisons"]:
            spread = comparison["repetition_spread_percent"]
            spread_text = "n/a" if spread is None else f"{spread:.3f}%"
            counts = comparison["completed_counts"]
            counts_text = (
                "n/a" if counts is None else json.dumps(counts, separators=(",", ":"))
            )
            lines.append(
                f"| {comparison['label']} | {counts_text} | {spread_text} | "
                f"{comparison['comparison']} | "
                f"{comparison['session_shift_exceeds_repetition_spread']} |"
            )
    lines.extend(
        [
            "",
            "## Limitations",
            "",
            "- These are laptop session measurements, not production throughput.",
            "- Closed-loop latency is service-capacity evidence, not open-loop user-perceived latency.",
            "- End-to-end observations include up to one 50 ms polling interval of quantization.",
            "- Generator, parent, and workers contend for the same logical processors.",
            "- A2 is a level-shift context check; each measurement is interpreted against its own repetition spread.",
        ]
    )
    return "\n".join(lines) + "\n"


async def run_suite(output_dir: Path, logger: RunLogger) -> dict[str, Any]:
    repository_root = Path(__file__).resolve().parents[1]
    repository_rejected_root = repository_root / "bench-data" / "rejected"
    source_commit_result = await asyncio.to_thread(
        subprocess.run,
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    source_commit = source_commit_result.stdout.strip()
    try:
        storage = select_work_roots(repository_root)
    except UnsafeStoragePath as exc:
        preflight = preflight_snapshot(repository_root)
        preflight["passed"] = False
        preflight["failures"].append("work_root_selection_failed")
        failure_results: dict[str, Any] = {
            "schema_version": 1,
            "session_id": output_dir.name,
            "source_commit": source_commit,
            "attachment_original_sha256": SOURCE_ATTACHMENT_SHA256,
            "preflight": preflight,
            "storage": {"selection_failure": str(exc)},
            "points": [],
            "stopped_reason": "work_root_selection_failed",
        }
        _atomic_json(output_dir / "raw-results.json", failure_results)
        logger.emit("work_root_selection_failed", error=str(exc))
        return failure_results
    session_short_id = f"s-{uuid.uuid4().hex[:8]}"
    session_work_root = storage.work_root / session_short_id
    session_work_root.mkdir(parents=False, exist_ok=False)
    _strict_descendant(session_work_root, storage.work_root, repository_root)
    preflight = preflight_snapshot(session_work_root)
    memcompression = MemCompressionSampler()
    memcompression_pids, memcompression_rss, memcompression_error = (
        memcompression.collect()
    )
    logger.emit(
        "storage_root_selected",
        work_root=str(storage.work_root),
        rejected_root=str(storage.rejected_root),
        session_work_root=str(session_work_root),
        path_budget=storage.path_budget,
    )
    results: dict[str, Any] = {
        "schema_version": 1,
        "session_id": output_dir.name,
        "source_commit": source_commit,
        "attachment_original_sha256": SOURCE_ATTACHMENT_SHA256,
        "preflight": preflight,
        "storage": {
            "work_root": str(storage.work_root),
            "rejected_root": str(storage.rejected_root),
            "session_work_root": str(session_work_root),
            "selection_attempts": storage.attempts,
            "path_budget": storage.path_budget,
        },
        "configuration": {
            "max_workers": 2,
            "max_queue": 8,
            "timeout_seconds": 120,
            "max_upload_bytes": 64 * MiB,
            "result_ttl_hours": 24,
            "poll_interval_seconds": POLL_INTERVAL_SECONDS,
            "retry_429_seconds": RETRY_429_SECONDS,
        },
        "memcompression_session_discovery": {
            "pids": memcompression_pids,
            "rss_bytes": memcompression_rss,
            "error": memcompression_error,
        },
        "points": [],
        "rejection_evidence_dirs": [],
    }
    _atomic_json(output_dir / "raw-results.json", results)
    if not preflight["passed"]:
        logger.emit("preflight_failed", failures=preflight["failures"])
        results["stopped_reason"] = "environment_preflight_failed"
        return results

    inputs: dict[int, Path] = {}
    input_metadata: dict[str, Any] = {}
    for sample_count in (30_000, 360_000):
        path = generate_fixture(
            sample_count, session_work_root / "inputs" / f"walk-{sample_count}.txt"
        )
        if path.stat().st_size > 64 * MiB:
            raise RuntimeError(
                f"generated {sample_count}-sample fixture exceeds configured upload limit"
            )
        inputs[sample_count] = path
        input_metadata[str(sample_count)] = {
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": _sha256_path(path),
        }
    results["inputs"] = input_metadata
    calibrations: dict[str, Any] = {}
    for sample_count in (30_000, 360_000):
        calibration = await run_memory_calibration(
            sample_count=sample_count,
            input_path=inputs[sample_count],
            session_dir=output_dir,
            session_work_root=session_work_root,
            work_root=storage.work_root,
            repository_root=repository_root,
            logger=logger,
            memcompression=memcompression,
        )
        calibrations[str(sample_count)] = calibration
        results["memory_calibrations"] = calibrations
        _atomic_json(output_dir / "raw-results.json", results)
        if calibration["abort_suite"] or not calibration["passed"]:
            evidence = _archive_or_clean_result(
                calibration,
                citable=False,
                full_label=str(calibration["label"]),
                aggregate=calibration,
                work_root=storage.work_root,
                external_rejected_root=storage.rejected_root,
                repository_rejected_root=repository_rejected_root,
                repository_root=repository_root,
                stdout_path=logger.path,
            )
            if evidence is not None:
                results["rejection_evidence_dirs"].append(evidence)
            results["stopped_reason"] = (
                str(calibration["abort_reason"])
                if calibration["abort_suite"]
                else f"memory_calibration_failed_{sample_count}"
            )
            _atomic_json(output_dir / "raw-results.json", results)
            return results

    threshold_30k = calibration_threshold(calibrations["30000"], 20)
    threshold_360k = calibration_threshold(calibrations["360000"], 2)
    available_samples = await read_available_memory_samples()
    available_bytes = min(int(row["available_bytes"]) for row in available_samples)
    decision = decide_memory_subsets(available_bytes, threshold_30k, threshold_360k)
    decision["available_samples"] = available_samples
    results["memory_thresholds"] = {
        "30000": threshold_30k,
        "360000": threshold_360k,
    }
    results["memory_gate_decision"] = decision
    results["skipped_points"] = []
    if not decision["run_30k"]:
        results["skipped_points"].append(
            {
                "subset": "30k-matrix-faults-a2",
                "reason": "available_memory_below_measured_threshold",
                "available_bytes": available_bytes,
                "threshold_bytes": threshold_30k["threshold_bytes"],
                "formula": threshold_30k["formula"],
                "deficit_bytes": threshold_30k["threshold_bytes"] - available_bytes,
            }
        )
    if not decision["run_360k"]:
        results["skipped_points"].append(
            {
                "subset": "360k-c2",
                "reason": "available_memory_below_measured_threshold",
                "available_bytes": available_bytes,
                "threshold_bytes": threshold_360k["threshold_bytes"],
                "formula": threshold_360k["formula"],
                "deficit_bytes": threshold_360k["threshold_bytes"] - available_bytes,
            }
        )
    _atomic_json(output_dir / "raw-results.json", results)
    if not decision["any_subset"]:
        results["stopped_reason"] = "both_memory_subsets_below_measured_thresholds"
        _atomic_json(output_dir / "raw-results.json", results)
        return results

    oracles = {
        sample_count: CorrectnessOracle(
            repository_rejected_root / f"{output_dir.name}-correctness-{sample_count}",
            input_metadata[str(sample_count)]["sha256"],
        )
        for sample_count in inputs
    }
    matrix_had_429 = False
    selected_matrix = [
        row
        for row in MATRIX
        if (row[0] == 30_000 and decision["run_30k"])
        or (row[0] == 360_000 and decision["run_360k"])
    ]
    for sample_count, concurrency, window_seconds in selected_matrix:
        repetitions: list[dict[str, Any]] = []
        for repetition_number in range(1, 4):
            label = f"n{sample_count}-c{concurrency}-r{repetition_number}"
            repetition = await run_repetition(
                label=label,
                sample_count=sample_count,
                concurrency=concurrency,
                window_seconds=window_seconds,
                target=TARGET_COMPLETIONS,
                input_path=inputs[sample_count],
                session_dir=output_dir,
                session_work_root=session_work_root,
                path_id_prefix=(
                    f"c{sample_count // 1000}-{concurrency}-{repetition_number}"
                ),
                oracle=oracles[sample_count],
                logger=logger,
                memcompression=memcompression,
            )
            repetitions.append(repetition)
            matrix_had_429 |= repetition["rejections_429"]["count"] > 0
            _atomic_json(output_dir / "raw-results.json", results | {"in_progress": repetitions})
            if repetition["abort_suite"]:
                results["correctness_mismatch"] = oracles[sample_count].mismatch
                results["aborted_at"] = label
                results["abort_reason"] = (
                    "correctness-mismatch"
                    if oracles[sample_count].mismatch is not None
                    else repetition["fatal_error"] or "disk-free-below-20-gib"
                )
                aborted_point = aggregate_point(repetitions)
                if "suite-aborted" not in aborted_point["invalid_reasons"]:
                    aborted_point["invalid_reasons"].append("suite-aborted")
                aborted_point["citable"] = False
                results["rejection_evidence_dirs"].extend(
                    _archive_or_clean_point(
                        aborted_point,
                        work_root=storage.work_root,
                        external_rejected_root=storage.rejected_root,
                        repository_rejected_root=repository_rejected_root,
                        repository_root=repository_root,
                        stdout_path=logger.path,
                    )
                )
                results["points"].append(aborted_point)
                _atomic_json(output_dir / "raw-results.json", results)
                return results
        point = aggregate_point(repetitions)
        results["rejection_evidence_dirs"].extend(
            _archive_or_clean_point(
                point,
                work_root=storage.work_root,
                external_rejected_root=storage.rejected_root,
                repository_rejected_root=repository_rejected_root,
                repository_root=repository_root,
                stdout_path=logger.path,
            )
        )
        results["points"].append(point)
        _atomic_json(output_dir / "raw-results.json", results)

    if decision["run_30k"]:
        fault_runners = (
            ("contract_faults", run_contract_faults),
            ("timeout_fault", run_timeout_fault),
            ("ttl_fault", run_ttl_fault),
        )
        for key, runner in fault_runners:
            results[key] = await runner(
                inputs[30_000],
                output_dir,
                session_work_root,
                logger,
                memcompression,
            )
            fault = results[key]
            evidence = _archive_or_clean_result(
                fault,
                citable=bool(fault["citable"]),
                full_label=key,
                aggregate=fault,
                work_root=storage.work_root,
                external_rejected_root=storage.rejected_root,
                repository_rejected_root=repository_rejected_root,
                repository_root=repository_root,
                stdout_path=logger.path,
            )
            if evidence is not None:
                results["rejection_evidence_dirs"].append(evidence)
            if fault.get("abort_suite"):
                results["aborted_at"] = key
                results["abort_reason"] = fault.get("fatal_error") or "fault-suite-aborted"
                _atomic_json(output_dir / "raw-results.json", results)
                return results
        if not matrix_had_429:
            burst = await run_repetition(
                label="fallback-429-burst",
                sample_count=30_000,
                concurrency=20,
                window_seconds=30.0,
                target=10_000,
                input_path=inputs[30_000],
                session_dir=output_dir,
                session_work_root=session_work_root,
                path_id_prefix="b429",
                oracle=oracles[30_000],
                logger=logger,
                memcompression=memcompression,
            )
            results["fallback_429_burst"] = burst
            results["fallback_burst_excluded_from_throughput_table"] = True
            burst_point = aggregate_point([burst])
            if burst["abort_suite"]:
                burst_point["invalid_reasons"].append("suite-aborted")
                burst_point["citable"] = False
            results["rejection_evidence_dirs"].extend(
                _archive_or_clean_point(
                    burst_point,
                    work_root=storage.work_root,
                    external_rejected_root=storage.rejected_root,
                    repository_rejected_root=repository_rejected_root,
                    repository_root=repository_root,
                    stdout_path=logger.path,
                )
            )
            if burst["abort_suite"]:
                results["correctness_mismatch"] = oracles[30_000].mismatch
                results["aborted_at"] = "fallback-429-burst"
                results["abort_reason"] = (
                    "correctness-mismatch"
                    if oracles[30_000].mismatch is not None
                    else burst["fatal_error"] or "disk-free-below-20-gib"
                )
                _atomic_json(output_dir / "raw-results.json", results)
                return results

        a2 = await run_repetition(
            label="a2-n30000-c1",
            sample_count=30_000,
            concurrency=1,
            window_seconds=300.0,
            target=TARGET_COMPLETIONS,
            input_path=inputs[30_000],
            session_dir=output_dir,
            session_work_root=session_work_root,
            path_id_prefix="a2-30-1",
            oracle=oracles[30_000],
            logger=logger,
            memcompression=memcompression,
        )
        a2_point = aggregate_point([a2])
        if a2["abort_suite"]:
            a2_point["invalid_reasons"].append("suite-aborted")
            a2_point["citable"] = False
        results["rejection_evidence_dirs"].extend(
            _archive_or_clean_point(
                a2_point,
                work_root=storage.work_root,
                external_rejected_root=storage.rejected_root,
                repository_rejected_root=repository_rejected_root,
                repository_root=repository_root,
                stdout_path=logger.path,
            )
        )
        if a2["abort_suite"]:
            results["correctness_mismatch"] = oracles[30_000].mismatch
            results["aborted_at"] = "a2-n30000-c1"
            results["abort_reason"] = (
                "correctness-mismatch"
                if oracles[30_000].mismatch is not None
                else a2["fatal_error"] or "disk-free-below-20-gib"
            )
            _atomic_json(output_dir / "raw-results.json", results)
            return results
        a1 = next(
            point
            for point in results["points"]
            if point["sample_count"] == 30_000 and point["concurrency"] == 1
        )
        results["a2"] = a2
        comparison_targets = [
            {
                "label": f"n{point['sample_count']}-c{point['concurrency']}",
                "completed_counts": [
                    int(row["formal"]["n"]) for row in point["repetitions"]
                ],
                "window_limited": all(
                    int(row["formal"]["n"]) < TARGET_COMPLETIONS
                    for row in point["repetitions"]
                ),
                "repetition_spread_percent": point["throughput_spread_percent"],
            }
            for point in results["points"]
        ]
        non_repetition_scenarios = [
            name
            for name in (
                "contract_faults",
                "timeout_fault",
                "ttl_fault",
                "fallback_429_burst",
                "a2",
            )
            if name in results
        ]
        comparison_targets.extend(
            {
                "label": name,
                "completed_counts": None,
                "window_limited": None,
                "repetition_spread_percent": None,
            }
            for name in non_repetition_scenarios
        )
        results["drift"] = compare_drift(
            [int(row["formal"]["n"]) for row in a1["repetitions"]],
            int(a2["formal"]["n"]),
            comparison_targets,
        )
        comparisons = {
            str(row["label"]): row for row in results["drift"]["point_spread_comparisons"]
        }
        for point in results["points"]:
            label = f"n{point['sample_count']}-c{point['concurrency']}"
            comparison = comparisons[label]
            point["session_shift_comparison"] = comparison["comparison"]
            point["session_shift_exceeds_repetition_spread"] = comparison[
                "session_shift_exceeds_repetition_spread"
            ]
        for name in non_repetition_scenarios:
            comparison = comparisons[name]
            results[name]["session_shift_comparison"] = comparison["comparison"]
            results[name]["session_shift_exceeds_repetition_spread"] = comparison[
                "session_shift_exceeds_repetition_spread"
            ]
    results["correctness"] = {str(key): oracle.summary() for key, oracle in oracles.items()}
    results["completed_at"] = datetime.now(UTC).isoformat()
    _atomic_json(output_dir / "raw-results.json", results)
    safe_rmtree(session_work_root, storage.work_root, repository_root)
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Record the environment gates without starting the service or load.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Result directory; defaults to bench-data/loadtest-<timestamp>.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repository_root = Path(__file__).resolve().parents[1]
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = (args.output_dir or repository_root / "bench-data" / f"loadtest-{timestamp}").resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    logger = RunLogger(output_dir / "stdout.log")
    try:
        if args.preflight_only:
            snapshot = preflight_snapshot(repository_root)
            _atomic_json(output_dir / "preflight.json", snapshot)
            logger.emit("preflight", passed=snapshot["passed"], failures=snapshot["failures"])
            print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True))
            return 0 if snapshot["passed"] else 3
        results = asyncio.run(run_suite(output_dir, logger))
        report = _report_markdown(results)
        (output_dir / "LOADTEST.md").write_text(report, encoding="utf-8")
        if results["preflight"]["passed"]:
            (repository_root / "LOADTEST.md").write_text(report, encoding="utf-8")
        logger.emit(
            "suite_end",
            completed=bool(results.get("completed_at")),
            output_dir=str(output_dir),
        )
        logger.flush()
        _refresh_rejection_stdout(
            [str(path) for path in results.get("rejection_evidence_dirs", [])],
            logger.path,
        )
        if not results["preflight"]["passed"]:
            return 3
        if results.get("correctness_mismatch") is not None:
            return 4
        if results.get("stopped_reason") is not None or results.get("abort_reason") is not None:
            return 5
        return 0
    finally:
        logger.close()


if __name__ == "__main__":
    raise SystemExit(main())
