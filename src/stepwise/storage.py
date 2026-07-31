"""Filesystem-backed run manifests and artifact access."""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import JobManifest, JobStatus


class JobNotFoundError(LookupError):
    pass


class ArtifactNotFoundError(LookupError):
    pass


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(now: datetime | None = None) -> str:
    return (now or _utc_now()).isoformat()


class JobRepository:
    """Persist job state under one UUID directory per analysis."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

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

    def _transition(
        self,
        run_id: str,
        status: JobStatus,
        *,
        result: dict | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> JobManifest:
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
                manifest = self.get(path.name)
            except JobNotFoundError:
                continue
            if manifest.status in {"queued", "running"}:
                self.mark_failed(
                    manifest.run_id,
                    "service_restarted",
                    "Analysis was interrupted because the service restarted.",
                )
                recovered.append(manifest.run_id)
        return recovered

    def artifact_path(self, run_id: str, name: str) -> Path:
        if not name or Path(name).name != name or "/" in name or "\\" in name:
            raise ArtifactNotFoundError(name)
        manifest = self.get(run_id)
        artifacts = [] if manifest.result is None else manifest.result.get("artifacts", [])
        allowed = {item.get("name") for item in artifacts if isinstance(item, dict)}
        if name not in allowed:
            raise ArtifactNotFoundError(name)
        path = self.artifact_dir(run_id) / name
        if not path.is_file():
            raise ArtifactNotFoundError(name)
        return path

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
            try:
                manifest = self.get(path.name)
                updated_at = datetime.fromisoformat(manifest.updated_at)
            except (JobNotFoundError, ValueError):
                continue
            if manifest.status not in {"succeeded", "failed"} or updated_at >= cutoff:
                continue
            resolved = path.resolve()
            if resolved.parent != self.root or resolved == self.root:
                continue
            shutil.rmtree(resolved)
            removed.append(manifest.run_id)
        return removed
