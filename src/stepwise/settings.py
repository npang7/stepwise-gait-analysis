"""Environment-backed service settings."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    max_upload_bytes: int = 64 * 1024 * 1024
    analysis_timeout_seconds: float = 120.0
    max_workers: int = 2
    max_queue: int = 8
    result_ttl_hours: float = 24.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_dir", Path(self.data_dir).resolve())
        if self.max_upload_bytes <= 0:
            raise ValueError("max_upload_bytes must be positive")
        if self.analysis_timeout_seconds <= 0:
            raise ValueError("analysis_timeout_seconds must be positive")
        if self.max_workers <= 0:
            raise ValueError("max_workers must be positive")
        if self.max_queue <= 0:
            raise ValueError("max_queue must be positive")
        if self.result_ttl_hours <= 0:
            raise ValueError("result_ttl_hours must be positive")

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            data_dir=Path(os.getenv("STEPWISE_DATA_DIR", "stepwise-data")),
            max_upload_bytes=int(os.getenv("STEPWISE_MAX_UPLOAD_BYTES", str(64 * 1024 * 1024))),
            analysis_timeout_seconds=float(
                os.getenv("STEPWISE_ANALYSIS_TIMEOUT_SECONDS", "120")
            ),
            max_workers=int(os.getenv("STEPWISE_MAX_WORKERS", "2")),
            max_queue=int(os.getenv("STEPWISE_MAX_QUEUE", "8")),
            result_ttl_hours=float(os.getenv("STEPWISE_RESULT_TTL_HOURS", "24")),
        )
