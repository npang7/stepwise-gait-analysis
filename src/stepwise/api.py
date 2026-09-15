"""FastAPI transport adapter for asynchronous StepWise analyses."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from .jobs import JobManager, QueueFullError, SubmissionError
from .models import AnalysisConfig, JobManifest, SensorMapping
from .settings import Settings
from .storage import ArtifactNotFoundError, JobNotFoundError

UPLOAD_CHUNK_BYTES = 64 * 1024


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


def _status_payload(manifest: JobManifest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": manifest.run_id,
        "status": manifest.status,
        "created_at": manifest.created_at,
        "updated_at": manifest.updated_at,
        "status_url": f"/api/v1/analyses/{manifest.run_id}",
        "result_url": f"/api/v1/analyses/{manifest.run_id}/result",
    }
    if manifest.status == "failed":
        payload["error"] = {
            "code": manifest.error_code,
            "message": manifest.error_message,
        }
    return payload


def _parse_config(sensor_mapping: str) -> AnalysisConfig:
    try:
        payload = json.loads(sensor_mapping or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("sensor_mapping must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise TypeError("sensor_mapping must be a JSON object")
    allowed = {
        "heel",
        "arch",
        "medial_forefoot",
        "lateral_forefoot",
        "pitch_eversion_sign",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"unknown sensor mapping fields: {', '.join(sorted(unknown))}")
    return AnalysisConfig(sensor_mapping=SensorMapping(**payload))


async def _stream_upload(upload: UploadFile, destination: Path, limit: int) -> int:
    written = 0
    with destination.open("xb") as handle:
        while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
            if written + len(chunk) > limit:
                raise SubmissionError(
                    "upload_too_large",
                    f"each recording must be at most {limit} bytes",
                )
            handle.write(chunk)
            written += len(chunk)
    return written


def create_app(
    settings: Settings | None = None,
    *,
    manager: JobManager | None = None,
) -> FastAPI:
    """Create an app; dependency injection keeps integration tests deterministic."""
    resolved_settings = settings or Settings.from_env()
    owns_manager = manager is None
    job_manager = manager or JobManager(
        resolved_settings.data_dir,
        max_workers=resolved_settings.max_workers,
        max_queue=resolved_settings.max_queue,
        timeout_seconds=resolved_settings.analysis_timeout_seconds,
        max_upload_bytes=resolved_settings.max_upload_bytes,
        result_ttl_hours=resolved_settings.result_ttl_hours,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_manager:
            job_manager.close()

    app = FastAPI(
        title="StepWise Analysis API",
        version="1.0.0",
        description=(
            "Asynchronous engineering screening of StepWise plantar-pressure and IMU recordings. "
            "Outputs are not medical diagnoses."
        ),
        lifespan=lifespan,
    )
    app.state.job_manager = job_manager
    app.state.settings = resolved_settings

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/analyses", status_code=202)
    async def create_analysis(
        walking: Annotated[UploadFile, File()],
        standing: Annotated[UploadFile | None, File()] = None,
        sensor_mapping: Annotated[str, Form()] = "{}",
    ) -> JSONResponse:
        try:
            config = _parse_config(sensor_mapping)
        except (TypeError, ValueError) as exc:
            return _error(422, "invalid_mapping", str(exc))

        walking_path = job_manager.repository.new_staging_path()
        standing_path = (
            job_manager.repository.new_staging_path() if standing is not None else None
        )
        try:
            await _stream_upload(walking, walking_path, resolved_settings.max_upload_bytes)
            if standing is not None and standing_path is not None:
                await _stream_upload(standing, standing_path, resolved_settings.max_upload_bytes)
            manifest = await asyncio.to_thread(
                job_manager.submit_staged,
                walking_path,
                standing_path,
                config,
            )
        except SubmissionError as exc:
            status_code = 413 if exc.code == "upload_too_large" else 422
            return _error(status_code, exc.code, exc.message)
        except QueueFullError:
            return _error(429, "queue_full", "The analysis queue is full; retry later.")
        finally:
            await asyncio.to_thread(job_manager.repository.remove_staged, walking_path)
            if standing_path is not None:
                await asyncio.to_thread(job_manager.repository.remove_staged, standing_path)
        return JSONResponse(status_code=202, content=_status_payload(manifest))

    @app.get("/api/v1/analyses/{run_id}")
    def get_analysis(run_id: str) -> JSONResponse:
        try:
            manifest = job_manager.repository.get(run_id)
        except JobNotFoundError:
            return _error(404, "analysis_not_found", "Analysis was not found.")
        return JSONResponse(content=_status_payload(manifest))

    @app.get("/api/v1/analyses/{run_id}/result")
    def get_result(run_id: str) -> JSONResponse:
        try:
            manifest = job_manager.repository.get(run_id)
        except JobNotFoundError:
            return _error(404, "analysis_not_found", "Analysis was not found.")
        if manifest.status != "succeeded" or manifest.result is None:
            message = (
                "Analysis failed; inspect the status endpoint for its error."
                if manifest.status == "failed"
                else "Analysis result is not ready."
            )
            return _error(409, "result_not_ready", message)
        return JSONResponse(content=manifest.result)

    @app.get("/api/v1/analyses/{run_id}/artifacts/{name}", response_model=None)
    def get_artifact(run_id: str, name: str) -> FileResponse | JSONResponse:
        try:
            manifest = job_manager.repository.get(run_id)
            path = job_manager.repository.artifact_path(run_id, name)
        except JobNotFoundError:
            return _error(404, "analysis_not_found", "Analysis was not found.")
        except ArtifactNotFoundError:
            return _error(404, "artifact_not_found", "Artifact was not found.")
        artifacts = [] if manifest.result is None else manifest.result.get("artifacts", [])
        media_type = next(
            (
                artifact.get("media_type")
                for artifact in artifacts
                if isinstance(artifact, dict) and artifact.get("name") == name
            ),
            "application/octet-stream",
        )
        return FileResponse(path, media_type=media_type, filename=name)

    return app
