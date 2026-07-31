"""Typed domain models shared by the pipeline and adapters."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from numbers import Real
from typing import Any, Literal


CHANNELS = frozenset({"P1", "P2", "P3", "P4"})


def strict_json_value(value: Any) -> Any:
    """Return JSON-compatible values while replacing non-finite numbers with null."""
    if isinstance(value, dict):
        return {str(key): strict_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [strict_json_value(item) for item in value]
    if isinstance(value, Real) and not isinstance(value, bool):
        number = float(value)
        if not math.isfinite(number):
            return None
        if isinstance(value, int):
            return value
    if hasattr(value, "item"):
        return strict_json_value(value.item())
    return value


@dataclass(frozen=True)
class SensorMapping:
    heel: str = "P2"
    arch: str = "P3"
    medial_forefoot: str = "P4"
    lateral_forefoot: str = "P1"
    pitch_eversion_sign: Literal["positive", "negative"] = "positive"

    def __post_init__(self) -> None:
        fields = ("heel", "arch", "medial_forefoot", "lateral_forefoot")
        channels = []
        for name in fields:
            channel = str(getattr(self, name)).upper()
            if channel not in CHANNELS:
                raise ValueError(f"{name} must be one of P1, P2, P3, P4")
            object.__setattr__(self, name, channel)
            channels.append(channel)
        if len(set(channels)) != len(channels):
            raise ValueError("sensor mapping channels must be unique")
        if self.pitch_eversion_sign not in {"positive", "negative"}:
            raise ValueError("pitch_eversion_sign must be positive or negative")


@dataclass(frozen=True)
class AnalysisConfig:
    sensor_mapping: SensorMapping = field(default_factory=SensorMapping)
    smooth_window: int = 3
    min_threshold_n: float = 5.0
    threshold_ratio: float = 0.08
    min_stance_s: float = 0.08
    body_weight_n: float | None = None

    def __post_init__(self) -> None:
        if self.smooth_window < 1:
            raise ValueError("smooth_window must be at least 1")
        if self.min_threshold_n < 0:
            raise ValueError("min_threshold_n must be non-negative")
        if not 0 < self.threshold_ratio <= 1:
            raise ValueError("threshold_ratio must be in (0, 1]")
        if self.min_stance_s <= 0:
            raise ValueError("min_stance_s must be positive")
        if self.body_weight_n is not None and self.body_weight_n <= 0:
            raise ValueError("body_weight_n must be positive when provided")


@dataclass(frozen=True)
class RiskCard:
    title: str
    level: str
    evidence: str
    interpretation: str
    action: str
    limitation: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class Artifact:
    name: str
    media_type: str
    size_bytes: int

    def __post_init__(self) -> None:
        if not self.name or "/" in self.name or "\\" in self.name:
            raise ValueError("artifact name must be a basename")
        if self.size_bytes < 0:
            raise ValueError("artifact size_bytes must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AnalysisResult:
    summary: dict[str, Any]
    metrics: dict[str, Any]
    risk_cards: tuple[RiskCard, ...]
    artifacts: tuple[Artifact, ...]

    def to_dict(self) -> dict[str, Any]:
        return strict_json_value(
            {
                "summary": self.summary,
                "metrics": self.metrics,
                "risk_cards": [card.to_dict() for card in self.risk_cards],
                "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            }
        )
