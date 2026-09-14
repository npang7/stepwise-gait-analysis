"""Per-stance and session-level feature extraction."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd

_Trapezoid = Callable[[Any, Any], Any]


def _select_trapezoid(module: Any) -> _Trapezoid:
    return module.trapezoid if hasattr(module, "trapezoid") else module.trapz


_TRAPEZOID = _select_trapezoid(np)


def _classify_step(row: pd.Series) -> str:
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


def extract_stance_features(
    frame: pd.DataFrame, intervals: list[tuple[int, int]]
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    previous_start_time: float | None = None
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
            "PressureImpulse_Ns": float(_TRAPEZOID(segment["TotalPressure"], segment["Time_s"])),
            "EarlyRearRatio_mean": float(early["RearRatio"].mean(skipna=True)),
            "EarlyFrontRatio_mean": float(early["FrontRatio"].mean(skipna=True)),
            "RearRatio_mean": float(segment["RearRatio"].mean(skipna=True)),
            "ArchRatio_mean": float(segment["ArchRatio"].mean(skipna=True)),
            "FrontRatio_mean": float(segment["FrontRatio"].mean(skipna=True)),
            "MedialRatio_mean": float(segment["MedialRatio"].mean(skipna=True)),
            "LateralRatio_mean": float(segment["LateralRatio"].mean(skipna=True)),
            "MedialLateralBalance_mean": float(segment["MedialLateralBalance"].mean(skipna=True)),
            "ToeRatio_late_stance_mean": float(late["ToeRatio"].mean(skipna=True)),
            "PushOffRatio_late_stance_mean": float(late["PushOffRatio"].mean(skipna=True)),
            "CoP_AP_early_mean": float(early["CoP_AP"].mean(skipna=True)),
            "CoP_AP_late_mean": float(late["CoP_AP"].mean(skipna=True)),
            "CoP_AP_progression": float(late["CoP_AP"].mean(skipna=True) - early["CoP_AP"].mean(skipna=True)),
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
        row["Pattern"] = _classify_step(pd.Series(row))
        rows.append(row)
    return pd.DataFrame(rows)


def summarize_session(
    frame: pd.DataFrame, steps: pd.DataFrame, enter_threshold: float, exit_threshold: float
) -> dict[str, Any]:
    duration = float(frame["Time_s"].iloc[-1] - frame["Time_s"].iloc[0])
    deltas = frame["Time_s"].diff().dropna()
    sample_rate = float((len(frame) - 1) / duration) if duration > 0 and len(frame) > 1 else None
    positive_deltas = deltas[deltas > 0]
    median_positive_delta = float(positive_deltas.median()) if len(positive_deltas) else None
    duplicate_count = int((deltas == 0).sum())
    cadence = float(len(steps) / duration * 60.0) if duration > 0 else None
    warnings: list[str] = []
    if sample_rate is not None and sample_rate < 50:
        warnings.append("Actual sample rate is below 50 Hz; gait-event timing may be unreliable.")
    if len(steps) < 6:
        warnings.append("Too few detected stance phases for robust gait screening.")
    zero_columns = frame[["P1", "P2", "P3", "P4"]].max()
    if (zero_columns == 0).any():
        warnings.append(
            "Some pressure channels are always zero: "
            + ", ".join(zero_columns[zero_columns == 0].index)
        )
    if duplicate_count:
        warnings.append(
            f"{duplicate_count} repeated timestamps were found; sample rate is estimated from total duration."
        )
    if sample_rate is None or sample_rate < 20 or len(steps) < 4:
        quality = "Low"
    elif sample_rate < 50 or len(steps) < 8:
        quality = "Medium"
    else:
        quality = "High"
    return {
        "samples": len(frame),
        "duration_s": duration,
        "estimated_sample_rate_hz": sample_rate,
        "median_positive_dt_s": median_positive_delta,
        "duplicate_timestamp_count": duplicate_count,
        "detected_steps_single_foot": len(steps),
        "estimated_single_foot_cadence_per_min": cadence,
        "contact_enter_threshold_n": enter_threshold,
        "contact_exit_threshold_n": exit_threshold,
        "mean_stance_time_s": None if steps.empty else float(steps["StanceTime_s"].mean()),
        "mean_stride_time_s": None if steps.empty else float(steps["StrideTime_s"].mean(skipna=True)),
        "data_quality": quality,
        "warnings": warnings,
    }


def compute_session_metrics(steps: pd.DataFrame, processed: pd.DataFrame) -> dict[str, float]:
    metrics: dict[str, float] = {}
    mean_columns = [
        "RearRatio_mean",
        "ArchRatio_mean",
        "FrontRatio_mean",
        "MedialRatio_mean",
        "LateralRatio_mean",
        "EarlyRearRatio_mean",
        "EarlyFrontRatio_mean",
        "PushOffRatio_late_stance_mean",
        "CoP_AP_progression",
        "CoP_ML_mean",
        "StrideTime_s",
        "LandingRoll_mean_deg",
        "InitialContactRoll_deg",
        "LandingPitch_mean_deg",
        "InitialContactPitch_deg",
        "RollRange_deg",
        "PitchRange_deg",
    ]
    for column in mean_columns:
        metrics[column] = (
            float(steps[column].mean(skipna=True)) if column in steps and not steps.empty else np.nan
        )
    stride = steps["StrideTime_s"].dropna() if "StrideTime_s" in steps else pd.Series(dtype=float)
    metrics["StrideCV"] = (
        float(stride.std(ddof=0) / stride.mean())
        if len(stride) >= 3 and float(stride.mean()) > 0
        else np.nan
    )
    contact = processed
    if "FootContact" in processed:
        mask = processed["FootContact"].astype(str).str.lower().isin(["true", "1"])
        if mask.any():
            contact = processed[mask]
    for column in ("Roll", "Pitch", "Yaw"):
        if column in contact and not contact.empty:
            metrics[f"{column}_stance_mean"] = float(contact[column].mean(skipna=True))
            metrics[f"{column}_stance_std"] = float(contact[column].std(skipna=True))
            metrics[f"{column}_stance_min"] = float(contact[column].min(skipna=True))
            metrics[f"{column}_stance_max"] = float(contact[column].max(skipna=True))
        else:
            for suffix in ("mean", "std", "min", "max"):
                metrics[f"{column}_stance_{suffix}"] = np.nan
    for key in (
        "LandingSoleGroundAngle_deg",
        "LandingSoleGroundAngle_abs_deg",
        "PitchDelta_stance_from_standing",
        "LandingPitchDelta_from_standing",
        "RollDelta_stance_from_standing",
    ):
        metrics[key] = np.nan
    return metrics
