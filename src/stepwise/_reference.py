"""Permanent correctness oracles for the pipeline's future fast paths.

These implementations are intentionally naive and independently readable. They must
never be optimised, and they must not be deleted when the corresponding fast paths land.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .parsing import InputValidationError

_REFERENCE_COLUMNS = [
    "Sample",
    "SystemTime",
    "P1",
    "P2",
    "P3",
    "P4",
    "AccX",
    "AccY",
    "AccZ",
    "GyrX",
    "GyrY",
    "GyrZ",
    "Pitch",
    "Roll",
    "Yaw",
]

_Trapezoid = Callable[[Any, Any], Any]


def _reference_trapezoid() -> _Trapezoid:
    return np.trapezoid if hasattr(np, "trapezoid") else np.trapz


def _reference_classify_step(row: pd.Series) -> str:
    labels: list[str] = []
    if row["FrontRatio_mean"] >= 0.70:
        labels.append("Forefoot-heavy")
    if row["RearRatio_mean"] >= 0.70:
        labels.append("Rearfoot-heavy")
    if row["MedialRatio_mean"] >= 0.65:
        labels.append("Medial overload")
    if row["LateralRatio_mean"] >= 0.65:
        labels.append("Lateral overload")
    if row.get("ArchRatio_mean", 0) >= 0.25:
        labels.append("High arch/midfoot loading")
    if row["PushOffRatio_late_stance_mean"] < 0.30:
        labels.append("Weak push-off candidate")
    return "; ".join(labels) if labels else "Balanced-like contact"


def reference_parse_text(text: str) -> pd.DataFrame:
    """Parse text with the original per-line filtering implementation."""
    rows: list[list[str]] = []
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == len(_REFERENCE_COLUMNS) and parts[0].isdigit():
            rows.append(parts)
    if not rows:
        raise InputValidationError("no_data_rows", "no StepWise data rows were found")

    frame = pd.DataFrame(rows, columns=_REFERENCE_COLUMNS)
    for column in _REFERENCE_COLUMNS:
        if column != "SystemTime":
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["Sample"]).reset_index(drop=True)
    if frame.empty:
        raise InputValidationError("no_data_rows", "no StepWise data rows were found")

    timestamps = pd.to_datetime(frame["SystemTime"], format="%H:%M:%S.%f", errors="coerce")
    if timestamps.isna().any():
        raise InputValidationError(
            "invalid_timestamp", "SystemTime values must use HH:MM:SS.mmm"
        )
    elapsed = (timestamps - timestamps.iloc[0]).dt.total_seconds()
    elapsed = elapsed.mask(elapsed < 0, elapsed + 24 * 3600)
    frame["Time_s"] = elapsed
    return frame


def reference_extract_stance_features(
    frame: pd.DataFrame, intervals: list[tuple[int, int]]
) -> pd.DataFrame:
    """Extract stance features with the original per-stance Python loop."""
    rows: list[dict[str, Any]] = []
    previous_start_time: float | None = None
    trapezoid = _reference_trapezoid()
    for step_index, (start_index, end_index) in enumerate(intervals, start=1):
        segment = frame.iloc[start_index : end_index + 1]
        early_end = start_index + max(1, int((end_index - start_index + 1) * 0.20))
        early = frame.iloc[start_index : early_end + 1]
        late_start = start_index + int((end_index - start_index + 1) * 0.65)
        late = frame.iloc[late_start : end_index + 1]
        start_time = float(segment["Time_s"].iloc[0])
        end_time = float(segment["Time_s"].iloc[-1])
        stance_time = end_time - start_time
        stride_time = np.nan if previous_start_time is None else start_time - previous_start_time
        swing_time = np.nan if previous_start_time is None else stride_time - stance_time
        previous_start_time = start_time
        row: dict[str, Any] = {
            "Step": step_index,
            "StartSample": int(segment["Sample"].iloc[0]),
            "EndSample": int(segment["Sample"].iloc[-1]),
            "StartTime_s": start_time,
            "EndTime_s": end_time,
            "StanceTime_s": stance_time,
            "StrideTime_s": stride_time,
            "SwingTime_s": swing_time,
            "PeakPressure_N": float(segment["TotalPressure"].max()),
            "MeanPressure_N": float(segment["TotalPressure"].mean()),
            "PressureImpulse_Ns": float(trapezoid(segment["TotalPressure"], segment["Time_s"])),
            "EarlyRearRatio_mean": float(early["RearRatio"].mean(skipna=True)),
            "EarlyFrontRatio_mean": float(early["FrontRatio"].mean(skipna=True)),
            "RearRatio_mean": float(segment["RearRatio"].mean(skipna=True)),
            "ArchRatio_mean": float(segment["ArchRatio"].mean(skipna=True)),
            "FrontRatio_mean": float(segment["FrontRatio"].mean(skipna=True)),
            "MedialRatio_mean": float(segment["MedialRatio"].mean(skipna=True)),
            "LateralRatio_mean": float(segment["LateralRatio"].mean(skipna=True)),
            "MedialLateralBalance_mean": float(
                segment["MedialLateralBalance"].mean(skipna=True)
            ),
            "ToeRatio_late_stance_mean": float(late["ToeRatio"].mean(skipna=True)),
            "PushOffRatio_late_stance_mean": float(late["PushOffRatio"].mean(skipna=True)),
            "CoP_AP_early_mean": float(early["CoP_AP"].mean(skipna=True)),
            "CoP_AP_late_mean": float(late["CoP_AP"].mean(skipna=True)),
            "CoP_AP_progression": float(
                late["CoP_AP"].mean(skipna=True) - early["CoP_AP"].mean(skipna=True)
            ),
            "CoP_ML_mean": float(segment["CoP_ML"].mean(skipna=True)),
            "InitialContactRoll_deg": float(segment["Roll"].iloc[0]),
            "LandingRoll_mean_deg": float(early["Roll"].mean(skipna=True)),
            "InitialContactPitch_deg": float(segment["Pitch"].iloc[0]),
            "LandingPitch_mean_deg": float(early["Pitch"].mean(skipna=True)),
            "PitchRange_deg": float(segment["Pitch"].max() - segment["Pitch"].min()),
            "RollRange_deg": float(segment["Roll"].max() - segment["Roll"].min()),
            "GyrMag_peak": float(segment["GyrMag"].max()),
            "AccMag_peak": float(segment["AccMag"].max()),
        }
        row["Pattern"] = _reference_classify_step(pd.Series(row))
        rows.append(row)
    return pd.DataFrame(rows)


def reference_processed_frame_roundtrip(frame: pd.DataFrame, csv_path: Path) -> pd.DataFrame:
    """Persist and reload a processed frame using the original eager CSV path."""
    frame.to_csv(csv_path, index=False)
    return pd.read_csv(csv_path)
