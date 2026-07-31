from __future__ import annotations

import argparse
import html
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt


DEFAULT_COLUMNS = [
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


@dataclass(frozen=True)
class SensorLayout:
    heel: tuple[str, ...] = ("P2",)
    arch: tuple[str, ...] = ("P3",)
    medial_forefoot: tuple[str, ...] = ("P4",)
    lateral_forefoot: tuple[str, ...] = ("P1",)
    toe: tuple[str, ...] = ()

    @property
    def rear(self) -> tuple[str, ...]:
        return self.heel

    @property
    def front(self) -> tuple[str, ...]:
        return self.medial_forefoot + self.lateral_forefoot + self.toe

    @property
    def medial(self) -> tuple[str, ...]:
        return self.medial_forefoot

    @property
    def lateral(self) -> tuple[str, ...]:
        return self.lateral_forefoot


def sum_pressure_channels(df: pd.DataFrame, channels: tuple[str, ...]) -> pd.Series:
    if not channels:
        return pd.Series(0.0, index=df.index)
    return df[[f"{channel}_smooth" for channel in channels]].sum(axis=1)


def parse_stepwise_txt(path: Path) -> pd.DataFrame:
    rows: list[list[str]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.strip().split()
        if len(parts) == len(DEFAULT_COLUMNS) and parts[0].isdigit():
            rows.append(parts)

    if not rows:
        raise ValueError(f"No data rows found in {path}")

    df = pd.DataFrame(rows, columns=DEFAULT_COLUMNS)
    for col in DEFAULT_COLUMNS:
        if col != "SystemTime":
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["Sample"]).reset_index(drop=True)
    times = pd.to_datetime(df["SystemTime"], format="%H:%M:%S.%f", errors="coerce")
    if times.isna().any():
        raise ValueError("Some SystemTime values cannot be parsed as HH:MM:SS.mmm")

    elapsed = (times - times.iloc[0]).dt.total_seconds()
    elapsed = elapsed.mask(elapsed < 0, elapsed + 24 * 3600)
    df["Time_s"] = elapsed
    return df


def rolling_smooth(series: pd.Series, window: int) -> pd.Series:
    window = max(1, int(window))
    if window % 2 == 0:
        window += 1
    return (
        series.rolling(window=window, center=True, min_periods=1)
        .median()
        .rolling(window=window, center=True, min_periods=1)
        .mean()
    )


def add_basic_features(
    df: pd.DataFrame,
    layout: SensorLayout,
    body_weight_n: float | None,
    smooth_window: int,
) -> pd.DataFrame:
    out = df.copy()
    pressure_cols = ["P1", "P2", "P3", "P4"]

    for col in pressure_cols:
        out[f"{col}_smooth"] = rolling_smooth(out[col].clip(lower=0), smooth_window)

    smooth_cols = [f"{col}_smooth" for col in pressure_cols]
    out["TotalPressure"] = out[smooth_cols].sum(axis=1)
    out["RearPressure"] = sum_pressure_channels(out, layout.rear)
    out["ArchPressure"] = sum_pressure_channels(out, layout.arch)
    out["FrontPressure"] = sum_pressure_channels(out, layout.front)
    out["MedialPressure"] = sum_pressure_channels(out, layout.medial)
    out["LateralPressure"] = sum_pressure_channels(out, layout.lateral)
    out["ToePressure"] = sum_pressure_channels(out, layout.toe)
    out["PushOffPressure"] = out["ToePressure"] if layout.toe else out["FrontPressure"]

    denom = out["TotalPressure"].replace(0, np.nan)
    out["RearRatio"] = out["RearPressure"] / denom
    out["ArchRatio"] = out["ArchPressure"] / denom
    out["FrontRatio"] = out["FrontPressure"] / denom
    out["MedialRatio"] = out["MedialPressure"] / denom
    out["LateralRatio"] = out["LateralPressure"] / denom
    out["ToeRatio"] = out["ToePressure"] / denom if layout.toe else np.nan
    out["PushOffRatio"] = out["PushOffPressure"] / denom
    out["MedialLateralBalance"] = (out["MedialPressure"] - out["LateralPressure"]) / (
        out["MedialPressure"] + out["LateralPressure"]
    ).replace(0, np.nan)

    # Coarse center-of-pressure proxy. Coordinates are normalized by anatomical role:
    # heel=0.0, arch=0.45, forefoot/toe=1.0 on the anterior-posterior axis;
    # medial forefoot is positive and lateral forefoot is negative.
    ap_weights = {col: 0.0 for col in pressure_cols}
    ml_weights = {col: 0.0 for col in pressure_cols}
    for channel in layout.arch:
        ap_weights[channel] = 0.45
    for channel in layout.front:
        ap_weights[channel] = 1.00
    for channel in layout.medial:
        ml_weights[channel] = 0.45
    for channel in layout.lateral:
        ml_weights[channel] = -0.45

    out["CoP_AP"] = sum(out[f"{col}_smooth"] * ap_weights[col] for col in pressure_cols) / denom
    out["CoP_ML"] = sum(out[f"{col}_smooth"] * ml_weights[col] for col in pressure_cols) / denom

    if body_weight_n:
        out["TotalPressure_BW"] = out["TotalPressure"] / body_weight_n

    out["AccMag"] = np.sqrt(out["AccX"] ** 2 + out["AccY"] ** 2 + out["AccZ"] ** 2)
    out["GyrMag"] = np.sqrt(out["GyrX"] ** 2 + out["GyrY"] ** 2 + out["GyrZ"] ** 2)
    return out


def adaptive_threshold(total_pressure: pd.Series, min_threshold_n: float, ratio: float) -> tuple[float, float]:
    peak = float(total_pressure.max())
    enter = max(min_threshold_n, peak * ratio)
    exit_ = enter * 0.55
    return enter, exit_


def hysteresis_contact(total_pressure: pd.Series, enter_threshold: float, exit_threshold: float) -> np.ndarray:
    contact = np.zeros(len(total_pressure), dtype=bool)
    state = False
    for i, value in enumerate(total_pressure.to_numpy()):
        if not state and value >= enter_threshold:
            state = True
        elif state and value <= exit_threshold:
            state = False
        contact[i] = state
    return contact


def contact_intervals(contact: np.ndarray, time_s: pd.Series, min_stance_s: float) -> list[tuple[int, int]]:
    intervals: list[tuple[int, int]] = []
    start: int | None = None
    for i, is_contact in enumerate(contact):
        if is_contact and start is None:
            start = i
        if start is not None and (not is_contact or i == len(contact) - 1):
            end = i - 1 if not is_contact else i
            duration = float(time_s.iloc[end] - time_s.iloc[start])
            if duration >= min_stance_s:
                intervals.append((start, end))
            start = None
    return intervals


def classify_step(row: pd.Series) -> str:
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


def extract_step_features(df: pd.DataFrame, intervals: list[tuple[int, int]]) -> pd.DataFrame:
    steps: list[dict[str, float | int | str]] = []
    previous_start_time: float | None = None

    for step_index, (start_i, end_i) in enumerate(intervals, start=1):
        seg = df.iloc[start_i : end_i + 1]
        early_end = start_i + max(1, int((end_i - start_i + 1) * 0.20))
        early_seg = df.iloc[start_i : early_end + 1]
        late_start = start_i + int((end_i - start_i + 1) * 0.65)
        late_seg = df.iloc[late_start : end_i + 1]

        start_t = float(seg["Time_s"].iloc[0])
        end_t = float(seg["Time_s"].iloc[-1])
        stance_time = end_t - start_t
        stride_time = np.nan if previous_start_time is None else start_t - previous_start_time
        swing_time = np.nan if previous_start_time is None else stride_time - stance_time
        previous_start_time = start_t

        # np.trapz is available in the supported NumPy 1.26-2.x range.
        pressure_impulse = float(np.trapz(seg["TotalPressure"], seg["Time_s"]))
        row = {
            "Step": step_index,
            "StartSample": int(seg["Sample"].iloc[0]),
            "EndSample": int(seg["Sample"].iloc[-1]),
            "StartTime_s": start_t,
            "EndTime_s": end_t,
            "StanceTime_s": stance_time,
            "StrideTime_s": stride_time,
            "SwingTime_s": swing_time,
            "PeakPressure_N": float(seg["TotalPressure"].max()),
            "MeanPressure_N": float(seg["TotalPressure"].mean()),
            "PressureImpulse_Ns": pressure_impulse,
            "EarlyRearRatio_mean": float(early_seg["RearRatio"].mean(skipna=True)),
            "EarlyFrontRatio_mean": float(early_seg["FrontRatio"].mean(skipna=True)),
            "RearRatio_mean": float(seg["RearRatio"].mean(skipna=True)),
            "ArchRatio_mean": float(seg["ArchRatio"].mean(skipna=True)),
            "FrontRatio_mean": float(seg["FrontRatio"].mean(skipna=True)),
            "MedialRatio_mean": float(seg["MedialRatio"].mean(skipna=True)),
            "LateralRatio_mean": float(seg["LateralRatio"].mean(skipna=True)),
            "MedialLateralBalance_mean": float(seg["MedialLateralBalance"].mean(skipna=True)),
            "ToeRatio_late_stance_mean": float(late_seg["ToeRatio"].mean(skipna=True)),
            "PushOffRatio_late_stance_mean": float(late_seg["PushOffRatio"].mean(skipna=True)),
            "CoP_AP_early_mean": float(early_seg["CoP_AP"].mean(skipna=True)),
            "CoP_AP_late_mean": float(late_seg["CoP_AP"].mean(skipna=True)),
            "CoP_AP_progression": float(late_seg["CoP_AP"].mean(skipna=True) - early_seg["CoP_AP"].mean(skipna=True)),
            "CoP_ML_mean": float(seg["CoP_ML"].mean(skipna=True)),
            "InitialContactRoll_deg": float(seg["Roll"].iloc[0]),
            "LandingRoll_mean_deg": float(early_seg["Roll"].mean(skipna=True)),
            "InitialContactPitch_deg": float(seg["Pitch"].iloc[0]),
            "LandingPitch_mean_deg": float(early_seg["Pitch"].mean(skipna=True)),
            "PitchRange_deg": float(seg["Pitch"].max() - seg["Pitch"].min()),
            "RollRange_deg": float(seg["Roll"].max() - seg["Roll"].min()),
            "GyrMag_peak": float(seg["GyrMag"].max()),
            "AccMag_peak": float(seg["AccMag"].max()),
        }
        row["Pattern"] = classify_step(pd.Series(row))
        steps.append(row)

    return pd.DataFrame(steps)


def summarize_session(df: pd.DataFrame, steps: pd.DataFrame, enter_threshold: float, exit_threshold: float) -> dict:
    duration = float(df["Time_s"].iloc[-1] - df["Time_s"].iloc[0])
    dt = df["Time_s"].diff().dropna()
    hz = float((len(df) - 1) / duration) if duration > 0 and len(df) > 1 else None
    positive_dt = dt[dt > 0]
    median_positive_dt = float(positive_dt.median()) if len(positive_dt) else None
    duplicate_timestamp_count = int((dt == 0).sum())
    cadence = float(len(steps) / duration * 60.0) if duration > 0 else None

    warnings: list[str] = []
    if hz is not None and hz < 50:
        warnings.append("Actual sample rate is below 50 Hz; gait-event timing may be unreliable.")
    if len(steps) < 6:
        warnings.append("Too few detected stance phases for robust gait screening.")
    if (df[["P1", "P2", "P3", "P4"]].max() == 0).any():
        zero_cols = df[["P1", "P2", "P3", "P4"]].max()
        warnings.append("Some pressure channels are always zero: " + ", ".join(zero_cols[zero_cols == 0].index))
    if duplicate_timestamp_count:
        warnings.append(
            f"{duplicate_timestamp_count} repeated timestamps were found; sample rate is estimated from total duration."
        )

    if hz is None or hz < 20 or len(steps) < 4:
        quality = "Low"
    elif hz < 50 or len(steps) < 8:
        quality = "Medium"
    else:
        quality = "High"

    return {
        "samples": int(len(df)),
        "duration_s": duration,
        "estimated_sample_rate_hz": hz,
        "median_positive_dt_s": median_positive_dt,
        "duplicate_timestamp_count": duplicate_timestamp_count,
        "detected_steps_single_foot": int(len(steps)),
        "estimated_single_foot_cadence_per_min": cadence,
        "contact_enter_threshold_n": enter_threshold,
        "contact_exit_threshold_n": exit_threshold,
        "mean_stance_time_s": None if steps.empty else float(steps["StanceTime_s"].mean()),
        "mean_stride_time_s": None if steps.empty else float(steps["StrideTime_s"].mean(skipna=True)),
        "data_quality": quality,
        "warnings": warnings,
    }


def percentage(condition: pd.Series) -> float:
    if len(condition) == 0:
        return 0.0
    return float(condition.mean())


def add_finding(
    findings: list[dict[str, str]],
    title: str,
    severity: str,
    evidence: str,
    interpretation: str,
    suggestion: str,
    limitation: str,
) -> None:
    findings.append(
        {
            "title": title,
            "severity": severity,
            "evidence": evidence,
            "interpretation": interpretation,
            "suggestion": suggestion,
            "limitation": limitation,
        }
    )


def build_screening_findings(steps: pd.DataFrame, summary: dict, demo_mode: bool = False) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    quality = summary.get("data_quality", "Low")

    if quality == "Low":
        add_finding(
            findings,
            "Low-confidence trial" if not demo_mode else "Low-confidence trial (demo mode enabled)",
            "High priority",
            "Estimated sample rate, detected step count, or pressure-channel status is insufficient for robust screening.",
            "This trial should mainly be used to verify data parsing and visualization."
            if not demo_mode
            else "The following findings are forced to display for software demonstration only.",
            "Repeat the walking test after confirming WiFi packet rate, sensor wiring, and insole placement. Aim for stable 50-100 Hz and at least 10-20 valid stance phases.",
            "No gait-pattern conclusion should be treated as reliable when data quality is Low.",
        )
        if not demo_mode:
            return findings

    if steps.empty:
        return findings

    mean_rear = float(steps["RearRatio_mean"].mean(skipna=True))
    mean_arch = float(steps["ArchRatio_mean"].mean(skipna=True))
    mean_front = float(steps["FrontRatio_mean"].mean(skipna=True))
    mean_medial = float(steps["MedialRatio_mean"].mean(skipna=True))
    mean_lateral = float(steps["LateralRatio_mean"].mean(skipna=True))
    mean_push_late = float(steps["PushOffRatio_late_stance_mean"].mean(skipna=True))

    rear_rate = percentage(steps["RearRatio_mean"] >= 0.70)
    front_rate = percentage(steps["FrontRatio_mean"] >= 0.70)
    arch_rate = percentage(steps["ArchRatio_mean"] >= 0.25)
    medial_rate = percentage(steps["MedialRatio_mean"] >= 0.65)
    lateral_rate = percentage(steps["LateralRatio_mean"] >= 0.65)
    weak_push_rate = percentage(steps["PushOffRatio_late_stance_mean"] < 0.30)

    stride_cv = None
    stride_values = steps["StrideTime_s"].dropna()
    if len(stride_values) >= 3 and stride_values.mean() > 0:
        stride_cv = float(stride_values.std(ddof=0) / stride_values.mean())

    if rear_rate >= 0.50:
        add_finding(
            findings,
            "Rearfoot-heavy loading pattern",
            "Screening indicator",
            f"Mean rearfoot ratio = {mean_rear:.2f}; {rear_rate:.0%} of detected stance phases exceeded 0.70.",
            "The foot loading is concentrated more on the heel/rearfoot region during stance.",
            "Check whether the heel sensor is correctly positioned. If this pattern repeats in a high-quality trial, consider basic ankle mobility work and controlled heel-to-toe walking practice.",
            "This is not a diagnosis of heel pathology; it is only a plantar-loading distribution indicator.",
        )

    if front_rate >= 0.50:
        add_finding(
            findings,
            "Forefoot-heavy loading pattern",
            "Screening indicator",
            f"Mean forefoot ratio = {mean_front:.2f}; {front_rate:.0%} of detected stance phases exceeded 0.70.",
            "The foot loading is concentrated more on the metatarsal/toe region during stance.",
            "Check shoe comfort and sensor placement. If repeated, consider calf relaxation, foot intrinsic muscle training, and a slower retest with normal walking speed.",
            "This does not diagnose metatarsalgia or any foot disease.",
        )

    if medial_rate >= 0.50:
        add_finding(
            findings,
            "Medial loading bias",
            "Screening indicator",
            f"Mean medial ratio = {mean_medial:.2f}; {medial_rate:.0%} of detected stance phases exceeded 0.65.",
            "Pressure is biased toward the medial forefoot sensor region.",
            "Inspect insole alignment and shoe wear on the medial side. If repeated, basic foot-arch control and single-leg balance exercises may be used as rehabilitation-support suggestions.",
            "This does not diagnose flat foot, pronation, or clinical foot eversion.",
        )

    if arch_rate >= 0.50:
        add_finding(
            findings,
            "High arch/midfoot loading candidate",
            "Screening indicator",
            f"Mean arch/midfoot ratio = {mean_arch:.2f}; {arch_rate:.0%} of detected stance phases exceeded 0.25.",
            "The arch sensor carries a relatively high share of plantar load during stance.",
            "Confirm that P2 is located under the arch and not shifted toward the forefoot. If repeated in high-quality trials, this can be reported as an arch/midfoot loading indicator and compared with a local healthy baseline.",
            "This does not diagnose flat foot, pronation, or arch collapse. It is only a midfoot-contact/loading proxy.",
        )

    if lateral_rate >= 0.50:
        add_finding(
            findings,
            "Lateral loading bias",
            "Screening indicator",
            f"Mean lateral ratio = {mean_lateral:.2f}; {lateral_rate:.0%} of detected stance phases exceeded 0.65.",
            "Pressure is biased toward the lateral forefoot sensor region.",
            "Inspect insole alignment and shoe wear on the lateral side. If repeated, ankle stability and balance exercises may be used as rehabilitation-support suggestions.",
            "This does not diagnose high arch, supination, or clinical foot inversion.",
        )

    if weak_push_rate >= 0.50:
        add_finding(
            findings,
            "Weak push-off candidate",
            "Screening indicator",
            f"Mean late-stance forefoot push-off proxy = {mean_push_late:.2f}; {weak_push_rate:.0%} of detected stance phases were below 0.30.",
            "Forefoot pressure participation appears low near the end of stance.",
            "Confirm that P3/P4 are located under the first and fifth metatarsal regions. If repeated in high-quality trials, calf raises, toe-grip exercises, and resisted ankle plantarflexion can be suggested.",
            "This is not a diagnosis of calf weakness or neurological impairment.",
        )

    if stride_cv is not None and stride_cv >= 0.08:
        add_finding(
            findings,
            "Irregular step timing candidate",
            "Screening indicator",
            f"Stride-time coefficient of variation = {stride_cv:.1%}.",
            "Detected stride timing varies noticeably across steps.",
            "Repeat the test with a straight walking path and stable speed. Metronome-paced walking can be used for a simple rhythm-training demonstration.",
            "Single-foot stride timing is not enough for clinical gait variability assessment.",
        )

    if not findings:
        add_finding(
            findings,
            "Balanced-like screening result",
            "Normal-like indicator",
            "No engineering screening rule was triggered by the detected stance phases.",
            "The detected plantar-loading and timing features appear balanced under the current thresholds.",
            "Repeat with more steps and compare against a local healthy baseline to improve confidence.",
            "Normal-like screening does not rule out clinical gait issues.",
        )

    if demo_mode and quality == "Low":
        for finding in findings[1:]:
            finding["severity"] = f"Demo only - {finding['severity']}"
            finding["limitation"] = (
                "Demo mode is enabled on low-quality data. "
                + finding["limitation"]
            )

    return findings


def risk_level(score: int) -> str:
    if score >= 2:
        return "High"
    if score == 1:
        return "Medium"
    return "Low"


def add_risk_flag(
    flags: list[dict[str, str]],
    module: str,
    level: str,
    title: str,
    evidence: str,
    user_message: str,
    action: str,
    literature_basis: str,
) -> None:
    flags.append(
        {
            "module": module,
            "level": level,
            "title": title,
            "evidence": evidence,
            "user_message": user_message,
            "action": action,
            "literature_basis": literature_basis,
        }
    )


def build_risk_flags(steps: pd.DataFrame, summary: dict) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    quality = str(summary.get("data_quality", "Low"))

    if quality == "High":
        data_level = "Low"
        data_msg = "本次数据质量较好，可用于工程筛查。"
        data_action = "建议继续采集更多正常基线和不同步态模式数据，用于个体化阈值。"
    elif quality == "Medium":
        data_level = "Medium"
        data_msg = "本次数据可用于初步观察，但建议增加有效步数或提高采样稳定性。"
        data_action = "建议重新测试一次，目标为 50-100 Hz、至少 10-20 个有效触地段。"
    else:
        data_level = "High"
        data_msg = "本次数据质量不足，不建议解释具体步态风险。"
        data_action = "请先检查传感器、采样率、连接和鞋垫固定，再重新采集。"

    add_risk_flag(
        flags,
        "1. 数据质量",
        data_level,
        "数据可信度风险",
        f"Data quality = {quality}; sample rate = {format_value(summary.get('estimated_sample_rate_hz'))} Hz; detected stance phases = {summary.get('detected_steps_single_foot')}.",
        data_msg,
        data_action,
        "可穿戴步态分析依赖稳定采样率、事件时间和足够步数；gaitmap 和 wearable gait reviews 均以时序事件为基础。",
    )

    if steps.empty or quality == "Low":
        return flags

    stride_values = steps["StrideTime_s"].dropna()
    stride_cv = None
    if len(stride_values) >= 3 and stride_values.mean() > 0:
        stride_cv = float(stride_values.std(ddof=0) / stride_values.mean())
    rhythm_score = 0
    if stride_cv is not None and stride_cv >= 0.12:
        rhythm_score = 2
    elif stride_cv is not None and stride_cv >= 0.08:
        rhythm_score = 1
    add_risk_flag(
        flags,
        "2. 步态节律",
        risk_level(rhythm_score),
        "步态节律波动风险",
        "Stride-time CV = " + ("-" if stride_cv is None else f"{stride_cv:.1%}"),
        "同一只脚连续触地周期越稳定，步态节律越稳定；当前结果用于筛查节律波动。",
        "若风险为 Medium/High，建议直线复测并保持自然速度；可用节拍器做节律训练演示。",
        "Stride time、stance time、cadence 和 variability 是常用时空步态参数。",
    )

    early_front_rate = percentage(steps["EarlyFrontRatio_mean"] >= 0.65)
    early_rear_rate = percentage(steps["EarlyRearRatio_mean"] >= 0.45)
    landing_score = 0
    landing_title = "着地方式风险较低"
    landing_msg = "早期触地阶段未显示明显前掌提前接触或后跟缺失。"
    if early_front_rate >= 0.50:
        landing_score = 1
        landing_title = "前掌提前接触倾向"
        landing_msg = "早期触地阶段前掌压力占比较高，可能提示 forefoot-first contact tendency。"
    if early_rear_rate < 0.30:
        landing_score = max(landing_score, 1)
        landing_title = "后跟初始接触不足倾向"
        landing_msg = "早期触地阶段后跟压力占比较低，建议复测确认。"
    add_risk_flag(
        flags,
        "3. 着地方式",
        risk_level(landing_score),
        landing_title,
        f"EarlyFrontRatio>=0.65 in {early_front_rate:.0%} steps; EarlyRearRatio>=0.45 in {early_rear_rate:.0%} steps.",
        landing_msg,
        "若持续出现，建议检查鞋垫是否移位，并用侧面视频确认 initial contact 模式。",
        "FSR 鞋垫研究用 heel/metatarsal/toe 区域区分 heel strike、full contact、heel off、toe off。",
    )

    cop_prog = steps["CoP_AP_progression"].dropna()
    mean_cop_prog = float(cop_prog.mean()) if len(cop_prog) else np.nan
    weak_push_rate = percentage(steps["PushOffRatio_late_stance_mean"] < 0.30)
    transfer_score = 0
    transfer_title = "足底压力转移风险较低"
    transfer_msg = "压力中心整体呈现从后向前转移的趋势。"
    if not np.isnan(mean_cop_prog) and mean_cop_prog < 0.15:
        transfer_score = 1
        transfer_title = "足底压力前移不足倾向"
        transfer_msg = "早期到晚期触地的粗略压力中心前移幅度较小。"
    if weak_push_rate >= 0.50:
        transfer_score = max(transfer_score, 1)
        transfer_title = "晚期前掌推蹬参与不足倾向"
        transfer_msg = "晚期触地阶段前掌压力参与偏低，可能提示 push-off participation 较弱。"
    add_risk_flag(
        flags,
        "4. 足底压力转移",
        risk_level(transfer_score),
        transfer_title,
        f"Mean CoP_AP progression = {format_value(mean_cop_prog)}; weak push-off proxy in {weak_push_rate:.0%} steps.",
        transfer_msg,
        "若持续出现，建议做提踵、足趾抓地、踝跖屈训练；同时用更多步数复测。",
        "Pedobarography 常报告 CoP progression、pressure-time integral 和 gait phase pressure transfer。",
    )

    rear_rate = percentage(steps["RearRatio_mean"] >= 0.70)
    front_rate = percentage(steps["FrontRatio_mean"] >= 0.70)
    arch_rate = percentage(steps["ArchRatio_mean"] >= 0.25)
    medial_rate = percentage(steps["MedialRatio_mean"] >= 0.65)
    lateral_rate = percentage(steps["LateralRatio_mean"] >= 0.65)
    dist_score = 0
    dist_parts: list[str] = []
    if rear_rate >= 0.50:
        dist_score += 1
        dist_parts.append("后跟受力偏重")
    if front_rate >= 0.50:
        dist_score += 1
        dist_parts.append("前掌受力偏重")
    if arch_rate >= 0.50:
        dist_score += 1
        dist_parts.append("足弓/中足受力偏高")
    if medial_rate >= 0.50:
        dist_score += 1
        dist_parts.append("内侧前掌偏载")
    if lateral_rate >= 0.50:
        dist_score += 1
        dist_parts.append("外侧前掌偏载")
    dist_title = "足底受力分布风险较低" if not dist_parts else "足底受力分布偏移提示"
    dist_msg = "当前未见明显前后足或内外侧偏载。" if not dist_parts else "本次检测到：" + "、".join(dist_parts) + "。"
    add_risk_flag(
        flags,
        "5. 内外侧/前后足受力分布",
        risk_level(min(dist_score, 2)),
        dist_title,
        f"rear={rear_rate:.0%}, front={front_rate:.0%}, arch={arch_rate:.0%}, medial={medial_rate:.0%}, lateral={lateral_rate:.0%} of steps crossed engineering thresholds.",
        dist_msg,
        "若同类偏载在多次测试中重复出现，建议检查鞋底磨损、鞋垫位置，并结合视频或专业评估确认。",
        "Plantar pressure/pedobarography 文献常用 peak pressure、pressure-time integral、contact time、region distribution、CoP 等变量。",
    )

    retest_level = "Low" if quality == "High" and not dist_parts and transfer_score == 0 and rhythm_score == 0 else "Medium"
    add_risk_flag(
        flags,
        "6. 建议与复测提示",
        retest_level,
        "复测与进一步确认建议",
        f"Generated from data quality={quality}, rhythm risk={risk_level(rhythm_score)}, transfer risk={risk_level(transfer_score)}, distribution risk={risk_level(min(dist_score, 2))}.",
        "本系统适合作为低成本筛查工具。单次结果不能替代专业评估。",
        "建议每种模式采集 3 次，每次 20-30 个有效触地段；若需要判断 toe-in/toe-out，请加入俯视视频或 Foot Progression Angle 算法。",
        "Foot progression angle 文献说明 toe-in/toe-out 的严格判断需要足部前后轴相对行走方向的角度；压力只能提示相关受力模式。",
    )

    return flags


def plot_results(df: pd.DataFrame, steps: pd.DataFrame, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(13, 6))
    for col in ["P1_smooth", "P2_smooth", "P3_smooth", "P4_smooth"]:
        ax.plot(df["Time_s"], df[col], label=col.replace("_smooth", ""))
    ax.plot(df["Time_s"], df["TotalPressure"], label="Total", linewidth=2.2, color="black")
    for _, step in steps.iterrows():
        ax.axvspan(step["StartTime_s"], step["EndTime_s"], color="tab:green", alpha=0.12)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Force (N)")
    ax.set_title("Plantar pressure and detected stance phases")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(output_dir / "pressure_stance.png", dpi=180)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(13, 6))
    ax.plot(df["Time_s"], df["AccX"], label="AccX")
    ax.plot(df["Time_s"], df["AccY"], label="AccY")
    ax.plot(df["Time_s"], df["AccZ"], label="AccZ")
    ax.plot(df["Time_s"], df["AccMag"], label="AccMag", linewidth=2.0, color="black")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Acceleration")
    ax.set_title("IMU acceleration")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(output_dir / "acceleration.png", dpi=180)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(13, 6))
    for col in ["Pitch", "Roll", "Yaw"]:
        ax.plot(df["Time_s"], df[col], label=col)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Angle (deg)")
    ax.set_title("Foot orientation")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(output_dir / "orientation.png", dpi=180)
    plt.close(fig)


def format_value(value: object, digits: int = 3) -> str:
    if value is None:
        return "-"
    try:
        if pd.isna(value):
            return "-"
    except TypeError:
        pass
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def write_text_report(summary: dict, steps: pd.DataFrame, findings: list[dict[str, str]], output_dir: Path) -> None:
    lines = [
        "StepWise Gait Analysis Report",
        "=" * 32,
        "",
        "Session Summary",
        "-" * 16,
        f"Samples: {summary['samples']}",
        f"Duration: {format_value(summary['duration_s'])} s",
        f"Estimated sample rate: {format_value(summary['estimated_sample_rate_hz'])} Hz",
        f"Detected stance phases: {summary['detected_steps_single_foot']}",
        f"Estimated single-foot cadence: {format_value(summary['estimated_single_foot_cadence_per_min'])} /min",
        f"Contact threshold enter/exit: {format_value(summary['contact_enter_threshold_n'])} N / {format_value(summary['contact_exit_threshold_n'])} N",
        f"Mean stance time: {format_value(summary['mean_stance_time_s'])} s",
        f"Mean stride time: {format_value(summary['mean_stride_time_s'])} s",
        f"Data quality: {summary.get('data_quality', '-')}",
        "",
        "Quality Warnings",
        "-" * 16,
    ]

    warnings = summary.get("warnings") or []
    lines.extend([f"- {warning}" for warning in warnings] if warnings else ["- None"])
    lines.extend(["", "Screening Findings", "-" * 18])

    for i, finding in enumerate(findings, start=1):
        lines.extend(
            [
                f"{i}. {finding['title']} [{finding['severity']}]",
                f"   Evidence: {finding['evidence']}",
                f"   Interpretation: {finding['interpretation']}",
                f"   Suggested action: {finding['suggestion']}",
                f"   Limitation: {finding['limitation']}",
                "",
            ]
        )

    lines.extend(["", "Detected Step Features", "-" * 22])

    if steps.empty:
        lines.append("No valid stance phase was detected.")
    else:
        display_cols = [
            "Step",
            "StartTime_s",
            "EndTime_s",
            "StanceTime_s",
            "StrideTime_s",
            "PeakPressure_N",
            "RearRatio_mean",
            "ArchRatio_mean",
            "FrontRatio_mean",
            "MedialRatio_mean",
            "LateralRatio_mean",
            "PushOffRatio_late_stance_mean",
            "Pattern",
        ]
        lines.append(steps[display_cols].to_string(index=False))

    lines.extend(
        [
            "",
            "Interpretation Note",
            "-" * 19,
            "This is an engineering screening report, not a medical diagnosis.",
            "Reliable gait-event analysis usually needs stable 50-100 Hz sampling and calibrated pressure sensors.",
        ]
    )
    (output_dir / "report.txt").write_text("\n".join(lines), encoding="utf-8")


def write_html_report(summary: dict, steps: pd.DataFrame, findings: list[dict[str, str]], output_dir: Path) -> None:
    warning_items = "".join(
        f"<li>{html.escape(str(warning))}</li>" for warning in (summary.get("warnings") or ["None"])
    )

    metrics = [
        ("Samples", summary["samples"]),
        ("Duration", f"{format_value(summary['duration_s'])} s"),
        ("Estimated sample rate", f"{format_value(summary['estimated_sample_rate_hz'])} Hz"),
        ("Detected stance phases", summary["detected_steps_single_foot"]),
        ("Single-foot cadence", f"{format_value(summary['estimated_single_foot_cadence_per_min'])} /min"),
        ("Enter threshold", f"{format_value(summary['contact_enter_threshold_n'])} N"),
        ("Exit threshold", f"{format_value(summary['contact_exit_threshold_n'])} N"),
        ("Mean stance time", f"{format_value(summary['mean_stance_time_s'])} s"),
        ("Mean stride time", f"{format_value(summary['mean_stride_time_s'])} s"),
        ("Data quality", summary.get("data_quality", "-")),
    ]
    metric_cards = "\n".join(
        f"<div class='metric'><span>{html.escape(label)}</span><strong>{html.escape(str(value))}</strong></div>"
        for label, value in metrics
    )

    if steps.empty:
        step_table = "<p class='empty'>No valid stance phase was detected.</p>"
    else:
        display_cols = [
            "Step",
            "StartTime_s",
            "EndTime_s",
            "StanceTime_s",
            "StrideTime_s",
            "PeakPressure_N",
            "RearRatio_mean",
            "ArchRatio_mean",
            "FrontRatio_mean",
            "MedialRatio_mean",
            "LateralRatio_mean",
            "PushOffRatio_late_stance_mean",
            "Pattern",
        ]
        renamed = {
            "StartTime_s": "Start (s)",
            "EndTime_s": "End (s)",
            "StanceTime_s": "Stance (s)",
            "StrideTime_s": "Stride (s)",
            "PeakPressure_N": "Peak Pressure (N)",
            "RearRatio_mean": "Rear Ratio",
            "ArchRatio_mean": "Arch Ratio",
            "FrontRatio_mean": "Front Ratio",
            "MedialRatio_mean": "Medial Ratio",
            "LateralRatio_mean": "Lateral Ratio",
            "PushOffRatio_late_stance_mean": "Late Push-off Proxy",
        }
        table_df = steps[display_cols].rename(columns=renamed).copy()
        for col in table_df.columns:
            if col != "Pattern":
                table_df[col] = table_df[col].map(lambda value: format_value(value))
        step_table = table_df.to_html(index=False, escape=True, classes="steps")

    finding_cards = "\n".join(
        f"""
        <article class="finding">
          <div class="finding-head">
            <h3>{html.escape(finding['title'])}</h3>
            <span>{html.escape(finding['severity'])}</span>
          </div>
          <p><strong>Evidence:</strong> {html.escape(finding['evidence'])}</p>
          <p><strong>Interpretation:</strong> {html.escape(finding['interpretation'])}</p>
          <p><strong>Suggested action:</strong> {html.escape(finding['suggestion'])}</p>
          <p class="small"><strong>Limitation:</strong> {html.escape(finding['limitation'])}</p>
        </article>
        """
        for finding in findings
    )

    report_html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>StepWise Gait Analysis Report</title>
  <style>
    body {{
      margin: 0;
      font-family: Arial, Helvetica, sans-serif;
      color: #1f2933;
      background: #f5f7fa;
    }}
    header {{
      background: #17324d;
      color: white;
      padding: 28px 36px;
    }}
    header h1 {{
      margin: 0 0 8px;
      font-size: 30px;
      letter-spacing: 0;
    }}
    header p {{
      margin: 0;
      color: #d9e5f2;
    }}
    main {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 28px;
    }}
    section {{
      margin-bottom: 28px;
      background: white;
      border: 1px solid #d9e2ec;
      border-radius: 8px;
      padding: 22px;
    }}
    h2 {{
      margin: 0 0 16px;
      font-size: 20px;
      color: #17324d;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 12px;
    }}
    .metric {{
      border: 1px solid #d9e2ec;
      border-radius: 8px;
      padding: 14px;
      background: #fbfcfe;
    }}
    .metric span {{
      display: block;
      font-size: 13px;
      color: #52616f;
      margin-bottom: 8px;
    }}
    .metric strong {{
      font-size: 22px;
      color: #102a43;
    }}
    .warnings {{
      border-left: 5px solid #d64545;
      background: #fff8f8;
    }}
    .warnings ul {{
      margin: 0;
      padding-left: 22px;
    }}
    .finding {{
      border: 1px solid #d9e2ec;
      border-radius: 8px;
      padding: 16px;
      margin-bottom: 14px;
      background: #fbfcfe;
    }}
    .finding-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin-bottom: 8px;
    }}
    .finding h3 {{
      margin: 0;
      color: #102a43;
      font-size: 18px;
    }}
    .finding span {{
      background: #dbeafe;
      color: #17324d;
      border-radius: 999px;
      padding: 5px 10px;
      white-space: nowrap;
      font-size: 13px;
      font-weight: 700;
    }}
    .finding p {{
      margin: 8px 0;
      line-height: 1.5;
    }}
    .small {{
      color: #52616f;
      font-size: 13px;
    }}
    table.steps {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    table.steps th, table.steps td {{
      border-bottom: 1px solid #d9e2ec;
      padding: 10px 8px;
      text-align: left;
      vertical-align: top;
    }}
    table.steps th {{
      background: #eef3f8;
      color: #17324d;
    }}
    .charts {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 18px;
    }}
    .charts img {{
      width: 100%;
      border: 1px solid #d9e2ec;
      border-radius: 8px;
      background: white;
    }}
    .note {{
      color: #52616f;
      line-height: 1.6;
    }}
  </style>
</head>
<body>
  <header>
    <h1>StepWise Gait Analysis Report</h1>
    <p>Smart insole pressure and IMU screening output</p>
  </header>
  <main>
    <section>
      <h2>Session Summary</h2>
      <div class="metrics">
        {metric_cards}
      </div>
    </section>
    <section class="warnings">
      <h2>Quality Warnings</h2>
      <ul>{warning_items}</ul>
    </section>
    <section>
      <h2>Screening Findings</h2>
      {finding_cards}
    </section>
    <section>
      <h2>Detected Step Features</h2>
      {step_table}
    </section>
    <section>
      <h2>Charts</h2>
      <div class="charts">
        <img src="pressure_stance.png" alt="Pressure and stance phases">
        <img src="acceleration.png" alt="IMU acceleration">
        <img src="orientation.png" alt="Foot orientation">
      </div>
    </section>
    <section>
      <h2>Interpretation Note</h2>
      <p class="note">
        This report is for engineering gait screening and prototype verification.
        It is not a medical diagnosis. Reliable gait-event analysis usually needs
        stable 50-100 Hz sampling and calibrated pressure sensors.
      </p>
    </section>
  </main>
</body>
</html>
"""
    (output_dir / "report.html").write_text(report_html, encoding="utf-8")


def user_result_title(findings: list[dict[str, str]], summary: dict) -> tuple[str, str]:
    if summary.get("data_quality") == "Low":
        return "本次数据质量不足，建议重新测试", "本报告仅用于确认设备采集流程，不建议解读为步态结果。"
    if findings and findings[0]["title"].startswith("Balanced-like"):
        return "本次筛查未发现明显异常倾向", "当前工程指标下，足底受力和步态节律整体较均衡。"
    return "本次筛查发现若干步态倾向", "这些结果是工程筛查提示，不是医学诊断。"


def write_user_report(
    summary: dict,
    steps: pd.DataFrame,
    findings: list[dict[str, str]],
    risk_flags: list[dict[str, str]],
    output_dir: Path,
) -> None:
    title, subtitle = user_result_title(findings, summary)
    usable_findings = [f for f in findings if not f["title"].startswith("Low-confidence")]
    if not usable_findings:
        usable_findings = findings

    quality_label = summary.get("data_quality", "-")
    quality_class = {"High": "good", "Medium": "medium", "Low": "low"}.get(str(quality_label), "medium")

    finding_cards = "\n".join(
        f"""
        <article class="finding">
          <h3>{html.escape(f['title'])}</h3>
          <p><strong>为什么这么说：</strong>{html.escape(f['evidence'])}</p>
          <p><strong>这代表什么：</strong>{html.escape(f['interpretation'])}</p>
          <p><strong>建议：</strong>{html.escape(f['suggestion'])}</p>
          <p class="limit">{html.escape(f['limitation'])}</p>
        </article>
        """
        for f in usable_findings
    )
    risk_cards = "\n".join(
        f"""
        <article class="risk risk-{html.escape(flag['level'].lower())}">
          <div class="risk-head">
            <h3>{html.escape(flag['module'])}: {html.escape(flag['title'])}</h3>
            <span>{html.escape(flag['level'])}</span>
          </div>
          <p><strong>证据：</strong>{html.escape(flag['evidence'])}</p>
          <p><strong>提示：</strong>{html.escape(flag['user_message'])}</p>
          <p><strong>建议：</strong>{html.escape(flag['action'])}</p>
        </article>
        """
        for flag in risk_flags
    )

    avg_front = "-" if steps.empty else format_value(float(steps["FrontRatio_mean"].mean(skipna=True)))
    avg_rear = "-" if steps.empty else format_value(float(steps["RearRatio_mean"].mean(skipna=True)))
    avg_medial = "-" if steps.empty else format_value(float(steps["MedialRatio_mean"].mean(skipna=True)))
    avg_lateral = "-" if steps.empty else format_value(float(steps["LateralRatio_mean"].mean(skipna=True)))
    avg_arch = "-" if steps.empty else format_value(float(steps["ArchRatio_mean"].mean(skipna=True)))

    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>StepWise 用户步态筛查报告</title>
  <style>
    body {{ margin: 0; font-family: Arial, "Microsoft YaHei", sans-serif; background: #f6f8fb; color: #17202a; }}
    header {{ padding: 34px 44px; background: #12324a; color: white; }}
    header h1 {{ margin: 0 0 10px; font-size: 30px; }}
    header p {{ margin: 0; color: #d9e9f5; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 28px; }}
    section {{ background: white; border: 1px solid #d7e0ea; border-radius: 10px; padding: 22px; margin-bottom: 22px; }}
    .hero {{ border-left: 8px solid #2f80ed; }}
    .hero h2 {{ margin: 0 0 8px; font-size: 26px; color: #12324a; }}
    .hero p {{ margin: 0; line-height: 1.6; color: #52616f; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }}
    .metric {{ border: 1px solid #d7e0ea; border-radius: 8px; padding: 14px; background: #fbfdff; }}
    .metric span {{ display: block; color: #64748b; font-size: 13px; margin-bottom: 8px; }}
    .metric strong {{ color: #0f2f48; font-size: 22px; }}
    .quality {{ display: inline-block; padding: 6px 12px; border-radius: 999px; font-weight: 700; }}
    .good {{ background: #dcfce7; color: #166534; }}
    .medium {{ background: #fef3c7; color: #92400e; }}
    .low {{ background: #fee2e2; color: #991b1b; }}
    h2 {{ margin: 0 0 16px; color: #12324a; font-size: 21px; }}
    .finding {{ border: 1px solid #d7e0ea; border-radius: 8px; padding: 16px; margin-bottom: 14px; background: #fbfdff; }}
    .finding h3 {{ margin: 0 0 10px; color: #0f2f48; }}
    .finding p {{ margin: 8px 0; line-height: 1.55; }}
    .limit {{ color: #64748b; font-size: 13px; }}
    .risk {{ border: 1px solid #d7e0ea; border-radius: 8px; padding: 16px; margin-bottom: 14px; background: #fbfdff; }}
    .risk-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; }}
    .risk h3 {{ margin: 0; color: #0f2f48; font-size: 17px; }}
    .risk span {{ border-radius: 999px; padding: 5px 11px; font-weight: 700; white-space: nowrap; }}
    .risk-low span {{ background: #dcfce7; color: #166534; }}
    .risk-medium span {{ background: #fef3c7; color: #92400e; }}
    .risk-high span {{ background: #fee2e2; color: #991b1b; }}
    .risk p {{ margin: 8px 0; line-height: 1.55; }}
    .charts img {{ width: 100%; border: 1px solid #d7e0ea; border-radius: 8px; margin-bottom: 16px; }}
    .note {{ color: #52616f; line-height: 1.7; }}
  </style>
</head>
<body>
  <header>
    <h1>StepWise 用户步态筛查报告</h1>
    <p>基于智能鞋垫压力传感器与足部 IMU 的非诊断性筛查结果</p>
  </header>
  <main>
    <section class="hero">
      <h2>{html.escape(title)}</h2>
      <p>{html.escape(subtitle)}</p>
    </section>

    <section>
      <h2>测试概况</h2>
      <div class="grid">
        <div class="metric"><span>数据质量</span><strong><span class="quality {quality_class}">{html.escape(str(quality_label))}</span></strong></div>
        <div class="metric"><span>有效触地段</span><strong>{summary['detected_steps_single_foot']}</strong></div>
        <div class="metric"><span>测试时长</span><strong>{format_value(summary['duration_s'])} s</strong></div>
        <div class="metric"><span>估计采样率</span><strong>{format_value(summary['estimated_sample_rate_hz'])} Hz</strong></div>
        <div class="metric"><span>平均触地时间</span><strong>{format_value(summary['mean_stance_time_s'])} s</strong></div>
        <div class="metric"><span>平均同脚步周期</span><strong>{format_value(summary['mean_stride_time_s'])} s</strong></div>
      </div>
    </section>

    <section>
      <h2>筛查结果与建议</h2>
      {finding_cards}
    </section>

    <section>
      <h2>六项风险提示</h2>
      {risk_cards}
    </section>

    <section>
      <h2>足底受力概览</h2>
      <div class="grid">
        <div class="metric"><span>后跟平均占比</span><strong>{avg_rear}</strong></div>
        <div class="metric"><span>足弓平均占比</span><strong>{avg_arch}</strong></div>
        <div class="metric"><span>前掌平均占比</span><strong>{avg_front}</strong></div>
        <div class="metric"><span>内侧前掌平均占比</span><strong>{avg_medial}</strong></div>
        <div class="metric"><span>外侧前掌平均占比</span><strong>{avg_lateral}</strong></div>
      </div>
    </section>

    <section>
      <h2>波形图</h2>
      <div class="charts">
        <img src="pressure_stance.png" alt="足底压力与触地阶段">
        <img src="orientation.png" alt="足部姿态角">
      </div>
    </section>

    <section>
      <h2>重要说明</h2>
      <p class="note">本报告是工程筛查结果，不是临床诊断。若多次测试持续出现同类异常倾向，建议结合视频、足进角测量或专业设备进一步评估。</p>
    </section>
  </main>
</body>
</html>
"""
    (output_dir / "user_report.html").write_text(html_text, encoding="utf-8")


def write_data_guide(output_dir: Path) -> None:
    guide_html = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>StepWise 数据分析指标指南</title>
  <style>
    body { margin: 0; font-family: Arial, "Microsoft YaHei", sans-serif; color: #17202a; background: #f7f9fc; }
    header { background: #17324d; color: white; padding: 34px 42px; }
    header h1 { margin: 0 0 8px; font-size: 30px; }
    header p { margin: 0; color: #d9e9f5; }
    main { max-width: 1180px; margin: 0 auto; padding: 28px; }
    section { background: white; border: 1px solid #d7e0ea; border-radius: 10px; padding: 22px; margin-bottom: 22px; }
    h2 { margin: 0 0 14px; color: #17324d; }
    h3 { color: #1f4e79; margin-top: 22px; }
    p, li { line-height: 1.65; }
    code { background: #eef3f8; padding: 2px 5px; border-radius: 4px; }
    table { width: 100%; border-collapse: collapse; margin: 14px 0; font-size: 14px; }
    th, td { border: 1px solid #d7e0ea; padding: 10px; text-align: left; vertical-align: top; }
    th { background: #eef3f8; color: #17324d; }
    .warn { border-left: 6px solid #d64545; background: #fff8f8; }
    .src a { color: #1d4ed8; }
  </style>
</head>
<body>
  <header>
    <h1>StepWise 数据分析指标指南</h1>
    <p>指标公式、传感器对应关系、结论规则与文献依据</p>
  </header>
  <main>
    <section class="warn">
      <h2>定位边界</h2>
      <p>StepWise 输出的是 <strong>gait screening indicators</strong>，不是 clinical diagnosis。它可以提示足底受力偏移、推蹬不足候选、步态节律异常候选、toe-in/toe-out 相关受力模式候选，但不能单独诊断足内翻、足外翻、扁平足、神经系统疾病或骨科疾病。</p>
    </section>

    <section>
      <h2>传感器位置与解剖含义</h2>
      <table>
        <tr><th>通道</th><th>你们当前放置位置</th><th>分析中对应变量</th><th>可支持的判断</th></tr>
        <tr><td>P1</td><td>脚后跟</td><td>Heel / Rearfoot pressure</td><td>触地开始、后跟受力、rearfoot-heavy</td></tr>
        <tr><td>P2</td><td>足弓</td><td>Arch / Midfoot pressure</td><td>足弓/中足接触负载 proxy</td></tr>
        <tr><td>P3</td><td>大拇指根部</td><td>Medial forefoot / 1st metatarsal pressure</td><td>内侧前掌偏载、toe-out 相关受力模式候选</td></tr>
        <tr><td>P4</td><td>小拇指根部</td><td>Lateral forefoot / 5th metatarsal pressure</td><td>外侧前掌偏载、toe-in 相关受力模式候选</td></tr>
        <tr><td>IMU</td><td>足部/鞋垫系统上的惯性传感器</td><td>Acceleration, gyroscope, Pitch/Roll/Yaw</td><td>足部姿态变化、运动强度、事件辅助</td></tr>
      </table>
      <p>文献中的低成本压力鞋垫常把 FSR 放在 heel、1st/3rd/5th metatarsal heads、great toe 等关键接触点；你们的 P1/P3/P4 与该布置高度对应，P2 提供中足/足弓额外信息。</p>
    </section>

    <section>
      <h2>核心公式与结论规则</h2>
      <table>
        <tr><th>指标</th><th>公式</th><th>怎么对应硬件</th><th>结论如何得出</th><th>依据</th></tr>
        <tr>
          <td>实际采样率</td>
          <td><code>fs = (N - 1) / (t_last - t_first)</code></td>
          <td>用 TXT 中 SystemTime 形成真实时间轴。若毫秒时间戳重复，用总时长估计。</td>
          <td>fs ≥ 50 Hz 且有效触地段充足时，时间类指标更可信；否则输出低置信度。</td>
          <td>IMU/可穿戴步态分析依赖准确事件时间；gaitmap 强调标准化传感器数据与步态参数计算。</td>
        </tr>
        <tr>
          <td>总压力</td>
          <td><code>P_total = P1 + P2 + P3 + P4</code></td>
          <td>四个 FSR 平滑后求和。</td>
          <td>用于识别 foot contact / stance phase。</td>
          <td>压力鞋垫研究常用单 FSR 或多 FSR 压力变化/总和判断步数和触地阶段。</td>
        </tr>
        <tr>
          <td>触地阈值</td>
          <td><code>theta_enter = max(5N, 0.08 * max(P_total))</code><br><code>theta_exit = 0.55 * theta_enter</code></td>
          <td>总压力超过进入阈值判定触地，低于退出阈值判定离地。</td>
          <td>形成 hysteresis，避免毛刺导致多次误触发。</td>
          <td>FSR 步数检测文献使用压力阈值；本项目阈值为工程筛查参数，应通过 baseline 调整。</td>
        </tr>
        <tr>
          <td>Stance time</td>
          <td><code>T_stance = t_end_contact - t_start_contact</code></td>
          <td>每个总压力触地段的持续时间。</td>
          <td>输出单脚触地时间；用于节律和步态周期分析。</td>
          <td>步态周期通常由 stance/swing 组成，stance 是足部接触地面的阶段。</td>
        </tr>
        <tr>
          <td>Stride time</td>
          <td><code>T_stride,j = t_start,j - t_start,j-1</code></td>
          <td>同一只脚连续两次触地开始之间。</td>
          <td>用于估计单脚步态周期和节律稳定性。</td>
          <td>时空步态参数包括 stance time、swing time、stride time、cadence。</td>
        </tr>
        <tr>
          <td>后跟压力占比</td>
          <td><code>RearRatio = P1 / P_total</code></td>
          <td>P1 是脚后跟。</td>
          <td>若多步 RearRatio ≥ 0.70，可标记 rearfoot-heavy loading pattern。</td>
          <td>heel FSR 可用于 stance 与触地分析；压力分布反映足底加载模式。</td>
        </tr>
        <tr>
          <td>前掌压力占比</td>
          <td><code>FrontRatio = (P3 + P4) / P_total</code></td>
          <td>P3/P4 是内外侧前掌。</td>
          <td>若多步 FrontRatio ≥ 0.70，可标记 forefoot-heavy loading pattern。</td>
          <td>1st/5th metatarsal heads 是压力鞋垫常用关键区域。</td>
        </tr>
        <tr>
          <td>足弓/中足占比</td>
          <td><code>ArchRatio = P2 / P_total</code></td>
          <td>P2 是足弓。</td>
          <td>若多步 ArchRatio ≥ 0.25，可标记 arch/midfoot loading candidate。</td>
          <td>这是本项目基于 P2 布置的工程 proxy，需要健康 baseline 校准；不能诊断扁平足。</td>
        </tr>
        <tr>
          <td>内侧前掌占比</td>
          <td><code>MedialRatio = P3 / P_total</code></td>
          <td>P3 是大拇指根部/第一跖骨区域。</td>
          <td>若多步 MedialRatio ≥ 0.65，可标记 medial loading bias；可作为 toe-out 相关受力模式候选。</td>
          <td>toe-out 会改变足底负载并更多影响内侧区域；但必须用 FPA/视频确认脚尖方向。</td>
        </tr>
        <tr>
          <td>外侧前掌占比</td>
          <td><code>LateralRatio = P4 / P_total</code></td>
          <td>P4 是小拇指根部/第五跖骨区域。</td>
          <td>若多步 LateralRatio ≥ 0.65，可标记 lateral loading bias；可作为 toe-in 相关受力模式候选。</td>
          <td>in-toeing 可将负载向外侧足部区域移动；压力只能提示相关模式，不能直接证明内八。</td>
        </tr>
        <tr>
          <td>推蹬 proxy</td>
          <td><code>PushOffRatio_late = mean((P3 + P4) / P_total)</code><br>取 stance 后 35%</td>
          <td>你们没有独立脚趾远端传感器，所以用前掌晚期受力作为推蹬 proxy。</td>
          <td>若多步 late stance 前掌占比过低，可标记 weak push-off candidate。</td>
          <td>toe-off/terminal stance 常与前足/趾部受力有关；本项目为简化 proxy。</td>
        </tr>
        <tr>
          <td>压力冲量</td>
          <td><code>Impulse = integral(P_total dt)</code></td>
          <td>每个 stance 段总压力对时间积分。</td>
          <td>用于比较每步承重强度，但需要 FSR 标定后才具有可靠物理意义。</td>
          <td>FSR 文献使用峰值、累计压力等压力特征提高步数/触地判断稳健性。</td>
        </tr>
        <tr>
          <td>IMU 模长与姿态范围</td>
          <td><code>|a| = sqrt(ax^2+ay^2+az^2)</code><br><code>|w| = sqrt(wx^2+wy^2+wz^2)</code><br><code>Range = max(angle)-min(angle)</code></td>
          <td>来自 MPU6050 的加速度、角速度、Pitch/Roll/Yaw。</td>
          <td>用于观察足部运动强度、姿态波动和事件辅助。</td>
          <td>gaitmap 和 IMU 步态分析文献支持用 foot-worn IMU 做事件检测与时空参数分析。</td>
        </tr>
      </table>
    </section>

    <section>
      <h2>为什么不能直接诊断足内翻/足外翻或内八/外八</h2>
      <p>内八/外八对应的核心角度通常是 Foot Progression Angle，即足部前后轴与行走方向之间的夹角。压力分布可以提示 toe-in/toe-out 相关受力模式，但不能单独测出脚尖方向。若要确认，需要俯视视频、动作捕捉或基于足部 IMU 的 FPA 算法。</p>
      <p>足内翻/足外翻涉及足踝关节和后足/前足姿态，单脚四点压力只能提示内外侧偏载，不足以做临床诊断。</p>
    </section>

    <section>
      <h2>六项风险提示如何生成</h2>
      <p>风险提示是工程筛查层面的 <strong>risk flags</strong>。文献支撑的是“这些指标有意义并常用于足底压力/可穿戴步态分析”；具体 Low/Medium/High 阈值需要你们用本鞋垫采集 normal baseline 后校准。当前脚本阈值用于毕业设计 demo 和初步筛查。</p>
      <table>
        <tr><th>模块</th><th>风险提示依据</th><th>当前工程规则</th><th>文献支撑点</th></tr>
        <tr>
          <td>1. 数据质量</td>
          <td>采样率、有效触地段数量、压力通道状态</td>
          <td>≥50 Hz 且 ≥8 个有效触地段为较可信；低于 20 Hz 或步数过少则 High risk</td>
          <td>可穿戴 IMU/FSR 步态事件检测依赖稳定采样和足够事件数量。</td>
        </tr>
        <tr>
          <td>2. 步态节律</td>
          <td>同脚 stride time 的 coefficient of variation</td>
          <td>CV ≥8% 为 Medium，≥12% 为 High</td>
          <td>stride time、stance time、cadence、variability 是常见 spatiotemporal gait parameters。</td>
        </tr>
        <tr>
          <td>3. 着地方式</td>
          <td>stance 前 20% 的后跟/前掌压力占比</td>
          <td>EarlyFrontRatio ≥0.65 多步出现提示前掌提前接触；EarlyRearRatio 低提示后跟初始接触不足</td>
          <td>FSR 可用于 heel strike、full contact、heel off、toe off 等接触阶段分类。</td>
        </tr>
        <tr>
          <td>4. 足底压力转移</td>
          <td>粗略 CoP_AP 从早期到晚期是否前移；late stance 前掌推蹬 proxy</td>
          <td>CoP_AP_progression 过小或 late forefoot ratio <0.30 多步出现则提示压力前移/推蹬不足倾向</td>
          <td>pedobarography 常用 CoP progression、pressure-time integral、contact region 描述足底滚动与压力转移。</td>
        </tr>
        <tr>
          <td>5. 内外侧/前后足受力分布</td>
          <td>Rear/Front/Arch/Medial/Lateral pressure ratios</td>
          <td>多步超过工程阈值则提示对应区域偏载</td>
          <td>足底压力研究常按 heel、midfoot、metatarsal、hallux/toe 区域分析峰值压力、冲量和接触时间。</td>
        </tr>
        <tr>
          <td>6. 建议与复测提示</td>
          <td>综合前 5 项风险和数据质量</td>
          <td>若多项 Medium/High，建议复测、视频确认或专业评估；若均 Low，建议继续建立 baseline</td>
          <td>FPA 文献说明 toe-in/toe-out 需要角度确认；压力只能提示相关受力模式。</td>
        </tr>
      </table>
    </section>

    <section class="src">
      <h2>主要参考文献/开源来源</h2>
      <ol>
        <li>Küderle et al., <a href="https://pmc.ncbi.nlm.nih.gov/articles/PMC10939318/">Gaitmap—An Open Ecosystem for IMU-Based Human Gait Analysis and Algorithm Benchmarking</a>, IEEE OJEMB, 2024.</li>
        <li>gaitmap documentation, <a href="https://gaitmap.readthedocs.io/en/stable/modules/event_detection.html">event detection module</a>.</li>
        <li><a href="https://www.mdpi.com/1424-8220/19/5/984">Design and Accuracy of an Instrumented Insole Using Pressure Sensors for Step Count</a>, Sensors, 2019.</li>
        <li>Jeon et al., <a href="https://pmc.ncbi.nlm.nih.gov/articles/PMC7506746/">Fast Wearable Sensor-Based Foot-Ground Contact Phase Classification</a>, Sensors, 2020.</li>
        <li><a href="https://www.mdpi.com/1424-8220/21/8/2727">Wearable Sensor-Based Real-Time Gait Detection: A Systematic Review</a>, Sensors, 2021.</li>
        <li>Wouda et al., <a href="https://pmc.ncbi.nlm.nih.gov/articles/PMC7888122/">Foot progression angle estimation using a single foot-worn inertial sensor</a>, JNER, 2021.</li>
        <li><a href="https://www.sciencedirect.com/science/article/pii/S0966636213001902">Foot loading patterns can be changed by deliberately walking with in-toeing or out-toeing gait modifications</a>, Gait & Posture, 2013.</li>
        <li><a href="https://www.mdpi.com/2076-3417/10/1/234">Use of Wearable Sensor Technology in Gait, Balance, and Range of Motion Analysis</a>, Applied Sciences, 2020.</li>
      </ol>
    </section>
  </main>
</body>
</html>
"""
    (output_dir / "data_guide.html").write_text(guide_html, encoding="utf-8")


def run_analysis(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    layout = SensorLayout(
        heel=tuple(args.heel),
        arch=tuple(args.arch),
        medial_forefoot=tuple(args.medial_forefoot),
        lateral_forefoot=tuple(args.lateral_forefoot),
        toe=tuple(args.toe or []),
    )

    df = parse_stepwise_txt(input_path)
    df = add_basic_features(df, layout, args.body_weight_n, args.smooth_window)

    enter_threshold, exit_threshold = adaptive_threshold(
        df["TotalPressure"],
        min_threshold_n=args.min_threshold_n,
        ratio=args.threshold_ratio,
    )
    df["FootContact"] = hysteresis_contact(df["TotalPressure"], enter_threshold, exit_threshold)
    intervals = contact_intervals(df["FootContact"].to_numpy(), df["Time_s"], args.min_stance_s)
    steps = extract_step_features(df, intervals)
    summary = summarize_session(df, steps, enter_threshold, exit_threshold)
    findings = build_screening_findings(steps, summary, demo_mode=args.demo_mode)
    risk_flags = build_risk_flags(steps, summary)

    df.to_csv(output_dir / "processed_gait_data.csv", index=False, encoding="utf-8-sig")
    steps.to_csv(output_dir / "gait_steps_analysis.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(findings).to_csv(output_dir / "screening_findings.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(risk_flags).to_csv(output_dir / "risk_flags.csv", index=False, encoding="utf-8-sig")
    (output_dir / "session_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "screening_findings.json").write_text(
        json.dumps(findings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "risk_flags.json").write_text(
        json.dumps(risk_flags, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    plot_results(df, steps, output_dir)
    write_text_report(summary, steps, findings, output_dir)
    write_html_report(summary, steps, findings, output_dir)
    write_user_report(summary, steps, findings, risk_flags, output_dir)
    write_data_guide(output_dir)

    print("StepWise gait analysis complete.")
    print(f"Input: {input_path}")
    print(f"Output directory: {output_dir}")
    print(f"HTML report: {output_dir / 'report.html'}")
    print(f"User report: {output_dir / 'user_report.html'}")
    print(f"Data guide: {output_dir / 'data_guide.html'}")
    print(f"Text report: {output_dir / 'report.txt'}")
    print(f"Warnings: {len(summary.get('warnings') or [])}")
    print(f"Screening findings: {len(findings)}")
    print(f"Risk flags: {len(risk_flags)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="StepWise smart-insole gait analysis")
    parser.add_argument("input", help="Path to StepWise TXT data file")
    parser.add_argument("--output-dir", default="stepwise_output", help="Directory for CSV, JSON, and PNG outputs")
    parser.add_argument("--body-weight-n", type=float, default=None, help="Optional body weight in Newtons")
    parser.add_argument("--smooth-window", type=int, default=3, help="Rolling smoothing window in samples")
    parser.add_argument("--min-threshold-n", type=float, default=5.0, help="Minimum total-pressure contact threshold")
    parser.add_argument("--threshold-ratio", type=float, default=0.08, help="Contact threshold as fraction of session peak")
    parser.add_argument("--min-stance-s", type=float, default=0.08, help="Minimum stance duration to keep")
    parser.add_argument("--heel", nargs="+", default=["P2"], help="Pressure channels located at heel/calcaneus")
    parser.add_argument("--arch", nargs="+", default=["P3"], help="Pressure channels located at arch/midfoot")
    parser.add_argument("--medial-forefoot", nargs="+", default=["P4"], help="Pressure channels at medial forefoot / first metatarsal")
    parser.add_argument("--lateral-forefoot", nargs="+", default=["P1"], help="Pressure channels at lateral forefoot / fifth metatarsal")
    parser.add_argument("--toe", nargs="*", default=[], help="Optional pressure channels at hallux/toe. Leave empty if no toe sensor exists.")
    parser.add_argument(
        "--demo-mode",
        action="store_true",
        help="Show all screening findings even when data quality is low. Use for software demonstration only.",
    )
    return parser


if __name__ == "__main__":
    run_analysis(build_parser().parse_args())
