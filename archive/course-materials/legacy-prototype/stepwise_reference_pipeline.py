from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from stepwise_gait_analysis import (
    SensorLayout,
    adaptive_threshold,
    add_basic_features,
    contact_intervals,
    extract_step_features,
    hysteresis_contact,
    parse_stepwise_txt,
    plot_results,
    summarize_session,
)


PRIMARY_REFERENCE = {
    "name": "Foot kinematics and kinetics data for different static foot posture",
    "why": (
        "Primary reference for inversion/eversion-related interpretation because it groups "
        "subjects by Foot Posture Index and includes foot kinematics plus plantar contact pressure."
    ),
    "url": "https://www.nature.com/articles/s41597-024-04166-3",
    "use_in_stepwise": (
        "Use as qualitative direction: highly pronated feet show more medial arch and "
        "1st/2nd metatarsal loading; highly supinated feet show more external arch and "
        "external metatarsal loading. StepWise maps this to the configured arch, medial forefoot, and lateral forefoot channels."
    ),
}

SUPPORTING_REFERENCES = [
    {
        "name": "Design and Accuracy of an Instrumented Insole Using Pressure Sensors for Step Count",
        "url": "https://pubmed.ncbi.nlm.nih.gov/30813515/",
        "use_in_stepwise": "Pressure-sensor insole reference supporting use of summed plantar pressure for stance/contact event detection.",
    },
    {
        "name": "CAD WALK Healthy Controls Dataset",
        "url": "https://zenodo.org/records/1265420",
        "use_in_stepwise": "Healthy dynamic plantar-pressure reference set.",
    },
    {
        "name": "Normative values for the Foot Posture Index",
        "url": "https://link.springer.com/article/10.1186/1757-1146-1-6",
        "use_in_stepwise": "Clinical foot-posture classification framework: pronated, normal, supinated.",
    },
    {
        "name": "Plantar pressure norm-reference in healthy adults",
        "url": "https://www.researchgate.net/publication/363823592_Plantar_pressure_during_gait_norm-referenced_measurement_for_Brazilian_healthy_adults_using_the_Footwork_ProR_System",
        "use_in_stepwise": "Healthy forefoot, midfoot, and hindfoot plantar-pressure norms.",
    },
    {
        "name": "GEDS wearable gait dataset",
        "url": "https://bmclab.pesquisa.ufabc.edu.br/datasets/geds/",
        "use_in_stepwise": "Open IMU and foot-ground-contact dataset for gait event and timing methods.",
    },
    {
        "name": "Drift-Free Foot Orientation Estimation in Running Using Wearable IMU",
        "url": "https://www.frontiersin.org/journals/bioengineering-and-biotechnology/articles/10.3389/fbioe.2020.00065/full",
        "use_in_stepwise": "Foot-mounted IMU reference for calibrated foot pitch/roll angles and foot-strike angle direction.",
    },
    {
        "name": "Footstrike angle cut-off values to classify footstrike pattern in runners",
        "url": "https://research.polyu.edu.hk/en/publications/footstrike-angle-cut-off-values-to-classify-footstrike-pattern-in/",
        "use_in_stepwise": "Reference showing that foot-strike classification can be based on foot-ground angle, while exact cutoffs depend on protocol and population.",
    },
    {
        "name": "OpenSense IMU calibration workflow",
        "url": "https://opensimconfluence.atlassian.net/wiki/spaces/OpenSim/pages/53084203/OpenSense%20-%20Kinematics%20with%20IMU%20Data",
        "use_in_stepwise": "Open-source biomechanical workflow showing that IMU data should be registered with a known calibration pose.",
    },
    {
        "name": "gaitmap coordinate systems",
        "url": "https://gaitmap.readthedocs.io/en/latest/source/user_guide/coordinate_systems.html",
        "use_in_stepwise": "Open-source gait-analysis documentation for foot-mounted sensor frames and the need for alignment.",
    },
    {
        "name": "Inversion of foot anatomy reference",
        "url": "https://www.kenhub.com/en/library/anatomy/inversion-of-foot",
        "use_in_stepwise": "Medical anatomy reference for defining inversion as the plantar surface tilting medially toward the body midline.",
    },
]

FRONTAL_TILT_THRESHOLD_DEG = 5.0
STRONG_FRONTAL_TILT_DEG = 8.0
ML_RATIO_MARGIN_THRESHOLD = 0.06
COP_ML_THRESHOLD = 0.04
ARCH_RATIO_SUPPORT_THRESHOLD = 0.10
LANDING_ANGLE_THRESHOLD_DEG = 12.0
EARLY_FOREFOOT_RATIO_THRESHOLD = 0.65
EARLY_REARFOOT_RATIO_THRESHOLD = 0.65


METRIC_KEYS = [
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
    "StrideCV",
    "LandingSoleGroundAngle_deg",
    "LandingSoleGroundAngle_abs_deg",
    "LandingRoll_mean_deg",
    "InitialContactRoll_deg",
    "LandingPitch_mean_deg",
    "PitchDelta_stance_from_standing",
    "Roll_stance_mean",
    "Roll_stance_std",
    "RollRange_deg",
    "Pitch_stance_mean",
    "Yaw_stance_mean",
]


def fmt(value: object, digits: int = 3) -> str:
    if value is None:
        return "-"
    try:
        if pd.isna(value):
            return "-"
    except TypeError:
        pass
    if isinstance(value, (float, np.floating)):
        return f"{float(value):.{digits}f}"
    return str(value)


def ratio_delta(metrics: dict[str, float], baseline: dict[str, float], key: str) -> float:
    value = metrics.get(key, np.nan)
    base = baseline.get(key, np.nan)
    if pd.isna(value) or pd.isna(base):
        return np.nan
    return float(value - base)


def analyze_stepwise_file(
    input_path: Path,
    output_dir: Path,
    *,
    layout: SensorLayout,
    smooth_window: int,
    min_threshold_n: float,
    threshold_ratio: float,
    min_stance_s: float,
    body_weight_n: float | None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    output_dir.mkdir(parents=True, exist_ok=True)

    raw = parse_stepwise_txt(input_path)
    processed = add_basic_features(raw, layout, body_weight_n, smooth_window)
    enter, exit_ = adaptive_threshold(
        processed["TotalPressure"],
        min_threshold_n=min_threshold_n,
        ratio=threshold_ratio,
    )
    processed["FootContact"] = hysteresis_contact(processed["TotalPressure"], enter, exit_)
    intervals = contact_intervals(processed["FootContact"].to_numpy(), processed["Time_s"], min_stance_s)
    steps = extract_step_features(processed, intervals)
    summary = summarize_session(processed, steps, enter, exit_)

    processed.to_csv(output_dir / "processed_gait_data.csv", index=False, encoding="utf-8-sig")
    steps.to_csv(output_dir / "gait_steps_analysis.csv", index=False, encoding="utf-8-sig")
    (output_dir / "session_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    plot_results(processed, steps, output_dir)
    return processed, steps, summary


def load_analyzed_folder(folder: Path) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    steps_path = folder / "gait_steps_analysis.csv"
    processed_path = folder / "processed_gait_data.csv"
    summary_path = folder / "session_summary.json"
    if not steps_path.exists() or not processed_path.exists() or not summary_path.exists():
        raise FileNotFoundError(
            f"Baseline folder must contain gait_steps_analysis.csv, processed_gait_data.csv, "
            f"and session_summary.json: {folder}"
        )
    return (
        pd.read_csv(processed_path),
        pd.read_csv(steps_path),
        json.loads(summary_path.read_text(encoding="utf-8")),
    )


def metric_means(steps: pd.DataFrame, processed: pd.DataFrame) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for col in [
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
    ]:
        metrics[col] = float(steps[col].mean(skipna=True)) if col in steps and not steps.empty else np.nan

    if "StrideTime_s" in steps and not steps.empty:
        stride = steps["StrideTime_s"].dropna()
        metrics["StrideCV"] = float(stride.std(ddof=0) / stride.mean()) if len(stride) >= 3 and stride.mean() > 0 else np.nan
    else:
        metrics["StrideCV"] = np.nan

    contact = processed
    if "FootContact" in processed:
        contact_mask = processed["FootContact"].astype(str).str.lower().isin(["true", "1"])
        if contact_mask.any():
            contact = processed[contact_mask]

    for col in ["Roll", "Pitch", "Yaw"]:
        if col in contact and not contact.empty:
            metrics[f"{col}_stance_mean"] = float(contact[col].mean(skipna=True))
            metrics[f"{col}_stance_std"] = float(contact[col].std(skipna=True))
            metrics[f"{col}_stance_min"] = float(contact[col].min(skipna=True))
            metrics[f"{col}_stance_max"] = float(contact[col].max(skipna=True))
        else:
            metrics[f"{col}_stance_mean"] = np.nan
            metrics[f"{col}_stance_std"] = np.nan
            metrics[f"{col}_stance_min"] = np.nan
            metrics[f"{col}_stance_max"] = np.nan

    return metrics


def add_standing_derived_metrics(
    metrics: dict[str, float],
    standing_calibration: dict[str, float | str | int] | None,
) -> dict[str, float]:
    metrics.setdefault("LandingSoleGroundAngle_deg", np.nan)
    metrics.setdefault("LandingSoleGroundAngle_abs_deg", np.nan)
    metrics.setdefault("PitchDelta_stance_from_standing", np.nan)
    metrics.setdefault("LandingPitchDelta_from_standing", np.nan)
    metrics.setdefault("RollDelta_stance_from_standing", np.nan)

    if not standing_calibration:
        return metrics

    roll_neutral = standing_calibration.get("Roll_neutral_mean", np.nan)
    pitch_neutral = standing_calibration.get("Pitch_neutral_mean", np.nan)
    landing_roll = metrics.get("LandingRoll_mean_deg", np.nan)
    stance_roll = metrics.get("Roll_stance_mean", np.nan)
    stance_pitch = metrics.get("Pitch_stance_mean", np.nan)
    landing_pitch = metrics.get("LandingPitch_mean_deg", np.nan)

    if not pd.isna(roll_neutral) and not pd.isna(landing_roll):
        angle = float(landing_roll - float(roll_neutral))
        metrics["LandingSoleGroundAngle_deg"] = angle
        metrics["LandingSoleGroundAngle_abs_deg"] = abs(angle)
    if not pd.isna(roll_neutral) and not pd.isna(stance_roll):
        metrics["RollDelta_stance_from_standing"] = float(stance_roll - float(roll_neutral))
    if not pd.isna(pitch_neutral) and not pd.isna(stance_pitch):
        metrics["PitchDelta_stance_from_standing"] = float(stance_pitch - float(pitch_neutral))
    if not pd.isna(pitch_neutral) and not pd.isna(landing_pitch):
        metrics["LandingPitchDelta_from_standing"] = float(landing_pitch - float(pitch_neutral))

    return metrics


def analyze_standing_calibration(
    input_path: Path,
    output_dir: Path,
    *,
    layout: SensorLayout,
    smooth_window: int,
    body_weight_n: float | None,
) -> dict[str, float | str | int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw = parse_stepwise_txt(input_path)
    processed = add_basic_features(raw, layout, body_weight_n, smooth_window)
    peak = float(processed["TotalPressure"].max()) if len(processed) else 0.0
    loaded = processed[processed["TotalPressure"] >= max(5.0, peak * 0.08)]
    if loaded.empty:
        loaded = processed
    calibration = {
        "source_file": str(input_path),
        "samples": int(len(processed)),
        "duration_s": float(processed["Time_s"].iloc[-1] - processed["Time_s"].iloc[0]) if len(processed) > 1 else 0.0,
        "Roll_neutral_mean": float(loaded["Roll"].mean(skipna=True)) if "Roll" in loaded else np.nan,
        "Pitch_neutral_mean": float(loaded["Pitch"].mean(skipna=True)) if "Pitch" in loaded else np.nan,
        "Yaw_neutral_mean": float(loaded["Yaw"].mean(skipna=True)) if "Yaw" in loaded else np.nan,
        "StaticRearRatio_mean": float(loaded["RearRatio"].mean(skipna=True)) if "RearRatio" in loaded else np.nan,
        "StaticArchRatio_mean": float(loaded["ArchRatio"].mean(skipna=True)) if "ArchRatio" in loaded else np.nan,
        "StaticMedialRatio_mean": float(loaded["MedialRatio"].mean(skipna=True)) if "MedialRatio" in loaded else np.nan,
        "StaticLateralRatio_mean": float(loaded["LateralRatio"].mean(skipna=True)) if "LateralRatio" in loaded else np.nan,
        "StaticCoP_ML_mean": float(loaded["CoP_ML"].mean(skipna=True)) if "CoP_ML" in loaded else np.nan,
    }
    processed.to_csv(output_dir / "standing_processed_data.csv", index=False, encoding="utf-8-sig")
    (output_dir / "standing_calibration.json").write_text(
        json.dumps(calibration, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return calibration


def make_sensor_layout(args: argparse.Namespace) -> SensorLayout:
    return SensorLayout(
        heel=tuple(args.heel),
        arch=tuple(args.arch),
        medial_forefoot=tuple(args.medial_forefoot),
        lateral_forefoot=tuple(args.lateral_forefoot),
        toe=tuple(args.toe or []),
    )


def risk_level(score: int, has_imu: bool, has_pressure: bool) -> str:
    if score >= 3 or (has_imu and has_pressure):
        return "High"
    if score >= 1:
        return "Medium"
    return "Low"


def make_evidence_line(metrics: dict[str, float], baseline: dict[str, float], key: str, label: str) -> str:
    return (
        f"{label}: current={fmt(metrics.get(key))}, "
        f"baseline={fmt(baseline.get(key))}, delta={fmt(ratio_delta(metrics, baseline, key))}"
    )


def public_metric_line(metrics: dict[str, float], key: str, label: str) -> str:
    return f"{label}: {fmt(metrics.get(key))}"


def build_public_reference_cards(
    metrics: dict[str, float],
    summary: dict,
    *,
    pitch_eversion_sign: str,
    standing_calibration: dict[str, float | str | int] | None,
) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    quality = str(summary.get("data_quality", "Low"))
    stance_count = int(summary.get("detected_steps_single_foot") or 0)

    low_confidence = quality == "Low" or stance_count < 4

    def add(title: str, level: str, evidence: list[str], interpretation: str, action: str, limitation: str) -> None:
        if low_confidence and title != "Walking posture cannot be inferred from this file":
            limitation = limitation + " This trial has low confidence because it contains too few valid stance phases."
        cards.append(
            {
                "title": title,
                "level": level,
                "evidence": " | ".join(evidence),
                "interpretation": interpretation,
                "action": action,
                "limitation": limitation,
            }
        )

    if stance_count < 3:
        add(
            "Walking posture cannot be inferred from this file",
            "High",
            [
                f"data quality={quality}",
                f"valid stance phases={stance_count}",
                f"sample rate={fmt(summary.get('estimated_sample_rate_hz'))} Hz",
            ],
            "The file does not contain enough valid walking cycles for posture-risk interpretation.",
            "Re-test with at least 10-20 valid stance phases.",
            "Posture risk should not be inferred from non-walking or very short recordings.",
        )
        return cards

    medial = metrics.get("MedialRatio_mean", np.nan)
    lateral = metrics.get("LateralRatio_mean", np.nan)
    arch = metrics.get("ArchRatio_mean", np.nan)
    cop_ml = metrics.get("CoP_ML_mean", np.nan)
    medial_margin = medial - lateral if not pd.isna(medial) and not pd.isna(lateral) else np.nan
    lateral_margin = -medial_margin if not pd.isna(medial_margin) else np.nan

    pitch_delta = metrics.get("PitchDelta_stance_from_standing", np.nan)
    landing_angle = metrics.get("LandingSoleGroundAngle_deg", np.nan)
    has_pitch_calibration = bool(standing_calibration) and not pd.isna(pitch_delta)
    eversion_imu = False
    inversion_imu = False
    if has_pitch_calibration and abs(pitch_delta) >= FRONTAL_TILT_THRESHOLD_DEG:
        positive_eversion = pitch_eversion_sign == "positive"
        eversion_imu = pitch_delta >= FRONTAL_TILT_THRESHOLD_DEG if positive_eversion else pitch_delta <= -FRONTAL_TILT_THRESHOLD_DEG
        inversion_imu = pitch_delta <= -FRONTAL_TILT_THRESHOLD_DEG if positive_eversion else pitch_delta >= FRONTAL_TILT_THRESHOLD_DEG

    eversion_pressure_hits = []
    if not pd.isna(medial_margin) and medial_margin >= ML_RATIO_MARGIN_THRESHOLD:
        eversion_pressure_hits.append("medial forefoot loading is higher than lateral forefoot loading")
    if not pd.isna(cop_ml) and cop_ml >= COP_ML_THRESHOLD:
        eversion_pressure_hits.append("CoP_ML proxy shifts toward the medial side")
    if not pd.isna(arch) and arch >= ARCH_RATIO_SUPPORT_THRESHOLD:
        eversion_pressure_hits.append("P2 arch/midfoot loading is elevated")

    inversion_pressure_hits = []
    if not pd.isna(lateral_margin) and lateral_margin >= ML_RATIO_MARGIN_THRESHOLD:
        inversion_pressure_hits.append("lateral forefoot loading is higher than medial forefoot loading")
    if not pd.isna(cop_ml) and cop_ml <= -COP_ML_THRESHOLD:
        inversion_pressure_hits.append("CoP_ML proxy shifts toward the lateral side")

    eversion_pressure = bool(eversion_pressure_hits)
    inversion_pressure = bool(inversion_pressure_hits)

    if (eversion_imu and inversion_pressure) or (inversion_imu and eversion_pressure) or (eversion_pressure and inversion_pressure):
        add(
            "Mixed inversion/eversion evidence",
            "Medium",
            [
                public_metric_line(metrics, "MedialRatio_mean", "medial forefoot ratio"),
                public_metric_line(metrics, "LateralRatio_mean", "lateral forefoot ratio"),
                f"medial-lateral margin={fmt(medial_margin)}",
                public_metric_line(metrics, "CoP_ML_mean", "CoP_ML proxy"),
                "Pitch delta from standing=" + fmt(pitch_delta, 2),
            ],
            "Pressure and calibrated Pitch do not point cleanly to one frontal-plane tendency.",
            "Repeat static standing calibration plus one obvious inversion and one obvious eversion trial to confirm the IMU sign.",
            "When frontal-plane IMU and pressure evidence conflict, StepWise should not force an inversion or eversion label.",
        )
    elif eversion_imu:
        eversion_level = "High" if eversion_pressure or abs(pitch_delta) >= STRONG_FRONTAL_TILT_DEG else "Medium"
        add(
            "Possible eversion / over-pronation related tendency",
            eversion_level,
            [
                public_metric_line(metrics, "MedialRatio_mean", "medial forefoot ratio"),
                public_metric_line(metrics, "LateralRatio_mean", "lateral forefoot ratio"),
                f"medial-lateral margin={fmt(medial_margin)}",
                public_metric_line(metrics, "ArchRatio_mean", "arch ratio"),
                public_metric_line(metrics, "CoP_ML_mean", "CoP_ML proxy"),
                "Pitch delta from standing=" + fmt(pitch_delta, 2) if standing_calibration else "Pitch direction not used: no standing calibration file",
                "positive evidence=" + ", ".join(eversion_pressure_hits + (["Pitch shifted toward eversion"] if eversion_imu else [])),
            ],
            (
                "The left-foot IMU tilt is toward the medial side relative to standing neutral. "
                "Pressure evidence is used only as supporting evidence."
            ),
            "Repeat with a standing calibration file and a frontal/top-view video. Confirm the arch sensor is working, because it is important for arch evidence.",
            "This is a screening warning, not a diagnosis of flat foot, pronation, or clinical foot eversion.",
        )
    elif inversion_imu:
        inversion_level = "High" if inversion_pressure or abs(pitch_delta) >= STRONG_FRONTAL_TILT_DEG else "Medium"
        add(
            "Possible inversion / over-supination related tendency",
            inversion_level,
            [
                public_metric_line(metrics, "MedialRatio_mean", "medial forefoot ratio"),
                public_metric_line(metrics, "LateralRatio_mean", "lateral forefoot ratio"),
                f"lateral-medial margin={fmt(lateral_margin)}",
                public_metric_line(metrics, "CoP_ML_mean", "CoP_ML proxy"),
                "Pitch delta from standing=" + fmt(pitch_delta, 2) if standing_calibration else "Pitch direction not used: no standing calibration file",
                "positive evidence=" + ", ".join(inversion_pressure_hits + (["Pitch shifted toward inversion"] if inversion_imu else [])),
            ],
            (
                "The left-foot IMU tilt is toward the lateral side relative to standing neutral. "
                "Pressure evidence is used only as supporting evidence."
            ),
            "Repeat with a standing calibration file and inspect lateral shoe/insole loading.",
            "This is a screening warning, not a diagnosis of high arch, ankle instability, or clinical foot inversion.",
        )
    elif eversion_pressure:
        add(
            "Medial loading bias; eversion not confirmed",
            "Low",
            [
                public_metric_line(metrics, "MedialRatio_mean", "medial forefoot ratio"),
                public_metric_line(metrics, "LateralRatio_mean", "lateral forefoot ratio"),
                f"medial-lateral margin={fmt(medial_margin)}",
                public_metric_line(metrics, "CoP_ML_mean", "CoP_ML proxy"),
                "Pitch delta from standing=" + fmt(pitch_delta, 2) if has_pitch_calibration else "Pitch calibration unavailable",
            ],
            "The pressure pattern is more medial, but calibrated Pitch does not support a clear eversion tendency.",
            "Use this as a pressure-loading note and confirm with video or repeated trials.",
            "Pressure distribution alone cannot medically confirm eversion/pronation.",
        )
    elif inversion_pressure:
        add(
            "Lateral loading bias; inversion not confirmed",
            "Low",
            [
                public_metric_line(metrics, "MedialRatio_mean", "medial forefoot ratio"),
                public_metric_line(metrics, "LateralRatio_mean", "lateral forefoot ratio"),
                f"lateral-medial margin={fmt(lateral_margin)}",
                public_metric_line(metrics, "CoP_ML_mean", "CoP_ML proxy"),
                "Pitch delta from standing=" + fmt(pitch_delta, 2) if has_pitch_calibration else "Pitch calibration unavailable",
            ],
            "The pressure pattern is more lateral, but calibrated Pitch does not support a clear inversion tendency.",
            "Use this as a pressure-loading note and confirm with video or repeated trials.",
            "Pressure distribution alone cannot medically confirm inversion/supination.",
        )

    forefoot_landing_evidence = []
    rearfoot_landing_evidence = []
    pressure_supports_forefoot = metrics.get("EarlyFrontRatio_mean", np.nan) >= EARLY_FOREFOOT_RATIO_THRESHOLD
    pressure_supports_rearfoot = metrics.get("EarlyRearRatio_mean", np.nan) >= EARLY_REARFOOT_RATIO_THRESHOLD
    angle_supports_forefoot = not pd.isna(landing_angle) and landing_angle <= -LANDING_ANGLE_THRESHOLD_DEG
    angle_supports_rearfoot = not pd.isna(landing_angle) and landing_angle >= LANDING_ANGLE_THRESHOLD_DEG

    if pressure_supports_forefoot:
        forefoot_landing_evidence.append(public_metric_line(metrics, "EarlyFrontRatio_mean", "early forefoot ratio"))
    if angle_supports_forefoot:
        forefoot_landing_evidence.append(
            "landing sole-ground angle="
            + fmt(landing_angle, 2)
            + " deg (negative means forefoot lower than heel)"
        )

    if pressure_supports_rearfoot:
        rearfoot_landing_evidence.append(public_metric_line(metrics, "EarlyRearRatio_mean", "early rearfoot ratio"))
    if angle_supports_rearfoot:
        rearfoot_landing_evidence.append(
            "landing sole-ground angle="
            + fmt(landing_angle, 2)
            + " deg (positive means forefoot higher than heel)"
        )

    if forefoot_landing_evidence and rearfoot_landing_evidence:
        pass
    elif forefoot_landing_evidence:
        add(
            "Forefoot-first landing tendency",
            "Medium",
            forefoot_landing_evidence,
            "The forefoot appears to load early or the foot lands with the forefoot lower than the heel.",
            "Confirm with side-view video.",
            "This is a landing-pattern screening flag, not a diagnosis.",
        )
    elif rearfoot_landing_evidence:
        add(
            "Rearfoot-heavy landing tendency",
            "Medium",
            rearfoot_landing_evidence,
            "The heel/rearfoot appears to load early or the foot lands with the forefoot higher than the heel.",
            "Confirm with side-view video and repeat with more valid stance phases.",
            "This is a landing-pattern screening flag, not a diagnosis of heel pathology.",
        )

    if metrics.get("PushOffRatio_late_stance_mean", np.nan) < 0.75:
        add(
            "Reduced late-stance forefoot push-off warning",
            "Medium",
            [public_metric_line(metrics, "PushOffRatio_late_stance_mean", "late push-off proxy")],
            "Forefoot pressure near the end of stance is lower than expected.",
            "Repeat and confirm medial/lateral forefoot sensor placement.",
            "This does not diagnose calf weakness or neurological impairment.",
        )

    if metrics.get("CoP_AP_progression", np.nan) < 0.15:
        add(
            "Reduced heel-to-forefoot pressure transfer",
            "Medium",
            [public_metric_line(metrics, "CoP_AP_progression", "anterior CoP progression")],
            "The pressure center did not move strongly from heel/arch toward forefoot.",
            "Inspect the pressure curves and repeat at natural walking speed.",
            "This is a four-sensor proxy, not a pressure-plate CoP measurement.",
        )

    if stance_count >= 8 and not pd.isna(metrics.get("StrideCV", np.nan)) and metrics["StrideCV"] >= 0.12:
        add(
            "High stride timing variability",
            "Medium",
            [f"stride-time CV={fmt(metrics.get('StrideCV'))}"],
            "Same-foot stride timing varies noticeably across the trial.",
            "Repeat on a straight path with stable speed.",
            "Single-foot timing with a small number of steps is only a screening flag.",
        )

    if not cards:
        add(
            "No clear posture risk detected",
            "Low",
            [
                public_metric_line(metrics, "MedialRatio_mean", "medial forefoot ratio"),
                public_metric_line(metrics, "LateralRatio_mean", "lateral forefoot ratio"),
                public_metric_line(metrics, "CoP_ML_mean", "CoP_ML proxy"),
                "Pitch direction not used without standing calibration" if not standing_calibration else "Pitch delta from standing=" + fmt(pitch_delta, 2),
                "Landing sole-ground angle=" + fmt(landing_angle, 2) if standing_calibration else "Landing angle requires standing calibration",
            ],
            "The current trial does not show a strong StepWise-supported posture-risk pattern.",
            "Repeat if visible posture is abnormal or symptoms exist.",
            "Normal-like screening does not rule out clinical gait or foot-posture issues.",
        )

    priority = {
        "Possible eversion / over-pronation related tendency": 0,
        "Possible inversion / over-supination related tendency": 0,
        "Mixed inversion/eversion evidence": 0,
        "Medial loading bias; eversion not confirmed": 2,
        "Lateral loading bias; inversion not confirmed": 2,
        "Mixed landing evidence: confirm with video": 1,
        "Forefoot-first landing tendency": 1,
        "Rearfoot-heavy landing tendency": 1,
        "Reduced late-stance forefoot push-off warning": 2,
        "Reduced heel-to-forefoot pressure transfer": 2,
        "High stride timing variability": 3,
        "No clear posture risk detected": 9,
        "Walking posture cannot be inferred from this file": -1,
    }
    return sorted(cards, key=lambda item: priority.get(item["title"], 5))


def build_posture_risk_cards(
    metrics: dict[str, float],
    baseline: dict[str, float],
    summary: dict,
    *,
    pitch_eversion_sign: str,
) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    quality = str(summary.get("data_quality", "Low"))
    stance_count = int(summary.get("detected_steps_single_foot") or 0)
    baseline_available = not pd.isna(baseline.get("MedialRatio_mean", np.nan))

    def add(title: str, level: str, evidence: list[str], interpretation: str, action: str, limitation: str) -> None:
        cards.append(
            {
                "title": title,
                "level": level,
                "evidence": " | ".join(evidence),
                "interpretation": interpretation,
                "action": action,
                "limitation": limitation,
            }
        )

    if quality == "Low" or stance_count < 4:
        add(
            "Walking posture cannot be inferred from this file",
            "High",
            [
                f"data quality={quality}",
                f"valid stance phases={stance_count}",
                f"sample rate={fmt(summary.get('estimated_sample_rate_hz'))} Hz",
            ],
            "The file does not contain enough valid walking cycles for posture-risk interpretation.",
            "Use this recording for connection/calibration checking only, then re-test with at least 10-20 valid stance phases.",
            "Posture risk should not be inferred from non-walking or very short recordings.",
        )
        return cards

    pitch_delta = ratio_delta(metrics, baseline, "Pitch_stance_mean")
    medial_delta = ratio_delta(metrics, baseline, "MedialRatio_mean")
    lateral_delta = ratio_delta(metrics, baseline, "LateralRatio_mean")
    arch_delta = ratio_delta(metrics, baseline, "ArchRatio_mean")
    cop_ml_delta = ratio_delta(metrics, baseline, "CoP_ML_mean")

    positive_eversion = pitch_eversion_sign == "positive"
    eversion_imu = (not pd.isna(pitch_delta)) and (
        (pitch_delta >= FRONTAL_TILT_THRESHOLD_DEG) if positive_eversion else (pitch_delta <= -FRONTAL_TILT_THRESHOLD_DEG)
    )
    inversion_imu = (not pd.isna(pitch_delta)) and (
        (pitch_delta <= -FRONTAL_TILT_THRESHOLD_DEG) if positive_eversion else (pitch_delta >= FRONTAL_TILT_THRESHOLD_DEG)
    )

    eversion_pressure = [
        ("medial forefoot loading increased", (not pd.isna(medial_delta)) and medial_delta >= ML_RATIO_MARGIN_THRESHOLD),
        ("arch/midfoot loading increased", (not pd.isna(arch_delta)) and arch_delta >= ARCH_RATIO_SUPPORT_THRESHOLD),
        ("CoP shifted medially", (not pd.isna(cop_ml_delta)) and cop_ml_delta >= COP_ML_THRESHOLD),
    ]
    inversion_pressure = [
        ("lateral forefoot loading increased", (not pd.isna(lateral_delta)) and lateral_delta >= ML_RATIO_MARGIN_THRESHOLD),
        ("CoP shifted laterally", (not pd.isna(cop_ml_delta)) and cop_ml_delta <= -COP_ML_THRESHOLD),
    ]
    eversion_pressure_hits = [name for name, hit in eversion_pressure if hit]
    inversion_pressure_hits = [name for name, hit in inversion_pressure if hit]

    evidence_conflict = (
        (eversion_imu and bool(inversion_pressure_hits))
        or (inversion_imu and bool(eversion_pressure_hits))
        or (bool(eversion_pressure_hits) and bool(inversion_pressure_hits))
    )

    if evidence_conflict:
        add(
            "Mixed frontal-plane evidence: repeat calibration",
            "Medium",
            [
                make_evidence_line(metrics, baseline, "Pitch_stance_mean", "stance Pitch"),
                make_evidence_line(metrics, baseline, "MedialRatio_mean", "medial forefoot ratio"),
                make_evidence_line(metrics, baseline, "LateralRatio_mean", "lateral forefoot ratio"),
                make_evidence_line(metrics, baseline, "CoP_ML_mean", "medial-lateral CoP proxy"),
                "eversion pressure evidence=" + (", ".join(eversion_pressure_hits) if eversion_pressure_hits else "none"),
                "inversion pressure evidence=" + (", ".join(inversion_pressure_hits) if inversion_pressure_hits else "none"),
            ],
            (
                "Pressure distribution and IMU Pitch do not point to the same frontal-plane foot tendency. "
                "A rigorous report should not call this a clean inversion or eversion result."
            ),
            "Repeat a standing neutral calibration plus one obvious inversion and one obvious eversion trial, then set --pitch-eversion-sign from those trials.",
            "Pitch direction depends on sensor mounting, and four pressure points are an indirect proxy for foot posture.",
        )

    if eversion_imu and not evidence_conflict:
        level = "High" if eversion_pressure_hits or abs(pitch_delta) >= STRONG_FRONTAL_TILT_DEG else "Medium"
        add(
            "Possible eversion / over-pronation related tendency",
            level,
            [
                make_evidence_line(metrics, baseline, "Pitch_stance_mean", "stance Pitch"),
                make_evidence_line(metrics, baseline, "MedialRatio_mean", "medial forefoot ratio"),
                make_evidence_line(metrics, baseline, "ArchRatio_mean", "arch ratio"),
                make_evidence_line(metrics, baseline, "CoP_ML_mean", "medial-lateral CoP proxy"),
                "positive pressure evidence=" + (", ".join(eversion_pressure_hits) if eversion_pressure_hits else "none"),
            ],
            (
                "The reference dataset links pronated foot posture with more medial arch and medial forefoot loading. "
                "This trial shows one or more StepWise signals in that direction."
            ),
            "Repeat the test with the same sensor placement, record a frontal/top-view video, and compare against more normal-baseline trials.",
            "This is a screening warning. It does not diagnose flat foot, pronation, or clinical foot eversion.",
        )

    if inversion_imu and not evidence_conflict:
        level = "High" if inversion_pressure_hits or abs(pitch_delta) >= STRONG_FRONTAL_TILT_DEG else "Medium"
        add(
            "Possible inversion / over-supination related tendency",
            level,
            [
                make_evidence_line(metrics, baseline, "Pitch_stance_mean", "stance Pitch"),
                make_evidence_line(metrics, baseline, "LateralRatio_mean", "lateral forefoot ratio"),
                make_evidence_line(metrics, baseline, "CoP_ML_mean", "medial-lateral CoP proxy"),
                "positive pressure evidence=" + (", ".join(inversion_pressure_hits) if inversion_pressure_hits else "none"),
            ],
            (
                "The reference dataset links supinated foot posture with greater external/lateral loading. "
                "This trial shows one or more StepWise signals in that direction."
            ),
            "Repeat the test with the same sensor placement, record a frontal/top-view video, and inspect lateral shoe/insole loading.",
            "This is a screening warning. It does not diagnose high arch, ankle instability, or clinical foot inversion.",
        )

    if eversion_pressure_hits and not eversion_imu and not evidence_conflict:
        add(
            "Medial loading bias; eversion not confirmed",
            "Low",
            [
                make_evidence_line(metrics, baseline, "Pitch_stance_mean", "stance Pitch"),
                make_evidence_line(metrics, baseline, "MedialRatio_mean", "medial forefoot ratio"),
                make_evidence_line(metrics, baseline, "CoP_ML_mean", "medial-lateral CoP proxy"),
                "positive pressure evidence=" + ", ".join(eversion_pressure_hits),
            ],
            "Pressure increased on the medial side relative to baseline, but calibrated Pitch did not support a clear eversion tendency.",
            "Use this as a pressure-loading note and confirm with video or repeated trials.",
            "Pressure distribution alone cannot medically confirm eversion/pronation.",
        )

    if inversion_pressure_hits and not inversion_imu and not evidence_conflict:
        add(
            "Lateral loading bias; inversion not confirmed",
            "Low",
            [
                make_evidence_line(metrics, baseline, "Pitch_stance_mean", "stance Pitch"),
                make_evidence_line(metrics, baseline, "LateralRatio_mean", "lateral forefoot ratio"),
                make_evidence_line(metrics, baseline, "CoP_ML_mean", "medial-lateral CoP proxy"),
                "positive pressure evidence=" + ", ".join(inversion_pressure_hits),
            ],
            "Pressure increased on the lateral side relative to baseline, but calibrated Pitch did not support a clear inversion tendency.",
            "Use this as a pressure-loading note and confirm with video or repeated trials.",
            "Pressure distribution alone cannot medically confirm inversion/supination.",
        )

    if metrics.get("EarlyFrontRatio_mean", np.nan) >= 0.65 or ratio_delta(metrics, baseline, "EarlyFrontRatio_mean") >= 0.20:
        add(
            "Forefoot-first landing tendency",
            "Medium",
            [make_evidence_line(metrics, baseline, "EarlyFrontRatio_mean", "early forefoot ratio")],
            "The forefoot appears to load early in stance.",
            "Confirm with side-view video and repeat with 10-20 valid stance phases.",
            "This is a landing-pattern screening flag, not a diagnosis.",
        )

    if metrics.get("EarlyRearRatio_mean", np.nan) >= 0.78 or ratio_delta(metrics, baseline, "EarlyRearRatio_mean") >= 0.14:
        add(
            "Rearfoot-heavy landing tendency",
            "Medium",
            [make_evidence_line(metrics, baseline, "EarlyRearRatio_mean", "early rearfoot ratio")],
            "The heel dominates early stance.",
            "Check heel sensor placement and compare with side-view video.",
            "Heel loading alone does not indicate heel pathology.",
        )

    push_delta = ratio_delta(metrics, baseline, "PushOffRatio_late_stance_mean")
    if (not pd.isna(push_delta) and push_delta <= -0.08) or metrics.get("PushOffRatio_late_stance_mean", np.nan) < 0.75:
        add(
            "Reduced late-stance forefoot push-off warning",
            "Medium",
            [make_evidence_line(metrics, baseline, "PushOffRatio_late_stance_mean", "late push-off proxy")],
            "Forefoot pressure near the end of stance is lower than expected.",
            "Repeat the test and confirm P3/P4 placement. If repeated, calf raise and toe-grip exercises can be suggested as rehabilitation support.",
            "This does not diagnose calf weakness or neurological impairment.",
        )

    if metrics.get("CoP_AP_progression", np.nan) < 0.15:
        add(
            "Reduced heel-to-forefoot pressure transfer",
            "Medium",
            [make_evidence_line(metrics, baseline, "CoP_AP_progression", "anterior CoP progression")],
            "The pressure center did not move strongly from heel/arch toward the forefoot.",
            "Inspect the pressure curves and repeat at natural walking speed.",
            "This is a four-sensor proxy, not a pressure-plate CoP measurement.",
        )

    if stance_count >= 10 and not pd.isna(metrics.get("StrideCV", np.nan)) and metrics["StrideCV"] >= 0.12:
        add(
            "High stride timing variability",
            "Medium",
            [f"stride-time CV={fmt(metrics.get('StrideCV'))}"],
            "Same-foot stride timing varies noticeably across the trial.",
            "Repeat on a straight path with stable speed.",
            "Single-foot timing with a small number of steps is only a screening flag.",
        )

    if baseline_available and not cards:
        add(
            "No clear posture risk detected",
            "Low",
            [
                make_evidence_line(metrics, baseline, "Pitch_stance_mean", "stance Pitch"),
                make_evidence_line(metrics, baseline, "MedialRatio_mean", "medial forefoot ratio"),
                make_evidence_line(metrics, baseline, "LateralRatio_mean", "lateral forefoot ratio"),
                make_evidence_line(metrics, baseline, "CoP_ML_mean", "medial-lateral CoP proxy"),
            ],
            "The current trial does not show a strong StepWise-supported posture-risk pattern relative to the normal baseline.",
            "Repeat if the visible walking posture is abnormal; the current recording may not have captured the intended motion or the sensor axis may be saturated.",
            "Normal-like screening does not rule out clinical gait or foot-posture issues.",
        )
    elif not baseline_available and not cards:
        add(
            "Baseline is required for inversion/eversion risk",
            "Medium",
            ["No analyzed normal baseline was provided."],
            "StepWise can still report raw loading, but inversion/eversion screening should be compared with a device-specific normal trial.",
            "Collect a normal walking file with the same shoe, same sensor placement, and at least 10-20 valid stance phases.",
            "External datasets establish the direction of evidence, not direct thresholds for this four-FSR insole.",
        )

    priority = {
        "Possible eversion / over-pronation related tendency": 0,
        "Possible inversion / over-supination related tendency": 0,
        "Medial loading bias; eversion not confirmed": 2,
        "Lateral loading bias; inversion not confirmed": 2,
        "Forefoot-first landing tendency": 1,
        "Rearfoot-heavy landing tendency": 1,
        "Reduced late-stance forefoot push-off warning": 2,
        "Reduced heel-to-forefoot pressure transfer": 2,
        "High stride timing variability": 3,
        "No clear posture risk detected": 9,
        "Baseline is required for inversion/eversion risk": 8,
        "Mixed frontal-plane evidence: repeat calibration": 0,
        "Walking posture cannot be inferred from this file": -1,
    }
    return sorted(cards, key=lambda item: priority.get(item["title"], 5))


def report_css() -> str:
    return """
    :root { color-scheme: light; }
    body { margin: 0; font-family: Arial, Helvetica, sans-serif; color: #17212b; background: #f4f7fb; }
    header { background: #0e2f44; color: white; padding: 34px 44px; }
    header h1 { margin: 0 0 10px; font-size: 30px; letter-spacing: 0; }
    header p { margin: 0; color: #dbeaf2; line-height: 1.55; }
    main { max-width: 1120px; margin: 0 auto; padding: 26px; }
    section { background: white; border: 1px solid #d8e1ea; border-radius: 8px; padding: 22px; margin-bottom: 20px; }
    h2 { margin: 0 0 16px; color: #0e2f44; font-size: 21px; letter-spacing: 0; }
    h3 { margin: 0 0 8px; color: #0f344d; font-size: 18px; letter-spacing: 0; }
    p { line-height: 1.62; }
    a { color: #1261a6; }
    .hero { border-left: 8px solid #2b7bbb; }
    .hero h2 { font-size: 27px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
    .metric { border: 1px solid #d8e1ea; background: #fbfdff; border-radius: 8px; padding: 14px; }
    .metric span { display: block; color: #657487; font-size: 13px; margin-bottom: 8px; }
    .metric strong { display: block; color: #0e2f44; font-size: 21px; }
    .card { border: 1px solid #d8e1ea; background: #fbfdff; border-radius: 8px; padding: 16px; margin-bottom: 12px; }
    .card-head { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
    .badge { display: inline-block; border-radius: 999px; padding: 5px 11px; font-weight: 700; white-space: nowrap; }
    .low { background: #dcfce7; color: #166534; }
    .medium { background: #fef3c7; color: #92400e; }
    .high { background: #fee2e2; color: #991b1b; }
    .note { color: #5e6c7a; }
    table { border-collapse: collapse; width: 100%; font-size: 14px; }
    th, td { border: 1px solid #d8e1ea; padding: 9px; vertical-align: top; text-align: left; }
    th { background: #eef4f8; color: #0e2f44; }
    img { width: 100%; border: 1px solid #d8e1ea; border-radius: 8px; margin-bottom: 14px; }
    code { background: #eef4f8; padding: 2px 5px; border-radius: 4px; }
    @page { size: A4; margin: 10mm; }
    @media print {
      body { background: white; font-size: 10px; }
      header { padding: 16px 20px; }
      header h1 { font-size: 22px; margin-bottom: 6px; }
      header p { font-size: 10px; }
      main { max-width: none; margin: 0; padding: 0; }
      section { padding: 12px; margin-bottom: 8px; border-radius: 6px; break-inside: auto; page-break-inside: auto; }
      .hero { padding: 13px; border-left-width: 5px; break-inside: avoid; page-break-inside: avoid; }
      .hero h2 { font-size: 18px; margin-bottom: 7px; }
      h2 { font-size: 15px; margin-bottom: 8px; }
      h3 { font-size: 12px; margin-bottom: 5px; }
      p { line-height: 1.35; margin: 5px 0; }
      .card { padding: 9px; margin-bottom: 7px; break-inside: avoid; page-break-inside: avoid; }
      .badge { padding: 3px 7px; font-size: 9px; }
      .grid { grid-template-columns: repeat(3, 1fr); gap: 6px; }
      .metric { padding: 8px; }
      .metric span { font-size: 9px; margin-bottom: 4px; }
      .metric strong { font-size: 13px; }
      table { font-size: 8.5px; }
      th, td { padding: 4px; }
      ul { margin: 6px 0 0 18px; padding: 0; }
      li { margin-bottom: 3px; line-height: 1.25; }
      img { margin-bottom: 6px; border-radius: 5px; }
      .pressure-map { max-height: 170mm; object-fit: contain; }
      .chart-img { max-height: 82mm; object-fit: contain; }
      .evidence-section, .visual-summary { break-before: page; page-break-before: always; }
      .boundary-section { display: none; }
      .screen-only { display: none; }
    }
    """


def badge_class(level: str) -> str:
    return {"Low": "low", "Medium": "medium", "High": "high"}.get(level, "medium")


def generated_at_text() -> str:
    now = datetime.now().astimezone()
    offset = now.strftime("%z")
    offset_text = f"UTC{offset[:3]}:{offset[3:]}" if offset else "local time"
    return f"{now:%Y-%m-%d %H:%M:%S} {offset_text}"


def find_browser_executable() -> str | None:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    for command in ["chrome", "msedge"]:
        resolved = shutil.which(command)
        if resolved:
            return resolved
    return None


def export_html_to_pdf(html_path: Path, pdf_path: Path) -> bool:
    browser = find_browser_executable()
    if not browser:
        return False
    html_path = html_path.resolve()
    pdf_path = pdf_path.resolve()
    with tempfile.TemporaryDirectory(prefix="stepwise_pdf_profile_") as profile_dir:
        command = [
            browser,
            "--headless",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-pdf-header-footer",
            f"--user-data-dir={profile_dir}",
            f"--print-to-pdf={pdf_path}",
            html_path.as_uri(),
        ]
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        except (subprocess.SubprocessError, OSError):
            return False
    return pdf_path.exists() and pdf_path.stat().st_size > 0


def overall_result(cards: list[dict[str, str]]) -> tuple[str, str, str]:
    top = cards[0]
    title = top["title"]
    level = top["level"]
    if title == "No clear posture risk detected":
        return "No clear StepWise posture risk detected", level, (
            "Your pressure and foot-angle signals look close to the current normal baseline under the screening rules."
        )
    if title == "Walking posture cannot be inferred from this file":
        return "This file is not enough for walking-posture screening", level, top["interpretation"]
    return title, level, top["interpretation"]


def user_evidence_basis_rows() -> str:
    rows = [
        (
            "Contact / stance detection",
            "The code uses total plantar pressure with an adaptive on/off threshold.",
            (
                "Pressure-insole studies support using summed plantar pressure to detect stance/contact. "
                f"The {EARLY_FOREFOOT_RATIO_THRESHOLD:.2f} ratio and {0.08:.2f} threshold-ratio values are StepWise prototype screening thresholds, not medical cutoffs."
            ),
        ),
        (
            "Inversion / eversion tendency",
            "Primary signal: Pitch change from standing neutral. Pressure distribution is supporting evidence only.",
            (
                "Inversion/eversion is a frontal-plane foot tilt, so a calibrated IMU angle is more direct than four pressure points. "
                "Public foot-posture datasets support the direction of pressure evidence: pronated feet tend toward more medial/arch loading, while supinated feet tend toward more lateral/external loading."
            ),
        ),
        (
            "Forefoot / rearfoot landing",
            "The report combines early-stance forefoot/rearfoot pressure with Roll-based landing angle.",
            (
                "Gait-event literature supports using initial contact timing, and foot-mounted IMU studies support foot-ground angle as a landing-pattern feature. "
                f"The current StepWise angle deadband is +/-{LANDING_ANGLE_THRESHOLD_DEG:.0f} deg for robust demonstration screening."
            ),
        ),
        (
            "Pressure transfer / push-off",
            "The report checks whether pressure moves from heel/arch toward forefoot during stance.",
            (
                "Normal stance includes forward pressure progression toward the forefoot during propulsion. "
                "StepWise reports this as a four-sensor proxy rather than a force-plate center-of-pressure measurement."
            ),
        ),
        (
            "Confidence check",
            "The report uses valid stance count, sampling rate, and conflicting evidence checks before assigning a clear tendency.",
            "If the recording is too short or pressure and IMU disagree, the report should say mixed or inconclusive instead of forcing a posture label.",
        ),
    ]
    return "".join(
        f"<tr><td>{html.escape(metric)}</td><td>{html.escape(rule)}</td><td>{html.escape(basis)}</td></tr>"
        for metric, rule, basis in rows
    )


def user_reference_items() -> str:
    selected = [
        PRIMARY_REFERENCE,
        SUPPORTING_REFERENCES[0],
        SUPPORTING_REFERENCES[2],
        SUPPORTING_REFERENCES[5],
        SUPPORTING_REFERENCES[6],
        SUPPORTING_REFERENCES[7],
    ]
    return "".join(
        f"<li><a href='{html.escape(ref['url'])}'>{html.escape(ref['name'])}</a>: {html.escape(ref['use_in_stepwise'])}</li>"
        for ref in selected
    )


def write_user_report(
    output_dir: Path,
    input_path: Path,
    summary: dict,
    metrics: dict[str, float],
    cards: list[dict[str, str]],
    *,
    generate_pdf: bool,
) -> None:
    title, level, subtitle = overall_result(cards)
    top_card = cards[0]
    top_next_step = top_card.get("action", "Repeat the trial under the same sensor placement.")
    generated_at = generated_at_text()
    cards_html = "".join(
        f"""
        <article class="card">
          <div class="card-head"><h3>{html.escape(card['title'])}</h3><span class="badge {badge_class(card['level'])}">{html.escape(card['level'])}</span></div>
          <p><strong>What this suggests:</strong> {html.escape(card['interpretation'])}</p>
          <p><strong>Sensor evidence:</strong> {html.escape(card['evidence'])}</p>
          <p><strong>Recommended next step:</strong> {html.escape(card['action'])}</p>
        </article>
        """
        for card in cards
    )
    evidence_rows = user_evidence_basis_rows()
    reference_items = user_reference_items()
    html_text = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>StepWise User Report</title><style>{report_css()}</style></head><body>
    <header><h1>StepWise Gait Screening Report</h1><p>Generated: {html.escape(generated_at)}</p></header><main>
    <section class="hero"><h2>{html.escape(title)}</h2><p>{html.escape(subtitle)}</p><p><span class="badge {badge_class(level)}">{html.escape(level)} risk</span></p><p><strong>Recommended next step:</strong> {html.escape(top_next_step)}</p><p class="note">Screening aid for posture tendency and loading pattern.</p></section>
    <section><h2>Posture-Risk Feedback</h2>{cards_html}</section>
    <section><h2>Key Measurements</h2><div class="grid">
      <div class="metric"><span>Confidence</span><strong>{html.escape(str(summary.get('data_quality', '-')))}</strong></div>
      <div class="metric"><span>Valid stance phases</span><strong>{summary.get('detected_steps_single_foot', '-')}</strong></div>
      <div class="metric"><span>Sample rate</span><strong>{fmt(summary.get('estimated_sample_rate_hz'), 1)} Hz</strong></div>
      <div class="metric"><span>Duration</span><strong>{fmt(summary.get('duration_s'), 2)} s</strong></div>
      <div class="metric"><span>Medial forefoot ratio</span><strong>{fmt(metrics.get('MedialRatio_mean'))}</strong></div>
      <div class="metric"><span>Lateral forefoot ratio</span><strong>{fmt(metrics.get('LateralRatio_mean'))}</strong></div>
      <div class="metric"><span>Arch ratio</span><strong>{fmt(metrics.get('ArchRatio_mean'))}</strong></div>
      <div class="metric"><span>Landing sole-ground angle</span><strong>{fmt(metrics.get('LandingSoleGroundAngle_deg'), 2)} deg</strong></div>
      <div class="metric"><span>Stance Pitch delta</span><strong>{fmt(metrics.get('PitchDelta_stance_from_standing'), 2)} deg</strong></div>
    </div></section>
    <section class="evidence-section"><h2>How StepWise Made This Judgment</h2>
      <p>StepWise combines calibrated IMU angles with plantar-pressure timing and distribution. Published references are used for the biomechanical direction of the evidence; the exact numeric thresholds are prototype screening thresholds for this four-sensor insole.</p>
      <table><tr><th>Part of analysis</th><th>Rule used in this report</th><th>Evidence basis</th></tr>{evidence_rows}</table>
    </section>
    <section><h2>Reference Basis</h2><ul>{reference_items}</ul></section>
    <section class="visual-summary"><h2>Visual Summary</h2><img class="chart-img" src="pressure_stance.png" alt="pressure"><img class="chart-img screen-only" src="orientation.png" alt="orientation"></section>
    <section class="boundary-section"><h2>Boundary</h2><p class="note">This report is a screening aid for posture tendency and loading pattern. If pain, instability, or repeated abnormal results occur, consult a qualified professional.</p></section>
    </main></body></html>"""
    html_path = output_dir / "user_report.html"
    html_path.write_text(html_text, encoding="utf-8")
    if generate_pdf:
        export_html_to_pdf(html_path, output_dir / "user_report.pdf")


def write_technical_report(
    output_dir: Path,
    input_path: Path,
    summary: dict,
    metrics: dict[str, float],
    baseline: dict[str, float],
    cards: list[dict[str, str]],
    baseline_source: str,
    reference_mode: str,
    standing_calibration: dict[str, float | str | int] | None,
) -> None:
    generated_at = generated_at_text()
    metric_rows = "".join(
        f"<tr><td>{html.escape(key)}</td><td>{fmt(metrics.get(key))}</td><td>{fmt(baseline.get(key))}</td><td>{fmt(ratio_delta(metrics, baseline, key))}</td></tr>"
        for key in METRIC_KEYS
    )
    card_rows = "".join(
        f"<tr><td>{html.escape(c['title'])}</td><td>{html.escape(c['level'])}</td><td>{html.escape(c['evidence'])}</td><td>{html.escape(c['interpretation'])}</td></tr>"
        for c in cards
    )
    ref_rows = "".join(
        f"<tr><td><a href='{html.escape(ref['url'])}'>{html.escape(ref['name'])}</a></td><td>{html.escape(ref['use_in_stepwise'])}</td></tr>"
        for ref in [PRIMARY_REFERENCE] + SUPPORTING_REFERENCES
    )
    standing_text = (
        "No standing calibration file was provided. Landing sole-ground angle and Pitch delta are shown as raw IMU trends only."
        if not standing_calibration
        else "Standing calibration: "
        + ", ".join(
            [
                f"Roll neutral={fmt(standing_calibration.get('Roll_neutral_mean'), 2)} deg",
                f"Pitch neutral={fmt(standing_calibration.get('Pitch_neutral_mean'), 2)} deg",
                f"static CoP_ML={fmt(standing_calibration.get('StaticCoP_ML_mean'))}",
            ]
        )
    )
    html_text = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>StepWise Technical Report</title><style>{report_css()}</style></head><body>
    <header><h1>StepWise Technical Report</h1><p>Generated: {html.escape(generated_at)}</p></header><main>
    <section><h2>Reference strategy</h2><p><strong>Reference mode:</strong> {html.escape(reference_mode)}</p><p><strong>Primary external dataset:</strong> <a href="{PRIMARY_REFERENCE['url']}">{html.escape(PRIMARY_REFERENCE['name'])}</a>.</p><p>{html.escape(PRIMARY_REFERENCE['why'])}</p><p><strong>How StepWise uses it:</strong> {html.escape(PRIMARY_REFERENCE['use_in_stepwise'])}</p><p><strong>Device baseline:</strong> {html.escape(baseline_source)}</p><p><strong>Standing calibration:</strong> {html.escape(standing_text)}</p></section>
    <section><h2>Decision rule</h2><p>External datasets provide the biomechanical direction of evidence, but StepWise does not copy public numeric thresholds as diagnoses. Frontal-plane inversion/eversion screening is driven by Pitch relative to standing neutral: for the current left-foot mounting, positive Pitch delta indicates right/medial-side tilt and negative Pitch delta indicates left/lateral-side tilt. Plantar pressure is treated as supporting evidence only; pressure alone is reported as medial or lateral loading bias, not as confirmed inversion or eversion. Roll relative to standing neutral is used for the landing sole-ground angle: negative means forefoot lower than heel, positive means forefoot higher than heel.</p></section>
    <section><h2>Risk output</h2><table><tr><th>Risk</th><th>Level</th><th>Evidence</th><th>Interpretation</th></tr>{card_rows}</table></section>
    <section><h2>Metric comparison</h2><table><tr><th>Metric</th><th>Current</th><th>Device normal baseline</th><th>Delta</th></tr>{metric_rows}</table></section>
    <section><h2>Reference list</h2><table><tr><th>Reference</th><th>Use</th></tr>{ref_rows}</table></section>
    <section><h2>Charts</h2><img class="chart-img" src="pressure_stance.png" alt="pressure"><img class="chart-img" src="acceleration.png" alt="acceleration"><img class="chart-img" src="orientation.png" alt="orientation"></section>
    </main></body></html>"""
    (output_dir / "technical_report.html").write_text(html_text, encoding="utf-8")


def write_data_guide(output_dir: Path) -> None:
    ref_rows = "".join(
        f"<tr><td><a href='{html.escape(ref['url'])}'>{html.escape(ref['name'])}</a></td><td>{html.escape(ref['use_in_stepwise'])}</td></tr>"
        for ref in [PRIMARY_REFERENCE] + SUPPORTING_REFERENCES
    )
    evidence_level_rows = f"""
    <tr><td>Stance segmentation</td><td><code>P_total</code> adaptive hysteresis</td><td>Pressure-insole literature supports summed pressure for contact detection.</td><td><code>T_on=max(5 N, 0.08*max(P_total))</code> is a StepWise noise-robust threshold.</td></tr>
    <tr><td>Early stance window</td><td>First 20% of detected stance</td><td>Gait-phase conventions define loading response near the beginning of stance.</td><td>20% is a practical window for this short single-foot recording, not a clinical phase boundary.</td></tr>
    <tr><td>Late stance window</td><td>Last 35% of detected stance</td><td>Terminal stance and pre-swing are propulsion-related gait periods.</td><td>65% stance start is used to capture late forefoot push-off with four pressure channels.</td></tr>
    <tr><td>Forefoot/rearfoot landing</td><td><code>EarlyFrontRatio</code>, <code>EarlyRearRatio</code>, and Roll landing angle</td><td>Foot strike can be described by first contact region and foot-ground angle.</td><td><code>0.65</code> ratio and <code>+/-12 deg</code> angle are screening cutoffs chosen to avoid weak/ambiguous flags.</td></tr>
    <tr><td>Inversion/eversion tendency</td><td><code>PitchDelta</code> from standing neutral</td><td>Inversion/eversion is frontal-plane foot tilt; IMU workflows require calibration to a known pose.</td><td><code>+/-5 deg</code> is an engineering deadband for this IMU mounting, not a medical diagnostic cutoff.</td></tr>
    <tr><td>Medial/lateral loading</td><td><code>MedialRatio</code>, <code>LateralRatio</code>, <code>CoP_ML</code></td><td>Foot-posture datasets show different plantar-pressure distributions for pronated and supinated groups.</td><td>Pressure-only evidence is reported as loading bias unless calibrated Pitch supports the same direction.</td></tr>
    """
    html_text = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>StepWise Data Guide</title><style>{report_css()}</style></head><body>
    <header><h1>StepWise Data Guide</h1><p>Reference-guided formulas and decision logic</p></header><main>
    <section><h2>Which public dataset should StepWise use?</h2><p>Use <strong>{html.escape(PRIMARY_REFERENCE['name'])}</strong> as the main reference for inversion/eversion-related screening. It is the closest match because it explicitly separates normal, highly pronated, and highly supinated foot postures and includes both pressure and foot kinematics.</p></section>
    <section><h2>Sensor mapping</h2><table><tr><th>Anatomical role</th><th>StepWise variable</th><th>Interpretation</th></tr>
    <tr><td>P2 heel / rearfoot channel</td><td>RearRatio</td><td>Rearfoot loading and initial heel contact</td></tr>
    <tr><td>P3 arch / midfoot channel</td><td>ArchRatio</td><td>Midfoot or arch contact/loading proxy</td></tr>
    <tr><td>P4 big-toe side / medial forefoot channel</td><td>MedialRatio</td><td>Medial forefoot loading; pronation/eversion-related pressure evidence</td></tr>
    <tr><td>P1 little-toe side / lateral forefoot channel</td><td>LateralRatio</td><td>Lateral forefoot loading; supination/inversion-related pressure evidence</td></tr>
    <tr><td>Pitch</td><td>PitchDelta_stance_from_standing</td><td>Left-foot frontal-plane tilt: negative = left/lateral tilt, positive = right/medial tilt</td></tr>
    <tr><td>Roll</td><td>LandingSoleGroundAngle_deg</td><td>Landing foot-ground angle: negative = forefoot lower than heel, positive = forefoot higher than heel</td></tr>
    </table></section>
    <section><h2>Core formulas</h2><table><tr><th>Metric</th><th>Formula</th><th>Use</th></tr>
    <tr><td>Total pressure</td><td><code>P_total=sum(all pressure channels)</code></td><td>Contact detection and normalization</td></tr>
    <tr><td>RearRatio</td><td><code>P_heel/P_total</code></td><td>Heel/rearfoot loading</td></tr>
    <tr><td>ArchRatio</td><td><code>P_arch/P_total</code></td><td>Midfoot/arch loading proxy</td></tr>
    <tr><td>MedialRatio</td><td><code>P_medial/P_total</code></td><td>Big-toe side loading</td></tr>
    <tr><td>LateralRatio</td><td><code>P_lateral/P_total</code></td><td>Little-toe side loading</td></tr>
    <tr><td>CoP_ML proxy</td><td><code>(0.45*P_medial-0.45*P_lateral)/P_total</code></td><td>Positive is medial, negative is lateral</td></tr>
    <tr><td>Landing sole-ground angle</td><td><code>mean(Roll in early stance)-standing Roll neutral</code></td><td>Negative: front lower; positive: front higher</td></tr>
    <tr><td>Pitch delta</td><td><code>mean(Pitch in stance)-standing Pitch neutral</code></td><td>Left-foot mounting: negative supports lateral/inversion tilt; positive supports medial/eversion tilt</td></tr>
    <tr><td>Baseline delta</td><td><code>delta=current_metric-normal_baseline_metric</code></td><td>Device-specific comparison</td></tr>
    <tr><td>Stride CV</td><td><code>std(stride_time)/mean(stride_time)</code></td><td>Step timing variability</td></tr>
    </table></section>
    <section><h2>Evidence Level and Threshold Origin</h2><p>The literature supports the measurement strategy and the direction of evidence. The exact numeric thresholds are StepWise screening thresholds because this prototype has four pressure sensors, one foot-mounted IMU, and device-specific mounting orientation.</p><table><tr><th>Decision part</th><th>StepWise rule</th><th>Literature or open-source support</th><th>Threshold status</th></tr>{evidence_level_rows}</table></section>
    <section><h2>Risk rules used in the code</h2><table><tr><th>Risk</th><th>Trigger evidence</th><th>Reference direction</th></tr>
    <tr><td>Eversion / over-pronation tendency</td><td>Pitch delta from standing >= +5 deg. MedialRatio, ArchRatio, and CoP_ML can strengthen the evidence but cannot confirm eversion alone.</td><td>Anatomically, eversion turns the sole laterally; pronated feet tend to show more medial loading in the primary dataset.</td></tr>
    <tr><td>Inversion / over-supination tendency</td><td>Pitch delta from standing <= -5 deg. LateralRatio and lateral CoP_ML can strengthen the evidence but cannot confirm inversion alone.</td><td>Anatomically, inversion turns the sole medially; supinated feet tend to show more external/lateral loading in the primary dataset.</td></tr>
    <tr><td>Medial/lateral loading bias</td><td>Pressure evidence without supporting Pitch delta</td><td>Reported as loading distribution only, because four FSRs cannot medically confirm frontal-plane foot posture.</td></tr>
    <tr><td>Forefoot-first landing</td><td>EarlyFrontRatio >= 0.650 and/or landing sole-ground angle <= -12 deg; conflicting pressure/angle evidence is reported as mixed.</td><td>FSR insole studies use heel/forefoot contact timing to describe gait events; calibrated foot-ground angle adds kinematic evidence.</td></tr>
    <tr><td>Rearfoot-heavy landing</td><td>EarlyRearRatio >= 0.650 and/or landing sole-ground angle >= 12 deg; conflicting pressure/angle evidence is reported as mixed.</td><td>Heel pressure and positive landing angle support heel/rearfoot-first landing.</td></tr>
    <tr><td>Reduced push-off</td><td>Late forefoot push-off proxy < 0.750 or delta <= -0.080</td><td>Forefoot/metatarsal loading is expected during propulsion.</td></tr>
    </table></section>
    <section><h2>Reference list</h2><table><tr><th>Reference</th><th>Use</th></tr>{ref_rows}</table></section>
    <section><h2>Important boundary</h2><p class="note">Public datasets should not be copied as direct numeric thresholds for this four-FSR shoe. They justify the direction of the evidence. The numeric threshold must be calibrated using StepWise normal walking trials collected with the same hardware, same shoe, and same sensor placement.</p></section>
    </main></body></html>"""
    (output_dir / "data_guide.html").write_text(html_text, encoding="utf-8")


def write_json_outputs(
    output_dir: Path,
    metrics: dict[str, float],
    baseline: dict[str, float],
    summary: dict,
    cards: list[dict[str, str]],
    baseline_source: str,
    reference_mode: str,
    standing_calibration: dict[str, float | str | int] | None,
) -> None:
    serializable = {
        "summary": summary,
        "metrics": metrics,
        "baseline_metrics": baseline,
        "baseline_source": baseline_source,
        "reference_mode": reference_mode,
        "standing_calibration": standing_calibration,
        "primary_reference": PRIMARY_REFERENCE,
        "supporting_references": SUPPORTING_REFERENCES,
        "posture_risks": cards,
    }
    def json_safe(value: object) -> object:
        if isinstance(value, dict):
            return {key: json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_safe(item) for item in value]
        if isinstance(value, np.generic):
            value = value.item()
        if isinstance(value, float) and not np.isfinite(value):
            return None
        return value

    (output_dir / "reference_screening_result.json").write_text(
        json.dumps(json_safe(serializable), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    pd.DataFrame(cards).to_csv(output_dir / "posture_risk_output.csv", index=False, encoding="utf-8-sig")
    metric_rows = []
    for key in METRIC_KEYS:
        metric_rows.append(
            {
                "metric": key,
                "current": metrics.get(key, np.nan),
                "baseline": baseline.get(key, np.nan),
                "delta": ratio_delta(metrics, baseline, key),
            }
        )
    pd.DataFrame(metric_rows).to_csv(output_dir / "reference_metric_comparison.csv", index=False, encoding="utf-8-sig")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reference-guided StepWise posture screening")
    parser.add_argument("input", nargs="+", help="One or more StepWise TXT data files")
    parser.add_argument("--output-dir", default="stepwise_reference_report", help="Output folder")
    parser.add_argument("--pdf", action="store_true", help="Also export user_report.pdf. Default: HTML only.")
    parser.add_argument(
        "--reference-mode",
        choices=["public-only", "device-baseline"],
        default="public-only",
        help="public-only uses literature direction and within-trial pressure balance; device-baseline compares against a StepWise normal walking baseline.",
    )
    parser.add_argument(
        "--device-baseline-dir",
        default="stepwise_batch_reports/normal",
        help="Analyzed normal-baseline folder containing gait_steps_analysis.csv and processed_gait_data.csv",
    )
    parser.add_argument(
        "--normal-file",
        default=None,
        help="Optional raw normal-walking TXT file. If provided, it is analyzed first and used as the device baseline.",
    )
    parser.add_argument(
        "--standing-file",
        default=None,
        help="Optional static standing TXT file for IMU neutral posture and sensor-placement calibration. This is not a healthy baseline.",
    )
    parser.add_argument(
        "--pitch-eversion-sign",
        choices=["positive", "negative"],
        default="positive",
        help="Set after calibration. For the current left-foot mounting, positive Pitch delta is treated as eversion and negative Pitch delta as inversion.",
    )
    parser.add_argument(
        "--roll-eversion-sign",
        choices=["positive", "negative"],
        dest="pitch_eversion_sign",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--smooth-window", type=int, default=3)
    parser.add_argument("--min-threshold-n", type=float, default=5.0)
    parser.add_argument("--threshold-ratio", type=float, default=0.08)
    parser.add_argument("--min-stance-s", type=float, default=0.08)
    parser.add_argument("--body-weight-n", type=float, default=None)
    parser.add_argument("--heel", nargs="+", default=["P2"], help="Pressure channels located at heel/rearfoot")
    parser.add_argument("--arch", nargs="+", default=["P3"], help="Pressure channels located at arch/midfoot")
    parser.add_argument("--medial-forefoot", nargs="+", default=["P4"], help="Pressure channels at big-toe side / medial forefoot")
    parser.add_argument("--lateral-forefoot", nargs="+", default=["P1"], help="Pressure channels at little-toe side / lateral forefoot")
    parser.add_argument("--toe", nargs="*", default=[], help="Optional toe channels")
    return parser


def unique_report_dir(root: Path, input_path: Path, used: set[str]) -> Path:
    stem = input_path.stem.strip() or "trial"
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in stem)
    safe = safe.strip("._") or "trial"
    candidate = safe
    i = 2
    while candidate.lower() in used:
        candidate = f"{safe}_{i}"
        i += 1
    used.add(candidate.lower())
    return root / candidate


def write_batch_index(output_dir: Path, rows: list[dict[str, object]]) -> None:
    row_html = "".join(
        f"""
        <tr>
          <td>{html.escape(str(row['trial']))}</td>
          <td>{html.escape(str(row['top_title']))}</td>
          <td>{html.escape(str(row['top_level']))}</td>
          <td>{html.escape(str(row['quality']))}</td>
          <td>{html.escape(str(row['steps']))}</td>
          <td><a href="{html.escape(str(row['folder']))}/user_report.html">User report</a></td>
        </tr>
        """
        for row in rows
    )
    html_text = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>StepWise Batch Reports</title><style>{report_css()}</style></head><body>
    <header><h1>StepWise Batch Reports</h1><p>Generated: {html.escape(generated_at_text())}</p></header><main>
    <section><h2>Report Index</h2><table><tr><th>Trial</th><th>Top result</th><th>Level</th><th>Quality</th><th>Stance phases</th><th>Open</th></tr>{row_html}</table></section>
    </main></body></html>"""
    (output_dir / "index.html").write_text(html_text, encoding="utf-8")
    pd.DataFrame(rows).to_csv(output_dir / "batch_summary.csv", index=False, encoding="utf-8-sig")


def run_one_file(args: argparse.Namespace, input_path: Path, output_dir: Path) -> tuple[dict, list[dict[str, str]]]:
    if not input_path.exists():
        raise FileNotFoundError(f"Cannot find input file: {input_path}")
    layout = make_sensor_layout(args)

    processed, steps, summary = analyze_stepwise_file(
        input_path,
        output_dir,
        layout=layout,
        smooth_window=args.smooth_window,
        min_threshold_n=args.min_threshold_n,
        threshold_ratio=args.threshold_ratio,
        min_stance_s=args.min_stance_s,
        body_weight_n=args.body_weight_n,
    )
    metrics = metric_means(steps, processed)

    standing_calibration = None
    if args.standing_file:
        standing_calibration = analyze_standing_calibration(
            Path(args.standing_file),
            output_dir / "_standing_calibration",
            layout=layout,
            smooth_window=args.smooth_window,
            body_weight_n=args.body_weight_n,
        )
    add_standing_derived_metrics(metrics, standing_calibration)

    reference_mode = args.reference_mode
    if args.normal_file:
        reference_mode = "device-baseline"
        normal_path = Path(args.normal_file)
        baseline_dir = output_dir / "_normal_baseline"
        baseline_processed, baseline_steps, _baseline_summary = analyze_stepwise_file(
            normal_path,
            baseline_dir,
            layout=layout,
            smooth_window=args.smooth_window,
            min_threshold_n=args.min_threshold_n,
            threshold_ratio=args.threshold_ratio,
            min_stance_s=args.min_stance_s,
            body_weight_n=args.body_weight_n,
        )
        baseline = metric_means(baseline_steps, baseline_processed)
        baseline_source = f"raw normal file analyzed at {baseline_dir}"
    elif reference_mode == "device-baseline":
        baseline_dir = Path(args.device_baseline_dir)
        baseline_processed, baseline_steps, _baseline_summary = load_analyzed_folder(baseline_dir)
        baseline = metric_means(baseline_steps, baseline_processed)
        baseline_source = str(baseline_dir)
    else:
        baseline = {key: np.nan for key in METRIC_KEYS}
        baseline_source = "No local healthy baseline used; public dataset direction + optional standing calibration"

    if reference_mode == "device-baseline":
        cards = build_posture_risk_cards(
            metrics,
            baseline,
            summary,
            pitch_eversion_sign=args.pitch_eversion_sign,
        )
    else:
        cards = build_public_reference_cards(
            metrics,
            summary,
            pitch_eversion_sign=args.pitch_eversion_sign,
            standing_calibration=standing_calibration,
        )

    write_json_outputs(output_dir, metrics, baseline, summary, cards, baseline_source, reference_mode, standing_calibration)
    write_user_report(output_dir, input_path, summary, metrics, cards, generate_pdf=args.pdf)
    write_technical_report(
        output_dir,
        input_path,
        summary,
        metrics,
        baseline,
        cards,
        baseline_source,
        reference_mode,
        standing_calibration,
    )
    write_data_guide(output_dir)
    return summary, cards


def main() -> None:
    args = build_parser().parse_args()
    input_paths = [Path(item) for item in args.input]
    root_output_dir = Path(args.output_dir)
    root_output_dir.mkdir(parents=True, exist_ok=True)

    batch_mode = len(input_paths) > 1
    rows: list[dict[str, object]] = []
    used: set[str] = set()

    for input_path in input_paths:
        output_dir = unique_report_dir(root_output_dir, input_path, used) if batch_mode else root_output_dir
        summary, cards = run_one_file(args, input_path, output_dir)
        top = cards[0] if cards else {"title": "No output", "level": "-"}
        rows.append(
            {
                "trial": input_path.stem,
                "folder": output_dir.name if batch_mode else ".",
                "top_title": top.get("title", "No output"),
                "top_level": top.get("level", "-"),
                "quality": summary.get("data_quality", "-"),
                "steps": summary.get("detected_steps_single_foot", "-"),
                "sample_rate_hz": summary.get("estimated_sample_rate_hz", "-"),
            }
        )

    if batch_mode:
        write_batch_index(root_output_dir, rows)
        print("Batch StepWise screening complete.")
        print(f"Inputs: {len(input_paths)} files")
        print(f"Output directory: {root_output_dir.resolve()}")
        print(f"Open: {(root_output_dir / 'index.html').resolve()}")
        print("PDF export: " + ("enabled" if args.pdf else "disabled"))
        return

    print("Reference-guided StepWise screening complete.")
    print(f"Input: {input_paths[0]}")
    print(f"Output: {root_output_dir.resolve()}")
    print(f"Primary reference: {PRIMARY_REFERENCE['name']}")
    print("PDF export: " + ("enabled" if args.pdf else "disabled"))
    print("Top posture risk:")
    for card in cards[:3]:
        print(f"- {card['level']}: {card['title']}")
        print(f"  Evidence: {card['evidence']}")


if __name__ == "__main__":
    main()
