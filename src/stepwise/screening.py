"""Data-quality and evidence-conflict screening rules.

The rules intentionally produce screening language, never a clinical diagnosis.
When evidence is sparse or contradictory, the safe outcome is explicit rather
than a forced posture label.
"""

from __future__ import annotations

import math
from typing import Any

from .models import RiskCard


FRONTAL_TILT_THRESHOLD_DEG = 5.0
STRONG_FRONTAL_TILT_DEG = 8.0
ML_RATIO_MARGIN_THRESHOLD = 0.06
COP_ML_THRESHOLD = 0.04
ARCH_RATIO_SUPPORT_THRESHOLD = 0.10
LANDING_ANGLE_THRESHOLD_DEG = 12.0
EARLY_FOREFOOT_RATIO_THRESHOLD = 0.65
EARLY_REARFOOT_RATIO_THRESHOLD = 0.65


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _number(metrics: dict[str, Any], key: str) -> float:
    value = metrics.get(key)
    return float(value) if _finite(value) else math.nan


def _fmt(value: Any, digits: int = 3) -> str:
    if not _finite(value):
        return "-"
    return f"{float(value):.{digits}f}"


def _metric_line(metrics: dict[str, Any], key: str, label: str) -> str:
    return f"{label}: {_fmt(metrics.get(key))}"


def build_risk_cards(
    metrics: dict[str, Any],
    summary: dict[str, Any],
    *,
    pitch_eversion_sign: str,
    standing_calibration: dict[str, Any] | None,
) -> tuple[RiskCard, ...]:
    """Build ordered, explainable screening cards from session evidence."""
    cards: list[RiskCard] = []
    quality = str(summary.get("data_quality", "Low"))
    stance_count = int(summary.get("detected_steps_single_foot") or 0)
    low_confidence = quality == "Low" or stance_count < 4

    def add(
        title: str,
        level: str,
        evidence: list[str],
        interpretation: str,
        action: str,
        limitation: str,
    ) -> None:
        if low_confidence and title != "Walking posture cannot be inferred from this file":
            limitation += " This trial has low confidence because it contains too few valid stance phases."
        cards.append(
            RiskCard(
                title=title,
                level=level,
                evidence=" | ".join(evidence),
                interpretation=interpretation,
                action=action,
                limitation=limitation,
            )
        )

    if stance_count < 3:
        add(
            "Walking posture cannot be inferred from this file",
            "High",
            [
                f"data quality={quality}",
                f"valid stance phases={stance_count}",
                f"sample rate={_fmt(summary.get('estimated_sample_rate_hz'))} Hz",
            ],
            "The file does not contain enough valid walking cycles for posture-risk interpretation.",
            "Re-test with at least 10-20 valid stance phases.",
            "Posture risk should not be inferred from non-walking or very short recordings.",
        )
        return tuple(cards)

    medial = _number(metrics, "MedialRatio_mean")
    lateral = _number(metrics, "LateralRatio_mean")
    arch = _number(metrics, "ArchRatio_mean")
    cop_ml = _number(metrics, "CoP_ML_mean")
    medial_margin = medial - lateral if _finite(medial) and _finite(lateral) else math.nan
    lateral_margin = -medial_margin if _finite(medial_margin) else math.nan
    pitch_delta = _number(metrics, "PitchDelta_stance_from_standing")
    landing_angle = _number(metrics, "LandingSoleGroundAngle_deg")

    has_pitch_calibration = bool(standing_calibration) and _finite(pitch_delta)
    eversion_imu = False
    inversion_imu = False
    if has_pitch_calibration and abs(pitch_delta) >= FRONTAL_TILT_THRESHOLD_DEG:
        positive_eversion = pitch_eversion_sign == "positive"
        eversion_imu = (
            pitch_delta >= FRONTAL_TILT_THRESHOLD_DEG
            if positive_eversion
            else pitch_delta <= -FRONTAL_TILT_THRESHOLD_DEG
        )
        inversion_imu = (
            pitch_delta <= -FRONTAL_TILT_THRESHOLD_DEG
            if positive_eversion
            else pitch_delta >= FRONTAL_TILT_THRESHOLD_DEG
        )

    eversion_pressure_hits: list[str] = []
    if _finite(medial_margin) and medial_margin >= ML_RATIO_MARGIN_THRESHOLD:
        eversion_pressure_hits.append("medial forefoot loading is higher than lateral forefoot loading")
    if _finite(cop_ml) and cop_ml >= COP_ML_THRESHOLD:
        eversion_pressure_hits.append("CoP_ML proxy shifts toward the medial side")
    if _finite(arch) and arch >= ARCH_RATIO_SUPPORT_THRESHOLD:
        eversion_pressure_hits.append("P2 arch/midfoot loading is elevated")

    inversion_pressure_hits: list[str] = []
    if _finite(lateral_margin) and lateral_margin >= ML_RATIO_MARGIN_THRESHOLD:
        inversion_pressure_hits.append("lateral forefoot loading is higher than medial forefoot loading")
    if _finite(cop_ml) and cop_ml <= -COP_ML_THRESHOLD:
        inversion_pressure_hits.append("CoP_ML proxy shifts toward the lateral side")

    eversion_pressure = bool(eversion_pressure_hits)
    inversion_pressure = bool(inversion_pressure_hits)
    pitch_evidence = (
        f"Pitch delta from standing={_fmt(pitch_delta, 2)}"
        if standing_calibration
        else "Pitch direction not used: no standing calibration file"
    )

    if (
        (eversion_imu and inversion_pressure)
        or (inversion_imu and eversion_pressure)
        or (eversion_pressure and inversion_pressure)
    ):
        add(
            "Mixed inversion/eversion evidence",
            "Medium",
            [
                _metric_line(metrics, "MedialRatio_mean", "medial forefoot ratio"),
                _metric_line(metrics, "LateralRatio_mean", "lateral forefoot ratio"),
                f"medial-lateral margin={_fmt(medial_margin)}",
                _metric_line(metrics, "CoP_ML_mean", "CoP_ML proxy"),
                f"Pitch delta from standing={_fmt(pitch_delta, 2)}",
            ],
            "Pressure and calibrated Pitch do not point cleanly to one frontal-plane tendency.",
            "Repeat static standing calibration plus one obvious inversion and one obvious eversion trial to confirm the IMU sign.",
            "When frontal-plane IMU and pressure evidence conflict, StepWise should not force an inversion or eversion label.",
        )
    elif eversion_imu:
        level = "High" if eversion_pressure or abs(pitch_delta) >= STRONG_FRONTAL_TILT_DEG else "Medium"
        add(
            "Possible eversion / over-pronation related tendency",
            level,
            [
                _metric_line(metrics, "MedialRatio_mean", "medial forefoot ratio"),
                _metric_line(metrics, "LateralRatio_mean", "lateral forefoot ratio"),
                f"medial-lateral margin={_fmt(medial_margin)}",
                _metric_line(metrics, "ArchRatio_mean", "arch ratio"),
                _metric_line(metrics, "CoP_ML_mean", "CoP_ML proxy"),
                pitch_evidence,
                "positive evidence=" + ", ".join(eversion_pressure_hits + ["Pitch shifted toward eversion"]),
            ],
            "The left-foot IMU tilt is toward the medial side relative to standing neutral. Pressure evidence is used only as supporting evidence.",
            "Repeat with a standing calibration file and a frontal/top-view video. Confirm the arch sensor is working.",
            "This is a screening warning, not a diagnosis of flat foot, pronation, or clinical foot eversion.",
        )
    elif inversion_imu:
        level = "High" if inversion_pressure or abs(pitch_delta) >= STRONG_FRONTAL_TILT_DEG else "Medium"
        add(
            "Possible inversion / over-supination related tendency",
            level,
            [
                _metric_line(metrics, "MedialRatio_mean", "medial forefoot ratio"),
                _metric_line(metrics, "LateralRatio_mean", "lateral forefoot ratio"),
                f"lateral-medial margin={_fmt(lateral_margin)}",
                _metric_line(metrics, "CoP_ML_mean", "CoP_ML proxy"),
                pitch_evidence,
                "positive evidence=" + ", ".join(inversion_pressure_hits + ["Pitch shifted toward inversion"]),
            ],
            "The left-foot IMU tilt is toward the lateral side relative to standing neutral. Pressure evidence is used only as supporting evidence.",
            "Repeat with a standing calibration file and inspect lateral shoe/insole loading.",
            "This is a screening warning, not a diagnosis of high arch, ankle instability, or clinical foot inversion.",
        )
    elif eversion_pressure:
        add(
            "Medial loading bias; eversion not confirmed",
            "Low",
            [
                _metric_line(metrics, "MedialRatio_mean", "medial forefoot ratio"),
                _metric_line(metrics, "LateralRatio_mean", "lateral forefoot ratio"),
                f"medial-lateral margin={_fmt(medial_margin)}",
                _metric_line(metrics, "CoP_ML_mean", "CoP_ML proxy"),
                pitch_evidence if has_pitch_calibration else "Pitch calibration unavailable",
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
                _metric_line(metrics, "MedialRatio_mean", "medial forefoot ratio"),
                _metric_line(metrics, "LateralRatio_mean", "lateral forefoot ratio"),
                f"lateral-medial margin={_fmt(lateral_margin)}",
                _metric_line(metrics, "CoP_ML_mean", "CoP_ML proxy"),
                pitch_evidence if has_pitch_calibration else "Pitch calibration unavailable",
            ],
            "The pressure pattern is more lateral, but calibrated Pitch does not support a clear inversion tendency.",
            "Use this as a pressure-loading note and confirm with video or repeated trials.",
            "Pressure distribution alone cannot medically confirm inversion/supination.",
        )

    pressure_forefoot = _number(metrics, "EarlyFrontRatio_mean") >= EARLY_FOREFOOT_RATIO_THRESHOLD
    pressure_rearfoot = _number(metrics, "EarlyRearRatio_mean") >= EARLY_REARFOOT_RATIO_THRESHOLD
    angle_forefoot = _finite(landing_angle) and landing_angle <= -LANDING_ANGLE_THRESHOLD_DEG
    angle_rearfoot = _finite(landing_angle) and landing_angle >= LANDING_ANGLE_THRESHOLD_DEG
    forefoot_evidence: list[str] = []
    rearfoot_evidence: list[str] = []
    if pressure_forefoot:
        forefoot_evidence.append(_metric_line(metrics, "EarlyFrontRatio_mean", "early forefoot ratio"))
    if angle_forefoot:
        forefoot_evidence.append(f"landing sole-ground angle={_fmt(landing_angle, 2)} deg")
    if pressure_rearfoot:
        rearfoot_evidence.append(_metric_line(metrics, "EarlyRearRatio_mean", "early rearfoot ratio"))
    if angle_rearfoot:
        rearfoot_evidence.append(f"landing sole-ground angle={_fmt(landing_angle, 2)} deg")

    if forefoot_evidence and not rearfoot_evidence:
        add(
            "Forefoot-first landing tendency",
            "Medium",
            forefoot_evidence,
            "The forefoot appears to load early or the foot lands with the forefoot lower than the heel.",
            "Confirm with side-view video.",
            "This is a landing-pattern screening flag, not a diagnosis.",
        )
    elif rearfoot_evidence and not forefoot_evidence:
        add(
            "Rearfoot-heavy landing tendency",
            "Medium",
            rearfoot_evidence,
            "The heel/rearfoot appears to load early or the foot lands with the forefoot higher than the heel.",
            "Confirm with side-view video and repeat with more valid stance phases.",
            "This is a landing-pattern screening flag, not a diagnosis of heel pathology.",
        )

    if _number(metrics, "PushOffRatio_late_stance_mean") < 0.75:
        add(
            "Reduced late-stance forefoot push-off warning",
            "Medium",
            [_metric_line(metrics, "PushOffRatio_late_stance_mean", "late push-off proxy")],
            "Forefoot pressure near the end of stance is lower than expected.",
            "Repeat and confirm medial/lateral forefoot sensor placement.",
            "This does not diagnose calf weakness or neurological impairment.",
        )
    if _number(metrics, "CoP_AP_progression") < 0.15:
        add(
            "Reduced heel-to-forefoot pressure transfer",
            "Medium",
            [_metric_line(metrics, "CoP_AP_progression", "anterior CoP progression")],
            "The pressure center did not move strongly from heel/arch toward forefoot.",
            "Inspect the pressure curves and repeat at natural walking speed.",
            "This is a four-sensor proxy, not a pressure-plate CoP measurement.",
        )
    stride_cv = _number(metrics, "StrideCV")
    if stance_count >= 8 and _finite(stride_cv) and stride_cv >= 0.12:
        add(
            "High stride timing variability",
            "Medium",
            [f"stride-time CV={_fmt(stride_cv)}"],
            "Same-foot stride timing varies noticeably across the trial.",
            "Repeat on a straight path with stable speed.",
            "Single-foot timing with a small number of steps is only a screening flag.",
        )

    if not cards:
        add(
            "No clear posture risk detected",
            "Low",
            [
                _metric_line(metrics, "MedialRatio_mean", "medial forefoot ratio"),
                _metric_line(metrics, "LateralRatio_mean", "lateral forefoot ratio"),
                _metric_line(metrics, "CoP_ML_mean", "CoP_ML proxy"),
                pitch_evidence if standing_calibration else "Pitch direction not used without standing calibration",
                f"Landing sole-ground angle={_fmt(landing_angle, 2)}" if standing_calibration else "Landing angle requires standing calibration",
            ],
            "The current trial does not show a strong StepWise-supported posture-risk pattern.",
            "Repeat if visible posture is abnormal or symptoms exist.",
            "Normal-like screening does not rule out clinical gait or foot-posture issues.",
        )

    priority = {
        "Possible eversion / over-pronation related tendency": 0,
        "Possible inversion / over-supination related tendency": 0,
        "Mixed inversion/eversion evidence": 0,
        "Forefoot-first landing tendency": 1,
        "Rearfoot-heavy landing tendency": 1,
        "Medial loading bias; eversion not confirmed": 2,
        "Lateral loading bias; inversion not confirmed": 2,
        "Reduced late-stance forefoot push-off warning": 2,
        "Reduced heel-to-forefoot pressure transfer": 2,
        "High stride timing variability": 3,
        "No clear posture risk detected": 9,
    }
    return tuple(sorted(cards, key=lambda card: priority.get(card.title, 5)))
