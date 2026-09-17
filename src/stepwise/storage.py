"""Filesystem-backed run manifests and artifact access."""

from __future__ import annotations

import json
import shutil
import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import JobManifest, JobStatus

_MANIFEST_LOCK_STRIPES = 32


class JobNotFoundError(LookupError):
    pass


class ArtifactNotFoundError(LookupError):
    pass


class ArtifactLease:
    """Keep one run visible to an in-flight artifact response."""

    def __init__(self, repository: JobRepository, run_id: str) -> None:
        self._repository = repository
        self.run_id = run_id
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._repository._release_artifact_lease(self.run_id)
        self._released = True


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(now: datetime | None = None) -> str:
    return (now or _utc_now()).isoformat()


class JobRepository:
    """Persist job state under one UUID directory per analysis."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging_dir = self.root / ".staging"
        self.staging_dir.mkdir(exist_ok=True)
        # These RLocks coordinate threads sharing this repository instance. They do not
        # protect separate service processes or replicas that share one data directory;
        # those deployments require an interprocess file lock or external coordination.
        self._manifest_locks = tuple(
            threading.RLock() for _ in range(_MANIFEST_LOCK_STRIPES)
        )
        self._artifact_guard = threading.RLock()
        self._active_artifact_leases: dict[str, int] = {}
        self._artifact_locks = tuple(threading.Lock() for _ in range(32))

    @staticmethod
    def _validate_run_id(run_id: str) -> str:
        try:
            parsed = uuid.UUID(run_id)
        except (ValueError, AttributeError) as exc:
            raise JobNotFoundError(run_id) from exc
        if str(parsed) != run_id.lower():
            raise JobNotFoundError(run_id)
        return str(parsed)

    def run_dir(self, run_id: str) -> Path:
        return self.root / self._validate_run_id(run_id)

    def input_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "input"

    def artifact_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "artifacts"

    def _manifest_lock_for(self, run_id: str) -> threading.RLock:
        canonical_run_id = self._validate_run_id(run_id)
        index = uuid.UUID(canonical_run_id).int % len(self._manifest_locks)
        return self._manifest_locks[index]

    def create(self) -> JobManifest:
        run_id = str(uuid.uuid4())
        run_dir = self.run_dir(run_id)
        run_dir.mkdir(parents=False, exist_ok=False)
        self.input_dir(run_id).mkdir()
        self.artifact_dir(run_id).mkdir()
        now = _timestamp()
        manifest = JobManifest(
            run_id=run_id,
            status="queued",
            created_at=now,
            updated_at=now,
        )
        return self.save(manifest)

    def save(self, manifest: JobManifest) -> JobManifest:
        with self._manifest_lock_for(manifest.run_id):
            run_dir = self.run_dir(manifest.run_id)
            if not run_dir.is_dir():
                raise JobNotFoundError(manifest.run_id)
            destination = run_dir / "manifest.json"
            temporary = run_dir / "manifest.json.tmp"
            temporary.write_text(
                json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, allow_nan=False),
                encoding="utf-8",
            )
            temporary.replace(destination)
            return manifest

    def get(self, run_id: str) -> JobManifest:
        with self._manifest_lock_for(run_id):
            manifest_path = self.run_dir(run_id) / "manifest.json"
            if not manifest_path.is_file():
                raise JobNotFoundError(run_id)
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                return JobManifest.from_dict(payload)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise JobNotFoundError(run_id) from exc

    def write_input(self, run_id: str, name: str, content: bytes) -> Path:
        if name not in {"walking.txt", "standing.txt"}:
            raise ValueError("input name is not allowed")
        destination = self.input_dir(run_id) / name
        temporary = destination.with_suffix(".tmp")
        temporary.write_bytes(content)
        temporary.replace(destination)
        return destination

    def new_staging_path(self) -> Path:
        return self.staging_dir / str(uuid.uuid4())

    def _validate_staging_path(self, path: Path) -> Path:
        resolved = path.resolve()
        if resolved.parent != self.staging_dir or resolved == self.staging_dir:
            raise ValueError("staging path must be a direct child of the staging directory")
        return resolved

    def remove_staged(self, path: Path) -> None:
        resolved = self._validate_staging_path(path)
        if resolved.is_dir():
            shutil.rmtree(resolved)
        else:
            resolved.unlink(missing_ok=True)

    def cleanup_staging(self) -> list[Path]:
        removed: list[Path] = []
        for path in sorted(self.staging_dir.iterdir()):
            self.remove_staged(path)
            removed.append(path)
        return removed

    def move_staged_input(self, run_id: str, name: str, staged: Path) -> Path:
        if name not in {"walking.txt", "standing.txt"}:
            raise ValueError("input name is not allowed")
        source = self._validate_staging_path(staged)
        destination = self.input_dir(run_id) / name
        source.replace(destination)
        return destination

    def delete_run(self, run_id: str) -> None:
        with self._manifest_lock_for(run_id):
            run_dir = self.run_dir(run_id).resolve()
            if run_dir.parent != self.root or run_dir == self.root:
                raise JobNotFoundError(run_id)
            if run_dir.exists():
                shutil.rmtree(run_dir)

    def _transition(
        self,
        run_id: str,
        status: JobStatus,
        *,
        result: dict | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> JobManifest:
        with self._manifest_lock_for(run_id):
            previous = self.get(run_id)
            manifest = replace(
                previous,
                status=status,
                updated_at=_timestamp(),
                result=result,
                error_code=error_code,
                error_message=error_message,
            )
            return self.save(manifest)

    def mark_running(self, run_id: str) -> JobManifest:
        return self._transition(run_id, "running")

    def mark_succeeded(self, run_id: str, result: dict) -> JobManifest:
        return self._transition(run_id, "succeeded", result=result)

    def mark_failed(self, run_id: str, code: str, message: str) -> JobManifest:
        return self._transition(
            run_id,
            "failed",
            error_code=code,
            error_message=message,
        )

    def recover_incomplete(self) -> list[str]:
        recovered: list[str] = []
        for path in sorted(self.root.iterdir()):
            if not path.is_dir():
                continue
            try:
                with self._manifest_lock_for(path.name):
                    manifest = self.get(path.name)
                    if manifest.status in {"queued", "running"}:
                        self.mark_failed(
                            manifest.run_id,
                            "service_restarted",
                            "Analysis was interrupted because the service restarted.",
                        )
                        recovered.append(manifest.run_id)
            except JobNotFoundError:
                continue
        return recovered

    def artifact_destination(self, run_id: str, name: str) -> Path:
        if not name or Path(name).name != name or "/" in name or "\\" in name:
            raise ArtifactNotFoundError(name)
        manifest = self.get(run_id)
        artifacts = [] if manifest.result is None else manifest.result.get("artifacts", [])
        allowed = {item.get("name") for item in artifacts if isinstance(item, dict)}
        if name not in allowed:
            raise ArtifactNotFoundError(name)
        return self.artifact_dir(run_id) / name

    def artifact_path(self, run_id: str, name: str) -> Path:
        path = self.artifact_destination(run_id, name)
        if not path.is_file():
            raise ArtifactNotFoundError(name)
        return path

    def artifact_materialization_lock(self, run_id: str, name: str) -> threading.Lock:
        index = hash((run_id, name)) % len(self._artifact_locks)
        return self._artifact_locks[index]

    def acquire_artifact_lease(self, run_id: str) -> ArtifactLease:
        with self._artifact_guard:
            self.get(run_id)
            self._active_artifact_leases[run_id] = (
                self._active_artifact_leases.get(run_id, 0) + 1
            )
        return ArtifactLease(self, run_id)

    def _release_artifact_lease(self, run_id: str) -> None:
        with self._artifact_guard:
            count = self._active_artifact_leases.get(run_id, 0)
            if count <= 1:
                self._active_artifact_leases.pop(run_id, None)
            else:
                self._active_artifact_leases[run_id] = count - 1

    def cleanup_expired(
        self,
        ttl_hours: float,
        *,
        now: datetime | None = None,
    ) -> list[str]:
        if ttl_hours <= 0:
            raise ValueError("ttl_hours must be positive")
        current = now or _utc_now()
        cutoff = current - timedelta(hours=ttl_hours)
        removed: list[str] = []
        for path in sorted(self.root.iterdir()):
            if not path.is_dir():
                continue
            with self._artifact_guard:
                try:
                    with self._manifest_lock_for(path.name):
                        manifest = self.get(path.name)
                        updated_at = datetime.fromisoformat(manifest.updated_at)
                        if (
                            manifest.status not in {"succeeded", "failed"}
                            or updated_at >= cutoff
                            or self._active_artifact_leases.get(manifest.run_id, 0) > 0
                        ):
                            continue
                        resolved = path.resolve()
                        if resolved.parent != self.root or resolved == self.root:
                            continue
                        shutil.rmtree(resolved)
                        removed.append(manifest.run_id)
                except (JobNotFoundError, ValueError):
                    continue
        return removed
