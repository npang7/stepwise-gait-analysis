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
    if not intervals:
        return pd.DataFrame()

    starts = np.asarray([start for start, _end in intervals], dtype=np.int64)
    ends = np.asarray([end for _start, end in intervals], dtype=np.int64)
    lengths = ends - starts + 1
    early_offsets = np.maximum(1, (lengths * 0.20).astype(np.int64))
    early_ends = starts + early_offsets
    late_starts = starts + (lengths * 0.65).astype(np.int64)
    stance_ids = np.arange(len(intervals), dtype=np.int64)

    def labels_for(label_starts: np.ndarray, label_ends: np.ndarray) -> np.ndarray:
        labels = np.full(len(frame), -1, dtype=np.int64)
        occupancy_delta = np.zeros(len(frame) + 1, dtype=np.int64)
        np.add.at(occupancy_delta, label_starts, 1)
        np.add.at(occupancy_delta, label_ends + 1, -1)
        active = np.cumsum(occupancy_delta[:-1]) > 0
        labels[active] = np.repeat(stance_ids, label_ends - label_starts + 1)
        return labels

    stance_labels = labels_for(starts, ends)
    early_labels = labels_for(starts, early_ends)
    late_labels = labels_for(late_starts, ends)
    tagged = frame.assign(
        _stance=stance_labels,
        _early=early_labels,
        _late=late_labels,
    )

    stance = tagged[tagged["_stance"] >= 0].groupby("_stance", sort=True)
    early = tagged[tagged["_early"] >= 0].groupby("_early", sort=True)
    late = tagged[tagged["_late"] >= 0].groupby("_late", sort=True)

    times = frame["Time_s"].to_numpy(dtype=np.float64)
    pressures = frame["TotalPressure"].to_numpy(dtype=np.float64)
    increments = np.zeros(len(frame), dtype=np.float64)
    increments[1:] = np.diff(times) * (pressures[1:] + pressures[:-1]) / 2.0
    increments[starts] = 0.0
    impulse = (
        pd.DataFrame({"_stance": stance_labels, "_increment": increments})
        .loc[lambda data: data["_stance"] >= 0]
        .groupby("_stance", sort=True)["_increment"]
        .sum()
        .to_numpy(dtype=np.float64)
    )

    start_times = frame["Time_s"].to_numpy(dtype=np.float64)[starts]
    end_times = frame["Time_s"].to_numpy(dtype=np.float64)[ends]
    stance_times = end_times - start_times
    stride_times = np.empty(len(intervals), dtype=np.float64)
    stride_times[0] = np.nan
    stride_times[1:] = np.diff(start_times)
    swing_times = stride_times - stance_times
    swing_times[0] = np.nan

    def stance_mean(column: str) -> np.ndarray:
        return stance[column].mean().to_numpy(dtype=np.float64)

    def early_mean(column: str) -> np.ndarray:
        return early[column].mean().to_numpy(dtype=np.float64)

    def late_mean(column: str) -> np.ndarray:
        return late[column].mean().to_numpy(dtype=np.float64)

    early_cop_ap = early_mean("CoP_AP")
    late_cop_ap = late_mean("CoP_AP")
    result = pd.DataFrame(
        {
            "Step": np.arange(1, len(intervals) + 1, dtype=np.int64),
            "StartSample": frame["Sample"].to_numpy()[starts].astype(np.int64),
            "EndSample": frame["Sample"].to_numpy()[ends].astype(np.int64),
            "StartTime_s": start_times,
            "EndTime_s": end_times,
            "StanceTime_s": stance_times,
            "StrideTime_s": stride_times,
            "SwingTime_s": swing_times,
            "PeakPressure_N": stance["TotalPressure"].max().to_numpy(dtype=np.float64),
            "MeanPressure_N": stance_mean("TotalPressure"),
            "PressureImpulse_Ns": impulse,
            "EarlyRearRatio_mean": early_mean("RearRatio"),
            "EarlyFrontRatio_mean": early_mean("FrontRatio"),
            "RearRatio_mean": stance_mean("RearRatio"),
            "ArchRatio_mean": stance_mean("ArchRatio"),
            "FrontRatio_mean": stance_mean("FrontRatio"),
            "MedialRatio_mean": stance_mean("MedialRatio"),
            "LateralRatio_mean": stance_mean("LateralRatio"),
            "MedialLateralBalance_mean": stance_mean("MedialLateralBalance"),
            "ToeRatio_late_stance_mean": late_mean("ToeRatio"),
            "PushOffRatio_late_stance_mean": late_mean("PushOffRatio"),
            "CoP_AP_early_mean": early_cop_ap,
            "CoP_AP_late_mean": late_cop_ap,
            "CoP_AP_progression": late_cop_ap - early_cop_ap,
            "CoP_ML_mean": stance_mean("CoP_ML"),
            "InitialContactRoll_deg": frame["Roll"].to_numpy(dtype=np.float64)[starts],
            "LandingRoll_mean_deg": early_mean("Roll"),
            "InitialContactPitch_deg": frame["Pitch"].to_numpy(dtype=np.float64)[starts],
            "LandingPitch_mean_deg": early_mean("Pitch"),
            "PitchRange_deg": (
                stance["Pitch"].max() - stance["Pitch"].min()
            ).to_numpy(dtype=np.float64),
            "RollRange_deg": (
                stance["Roll"].max() - stance["Roll"].min()
            ).to_numpy(dtype=np.float64),
            "GyrMag_peak": stance["GyrMag"].max().to_numpy(dtype=np.float64),
            "AccMag_peak": stance["AccMag"].max().to_numpy(dtype=np.float64),
        }
    )
    result["Pattern"] = result.apply(_classify_step, axis=1)
    return result


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
