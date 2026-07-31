from __future__ import annotations

import html
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path("stepwise_batch_reports")


@dataclass(frozen=True)
class TrialSpec:
    folder: str
    label: str
    expected: str


TRIALS = [
    TrialSpec("normal", "Normal walking", "Normal-like baseline"),
    TrialSpec("toe_in", "Toe-in walking", "Possible toe-in related loading pattern"),
    TrialSpec("toe_out", "Toe-out walking", "Possible toe-out related loading pattern"),
    TrialSpec("inversion", "Foot inversion trial", "Lateral loading / inversion-related pattern"),
    TrialSpec("eversion", "Foot eversion trial", "Medial loading / eversion-related pattern"),
    TrialSpec("weak_pushoff", "Weak push-off trial", "Reduced late-stance forefoot push-off"),
    TrialSpec("forefoot_landing", "Forefoot landing trial", "Forefoot-first landing pattern"),
    TrialSpec("rearfoot_landing", "Rearfoot landing trial", "Rearfoot landing pattern"),
    TrialSpec("unloaded_standing", "Unloaded standing / no gait", "Low-confidence or non-walking trial"),
]


def fmt(value, digits=3):
    if value is None:
        return "-"
    try:
        if pd.isna(value):
            return "-"
    except TypeError:
        pass
    if isinstance(value, (float, np.floating)):
        return f"{value:.{digits}f}"
    return str(value)


def load_steps(folder: str) -> pd.DataFrame:
    path = ROOT / folder / "gait_steps_analysis.csv"
    return pd.read_csv(path) if path.exists() else pd.DataFrame()


def load_processed(folder: str) -> pd.DataFrame:
    path = ROOT / folder / "processed_gait_data.csv"
    return pd.read_csv(path) if path.exists() else pd.DataFrame()


def load_summary(folder: str) -> dict:
    path = ROOT / folder / "session_summary.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def metric_means(steps: pd.DataFrame) -> dict[str, float]:
    cols = [
        "RearRatio_mean",
        "FrontRatio_mean",
        "ArchRatio_mean",
        "MedialRatio_mean",
        "LateralRatio_mean",
        "EarlyRearRatio_mean",
        "EarlyFrontRatio_mean",
        "PushOffRatio_late_stance_mean",
        "CoP_AP_progression",
        "CoP_ML_mean",
        "StrideTime_s",
        "RollRange_deg",
        "PitchRange_deg",
        "RollMean_deg",
        "PitchMean_deg",
    ]
    out = {}
    for col in cols:
        out[col] = float(steps[col].mean(skipna=True)) if col in steps and not steps.empty else np.nan
    if "StrideTime_s" in steps and not steps.empty:
        sv = steps["StrideTime_s"].dropna()
        out["StrideCV"] = float(sv.std(ddof=0) / sv.mean()) if len(sv) >= 3 and sv.mean() > 0 else np.nan
    if not steps.empty and "RollRange_deg" in steps:
        out["RollRange_deg"] = float(steps["RollRange_deg"].mean(skipna=True))
    if not steps.empty and "PitchRange_deg" in steps:
        out["PitchRange_deg"] = float(steps["PitchRange_deg"].mean(skipna=True))
    return out


def add_imu_metrics(out: dict[str, float], processed: pd.DataFrame) -> dict[str, float]:
    if processed.empty:
        return out
    if "FootContact" in processed:
        contact = processed[processed["FootContact"].astype(str).str.lower().isin(["true", "1"])]
        if contact.empty:
            contact = processed
    else:
        contact = processed
    for col in ["Roll", "Pitch", "Yaw", "AccMag", "GyrMag"]:
        if col in contact and not contact.empty:
            out[f"{col}_stance_mean"] = float(contact[col].mean(skipna=True))
            out[f"{col}_stance_std"] = float(contact[col].std(skipna=True))
            out[f"{col}_stance_min"] = float(contact[col].min(skipna=True))
            out[f"{col}_stance_max"] = float(contact[col].max(skipna=True))
    return out


def support_assessment(folder: str, m: dict[str, float], base: dict[str, float]) -> tuple[str, str, str]:
    delta = {k: m.get(k, np.nan) - base.get(k, np.nan) for k in set(m) | set(base)}
    if folder == "normal":
        return (
            "Supported",
            "This trial is used as the normal baseline for relative comparison.",
            "Use several normal trials in the final study so the baseline is not based on one recording.",
        )
    if folder == "toe_in":
        if delta["LateralRatio_mean"] >= 0.05 or delta["CoP_ML_mean"] <= -0.04:
            return (
                "Supported",
                f"Lateral loading increased by {delta['LateralRatio_mean']:.3f} or CoP shifted laterally by {-delta['CoP_ML_mean']:.3f} compared with normal.",
                "Report as a possible toe-in related loading pattern, not a direct toe-in diagnosis.",
            )
        return (
            "Not strongly supported",
            f"Lateral loading changed by only {delta['LateralRatio_mean']:.3f}; CoP_ML changed by {delta['CoP_ML_mean']:.3f} compared with normal.",
            "Pressure data alone does not support a toe-in tendency in this trial. Confirm with top-view video or foot progression angle.",
        )
    if folder == "toe_out":
        if delta["MedialRatio_mean"] >= 0.03 or delta["CoP_ML_mean"] >= 0.03:
            return (
                "Supported",
                f"Medial loading changed by {delta['MedialRatio_mean']:.3f}; CoP_ML shifted medially by {delta['CoP_ML_mean']:.3f} compared with normal.",
                "Report as a possible toe-out related loading pattern, not a direct toe-out diagnosis.",
            )
        return (
            "Not strongly supported",
            f"Medial loading changed by {delta['MedialRatio_mean']:.3f}; CoP_ML changed by {delta['CoP_ML_mean']:.3f} compared with normal.",
            "Confirm with top-view video or foot progression angle.",
        )
    if folder == "inversion":
        roll_delta = delta.get("Roll_stance_mean", np.nan)
        roll_range_delta = delta.get("RollRange_deg", np.nan)
        if (not pd.isna(roll_delta) and roll_delta <= -5.0) or delta["LateralRatio_mean"] >= 0.05 or delta["CoP_ML_mean"] <= -0.04:
            return (
                "Supported",
                f"Stance Roll changed by {roll_delta:.2f} deg; Roll range changed by {roll_range_delta:.2f} deg; lateral loading delta = {delta['LateralRatio_mean']:.3f}.",
                "Report as possible inversion-related frontal-plane foot posture tendency, not a clinical diagnosis.",
            )
        return (
            "Not strongly supported",
            f"Stance Roll changed by {roll_delta:.2f} deg; lateral loading changed by {delta['LateralRatio_mean']:.3f}; CoP_ML changed by {delta['CoP_ML_mean']:.3f}.",
            "The IMU/pressure data do not strongly support inversion in this trial.",
        )
    if folder == "eversion":
        roll_delta = delta.get("Roll_stance_mean", np.nan)
        if (not pd.isna(roll_delta) and roll_delta >= 5.0) or delta["MedialRatio_mean"] >= 0.05 or delta["ArchRatio_mean"] >= 0.05 or delta["CoP_ML_mean"] >= 0.04:
            return (
                "Supported",
                f"Stance Roll changed by {roll_delta:.2f} deg; medial loading delta = {delta['MedialRatio_mean']:.3f}; CoP_ML delta = {delta['CoP_ML_mean']:.3f}.",
                "Report as possible eversion-related frontal-plane foot posture tendency, not a clinical diagnosis.",
            )
        return (
            "Not strongly supported",
            f"Stance Roll changed by {roll_delta:.2f} deg; medial loading changed by {delta['MedialRatio_mean']:.3f}; CoP_ML changed by {delta['CoP_ML_mean']:.3f}.",
            "The IMU/pressure data do not strongly support eversion in this trial.",
        )
    if folder == "weak_pushoff":
        if delta["PushOffRatio_late_stance_mean"] <= -0.08:
            return (
                "Supported",
                f"Late-stance forefoot push-off proxy decreased by {-delta['PushOffRatio_late_stance_mean']:.3f} compared with normal.",
                "Report as weak push-off candidate.",
            )
        return (
            "Not strongly supported",
            f"Late-stance forefoot push-off proxy changed by {delta['PushOffRatio_late_stance_mean']:.3f} compared with normal.",
            "This trial does not show reduced forefoot push-off in the pressure data.",
        )
    if folder == "forefoot_landing":
        if m["EarlyFrontRatio_mean"] >= 0.65 or delta["EarlyFrontRatio_mean"] >= 0.20:
            return (
                "Supported",
                f"Early forefoot ratio = {m['EarlyFrontRatio_mean']:.3f}, compared with normal {base['EarlyFrontRatio_mean']:.3f}.",
                "Report as forefoot-first landing tendency.",
            )
        return (
            "Not strongly supported",
            f"Early forefoot ratio = {m['EarlyFrontRatio_mean']:.3f}; normal = {base['EarlyFrontRatio_mean']:.3f}.",
            "The pressure data does not show clear forefoot-first landing.",
        )
    if folder == "rearfoot_landing":
        if m["EarlyRearRatio_mean"] >= 0.75 or delta["EarlyRearRatio_mean"] >= 0.12:
            return (
                "Supported",
                f"Early rearfoot ratio = {m['EarlyRearRatio_mean']:.3f}, compared with normal {base['EarlyRearRatio_mean']:.3f}.",
                "Report as rearfoot landing tendency.",
            )
        return (
            "Partially supported",
            f"Early rearfoot ratio = {m['EarlyRearRatio_mean']:.3f}, compared with normal {base['EarlyRearRatio_mean']:.3f}.",
            "The trial shows rearfoot contact, but it is not far from the current normal baseline.",
        )
    if folder == "unloaded_standing":
        return (
            "Supported",
            "Only one valid stance-like segment was detected, so this is correctly treated as a low-confidence/non-walking trial.",
            "Use unloaded/static files for calibration checks, not gait interpretation.",
        )
    return ("Unknown", "-", "-")


def risk_flags_en(steps: pd.DataFrame, summary: dict) -> list[dict[str, str]]:
    quality = str(summary.get("data_quality", "Low"))
    flags = []
    def add(module, level, title, evidence, message, action, basis):
        flags.append({
            "module": module, "level": level, "title": title,
            "evidence": evidence, "message": message, "action": action, "basis": basis,
        })
    if quality == "High":
        add("1. Data quality", "Low", "Data confidence risk", f"Quality={quality}; sample rate={fmt(summary.get('estimated_sample_rate_hz'))} Hz; detected stance phases={summary.get('detected_steps_single_foot')}.", "The recording is suitable for engineering gait screening.", "Continue collecting more normal and abnormal trials for baseline calibration.", "Wearable gait analysis depends on stable sampling, event timing, and sufficient gait cycles.")
    elif quality == "Medium":
        add("1. Data quality", "Medium", "Data confidence risk", f"Quality={quality}; sample rate={fmt(summary.get('estimated_sample_rate_hz'))} Hz; detected stance phases={summary.get('detected_steps_single_foot')}.", "The recording can be used for preliminary observation, but more steps would improve confidence.", "Retest with at least 10-20 valid stance phases.", "Wearable gait analysis depends on stable sampling, event timing, and sufficient gait cycles.")
    else:
        add("1. Data quality", "High", "Data confidence risk", f"Quality={quality}; sample rate={fmt(summary.get('estimated_sample_rate_hz'))} Hz; detected stance phases={summary.get('detected_steps_single_foot')}.", "This recording is not suitable for detailed gait interpretation.", "Check sensor placement and repeat the trial.", "Low-quality trials should not be interpreted as gait patterns.")
        return flags

    if steps.empty:
        return flags
    m = metric_means(steps)
    stride_cv = m.get("StrideCV", np.nan)
    rhythm_level = "High" if not pd.isna(stride_cv) and stride_cv >= 0.12 else ("Medium" if not pd.isna(stride_cv) and stride_cv >= 0.08 else "Low")
    add("2. Gait rhythm", rhythm_level, "Stride timing variability", f"Stride-time CV={fmt(stride_cv)}.", "This estimates whether repeated same-foot gait cycles are rhythmically consistent.", "If Medium/High, retest with a straight path and natural speed.", "Stride time, stance time, cadence, and variability are common spatiotemporal gait parameters.")
    early_front = float((steps["EarlyFrontRatio_mean"] >= 0.65).mean()) if "EarlyFrontRatio_mean" in steps else 0
    early_rear = float((steps["EarlyRearRatio_mean"] >= 0.45).mean()) if "EarlyRearRatio_mean" in steps else 0
    landing_level = "Medium" if early_front >= 0.50 or early_rear < 0.30 else "Low"
    landing_title = "Forefoot-first or reduced heel-contact tendency" if landing_level != "Low" else "Landing pattern risk is low"
    add("3. Landing pattern", landing_level, landing_title, f"EarlyFrontRatio>=0.65 in {early_front:.0%} of steps; EarlyRearRatio>=0.45 in {early_rear:.0%} of steps.", "Early stance pressure distribution is used as a landing-pattern proxy.", "Confirm with side-view video if the flag is Medium/High.", "FSR insole studies use heel/metatarsal/toe regions to infer heel strike, full contact, heel off, and toe off.")
    weak_push_rate = float((steps["PushOffRatio_late_stance_mean"] < 0.30).mean()) if "PushOffRatio_late_stance_mean" in steps else 0
    mean_cop_prog = m.get("CoP_AP_progression", np.nan)
    transfer_level = "Medium" if weak_push_rate >= 0.50 or (not pd.isna(mean_cop_prog) and mean_cop_prog < 0.15) else "Low"
    transfer_title = "Reduced pressure transfer / push-off tendency" if transfer_level != "Low" else "Pressure transfer risk is low"
    add("4. Plantar pressure transfer", transfer_level, transfer_title, f"Mean CoP_AP progression={fmt(mean_cop_prog)}; weak push-off proxy in {weak_push_rate:.0%} of steps.", "This checks whether pressure progresses from rearfoot toward forefoot during stance.", "If flagged, repeat with more steps and inspect forefoot sensors.", "Pedobarography uses CoP progression, pressure-time integral, contact regions, and force-time features.")
    rear_rate = float((steps["RearRatio_mean"] >= 0.70).mean())
    front_rate = float((steps["FrontRatio_mean"] >= 0.70).mean())
    arch_rate = float((steps["ArchRatio_mean"] >= 0.25).mean())
    medial_rate = float((steps["MedialRatio_mean"] >= 0.65).mean())
    lateral_rate = float((steps["LateralRatio_mean"] >= 0.65).mean())
    flags_count = sum(x >= 0.50 for x in [rear_rate, front_rate, arch_rate, medial_rate, lateral_rate])
    dist_level = "High" if flags_count >= 2 else ("Medium" if flags_count == 1 else "Low")
    add("5. Regional loading distribution", dist_level, "Regional loading distribution flag" if dist_level != "Low" else "Regional loading risk is low", f"rear={rear_rate:.0%}, front={front_rate:.0%}, arch={arch_rate:.0%}, medial={medial_rate:.0%}, lateral={lateral_rate:.0%} crossed engineering thresholds.", "This checks whether pressure is concentrated in one region.", "If repeated, compare with a larger normal baseline and inspect shoe/insole placement.", "Plantar-pressure studies analyze heel, midfoot, metatarsal, and toe regions using peak pressure, contact time, impulse, and distribution.")
    retest_level = "Low" if all(f["level"] == "Low" for f in flags) else "Medium"
    add("6. Retest and follow-up", retest_level, "Retest guidance", f"Overall derived from quality={quality}, rhythm={rhythm_level}, landing={landing_level}, transfer={transfer_level}, distribution={dist_level}.", "This system is a screening tool, not a diagnosis.", "Collect at least three trials per condition and add video/FPA if toe-in or toe-out is important.", "Foot progression angle literature shows toe-in/toe-out requires angle confirmation; pressure only suggests related loading.")
    return flags


def english_finding_cards(folder: str, status: str, evidence: str, note: str, m: dict[str, float], base: dict[str, float]) -> list[dict[str, str]]:
    cards = [{
        "title": "Expected label evidence check",
        "severity": status,
        "evidence": evidence,
        "interpretation": note,
        "suggestion": "Use this as a labelled demo check. For final claims, compare against multiple normal trials and verify with video or foot progression angle when relevant.",
        "limitation": "This is an engineering screening result, not a clinical diagnosis.",
    }]
    # Add a few interpretable distribution notes.
    dominant = max(
        [
            ("rearfoot", m.get("RearRatio_mean", np.nan)),
            ("forefoot", m.get("FrontRatio_mean", np.nan)),
            ("medial forefoot", m.get("MedialRatio_mean", np.nan)),
            ("lateral forefoot", m.get("LateralRatio_mean", np.nan)),
        ],
        key=lambda item: -1 if pd.isna(item[1]) else item[1],
    )
    cards.append({
        "title": "Dominant loading region",
        "severity": "Information",
        "evidence": f"The highest mean regional ratio is {dominant[0]} ({dominant[1]:.3f}).",
        "interpretation": "This summarizes where load was concentrated in this recording.",
        "suggestion": "Use this together with the pressure curves rather than as a standalone diagnosis.",
        "limitation": "Four pressure points are a low-resolution proxy for plantar pressure.",
    })
    return cards


def posture_risk_cards(m: dict[str, float], base: dict[str, float], summary: dict) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    quality = str(summary.get("data_quality", "Low"))
    stance_count = int(summary.get("detected_steps_single_foot") or 0)

    def delta(key: str) -> float:
        return m.get(key, np.nan) - base.get(key, np.nan)

    def add(title: str, level: str, evidence: str, interpretation: str, action: str, limitation: str) -> None:
        cards.append(
            {
                "title": title,
                "level": level,
                "evidence": evidence,
                "interpretation": interpretation,
                "action": action,
                "limitation": limitation,
            }
        )

    if quality == "Low" or stance_count < 4:
        add(
            "Walking posture cannot be inferred from this file",
            "High",
            f"Data quality = {quality}; valid stance phases = {stance_count}.",
            "The file does not contain enough valid walking cycles for posture-risk interpretation.",
            "Use this recording for calibration or connection checking only. Re-test with at least 10-20 valid stance phases.",
            "Posture risks should not be inferred from non-walking or very short recordings.",
        )
        return cards

    # Landing risks.
    if m.get("EarlyFrontRatio_mean", np.nan) >= 0.65 or delta("EarlyFrontRatio_mean") >= 0.20:
        add(
            "Forefoot-first landing tendency",
            "Medium",
            f"Early forefoot ratio = {m['EarlyFrontRatio_mean']:.3f}; normal baseline = {base['EarlyFrontRatio_mean']:.3f}.",
            "The forefoot appears to load early in stance.",
            "Confirm with side-view video and repeat with at least 10-20 valid stance phases.",
            "This is a landing-pattern screening flag, not a diagnosis.",
        )

    if m.get("EarlyRearRatio_mean", np.nan) >= 0.78 or delta("EarlyRearRatio_mean") >= 0.14:
        add(
            "Rearfoot-heavy landing tendency",
            "Medium",
            f"Early rearfoot ratio = {m['EarlyRearRatio_mean']:.3f}; normal baseline = {base['EarlyRearRatio_mean']:.3f}.",
            "The heel appears to dominate the early stance phase.",
            "Check heel sensor placement and compare with side-view video.",
            "Heel loading alone does not indicate heel pathology.",
        )

    # Medial/lateral loading and toe-in/toe-out related loading.
    if delta("LateralRatio_mean") >= 0.05 or delta("CoP_ML_mean") <= -0.04:
        add(
            "Lateral loading bias / possible toe-in-related loading",
            "Medium",
            f"Lateral ratio delta = {delta('LateralRatio_mean'):.3f}; CoP_ML delta = {delta('CoP_ML_mean'):.3f}.",
            "Compared with the normal baseline, pressure shifts toward the lateral forefoot.",
            "Use top-view video or foot progression angle measurement to confirm toe-in. Use this report only as a loading-pattern warning.",
            "Pressure distribution cannot directly prove toe-in or foot inversion.",
        )

    if delta("MedialRatio_mean") >= 0.03 or delta("CoP_ML_mean") >= 0.03:
        add(
            "Medial loading bias / possible toe-out-related loading",
            "Medium",
            f"Medial ratio delta = {delta('MedialRatio_mean'):.3f}; CoP_ML delta = {delta('CoP_ML_mean'):.3f}.",
            "Compared with the normal baseline, pressure shifts toward the medial forefoot.",
            "Use top-view video or foot progression angle measurement to confirm toe-out. Use this report only as a loading-pattern warning.",
            "Pressure distribution cannot directly prove toe-out or foot eversion.",
        )

    if delta("ArchRatio_mean") >= 0.05 or m.get("ArchRatio_mean", np.nan) >= 0.25:
        add(
            "Midfoot / arch loading warning",
            "Medium",
            f"Arch ratio = {m['ArchRatio_mean']:.3f}; normal baseline = {base['ArchRatio_mean']:.3f}.",
            "The arch/midfoot sensor carries a relatively high share of pressure.",
            "Confirm P2 placement and compare with more normal baseline trials.",
            "This does not diagnose flat foot, arch collapse, pronation, or eversion.",
        )

    roll_delta = delta("Roll_stance_mean")
    if not pd.isna(roll_delta) and roll_delta <= -5.0:
        add(
            "Possible foot inversion-related posture risk",
            "Medium",
            f"Stance Roll changed by {roll_delta:.2f} deg compared with normal; mean stance Roll = {m['Roll_stance_mean']:.2f} deg, normal = {base['Roll_stance_mean']:.2f} deg.",
            "The foot-mounted IMU suggests a frontal-plane posture shift consistent with an inversion-related tendency.",
            "Confirm IMU attachment, repeat the trial, and compare with video or a simple frontal-view foot posture check.",
            "Roll direction depends on sensor mounting. This is an IMU-based posture screening flag, not a clinical diagnosis.",
        )

    if not pd.isna(roll_delta) and roll_delta >= 5.0:
        add(
            "Possible foot eversion-related posture risk",
            "Medium",
            f"Stance Roll changed by +{roll_delta:.2f} deg compared with normal; mean stance Roll = {m['Roll_stance_mean']:.2f} deg, normal = {base['Roll_stance_mean']:.2f} deg.",
            "The foot-mounted IMU suggests a frontal-plane posture shift consistent with an eversion-related tendency.",
            "Confirm IMU attachment, repeat the trial, and compare with video or a simple frontal-view foot posture check.",
            "Roll direction depends on sensor mounting. This is an IMU-based posture screening flag, not a clinical diagnosis.",
        )

    # Pressure transfer and push-off.
    if delta("PushOffRatio_late_stance_mean") <= -0.08 or m.get("PushOffRatio_late_stance_mean", np.nan) < 0.75:
        add(
            "Reduced late-stance forefoot push-off warning",
            "Medium",
            f"Late push-off proxy = {m['PushOffRatio_late_stance_mean']:.3f}; normal baseline = {base['PushOffRatio_late_stance_mean']:.3f}.",
            "Forefoot loading near the end of stance is lower than expected.",
            "Repeat the test and confirm P3/P4 placement. If repeated, basic calf raise and toe-grip training can be suggested as rehabilitation support.",
            "This does not diagnose calf weakness or neurological impairment.",
        )

    if m.get("CoP_AP_progression", np.nan) < 0.15:
        add(
            "Reduced heel-to-forefoot pressure transfer",
            "Medium",
            f"CoP_AP progression = {m['CoP_AP_progression']:.3f}; normal baseline = {base['CoP_AP_progression']:.3f}.",
            "The coarse pressure center did not move strongly from rearfoot toward forefoot.",
            "Inspect pressure curves and repeat the test with natural walking speed.",
            "This is a coarse four-sensor CoP proxy, not a pressure-plate measurement.",
        )

    # Rhythm risk, kept as a posture/function risk rather than data quality.
    if stance_count >= 10 and not pd.isna(m.get("StrideCV", np.nan)) and m["StrideCV"] >= 0.12:
        add(
            "High stride timing variability",
            "Medium",
            f"Stride-time CV = {m['StrideCV']:.3f}.",
            "Same-foot stride timing varies noticeably across the trial.",
            "Repeat with a straight path and more steps. Metronome-paced walking can be used for rhythm-training demonstration.",
            "With only one foot and a small number of steps, this is a screening flag only.",
        )

    if not cards:
        add(
            "No clear posture risk detected",
            "Low",
            "No posture-risk rule was triggered relative to the current normal baseline.",
            "The current pressure pattern appears normal-like under the engineering screening rules.",
            "Collect more normal baseline trials and repeat if symptoms or visible gait issues remain.",
            "Normal-like screening does not rule out clinical gait issues.",
        )

    priority = {
        "Possible foot inversion-related posture risk": 0,
        "Possible foot eversion-related posture risk": 0,
        "Forefoot-first landing tendency": 1,
        "Rearfoot-heavy landing tendency": 1,
        "Reduced late-stance forefoot push-off warning": 2,
        "Reduced heel-to-forefoot pressure transfer": 2,
        "Medial loading bias / possible toe-out-related loading": 3,
        "Lateral loading bias / possible toe-in-related loading": 3,
        "Midfoot / arch loading warning": 4,
        "High stride timing variability": 5,
        "No clear posture risk detected": 9,
        "Walking posture cannot be inferred from this file": -1,
    }
    cards.sort(key=lambda card: priority.get(card["title"], 8))
    return cards


def write_html(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def user_friendly_copy(title: str) -> tuple[str, str, str]:
    copy = {
        "No clear posture risk detected": (
            "No obvious gait-risk pattern detected",
            "Your pressure and foot-angle pattern looked generally balanced in this short walking test.",
            "Keep using this as a baseline. If you feel pain or instability, repeat the test and compare with a professional assessment.",
        ),
        "Walking posture cannot be inferred from this file": (
            "Not enough walking data to judge posture",
            "This file does not contain enough valid walking steps for a meaningful posture result.",
            "Please repeat the walking test with at least 10-20 clear steps.",
        ),
        "Possible foot inversion-related posture risk": (
            "Possible foot-inversion tendency",
            "Your foot-angle sensor showed the foot tilting inward more than your normal baseline during stance.",
            "Repeat the test and check the IMU attachment. If this appears repeatedly, consider ankle-stability and balance exercises or a professional gait check.",
        ),
        "Possible foot eversion-related posture risk": (
            "Possible foot-eversion tendency",
            "Your foot-angle sensor showed the foot tilting outward more than your normal baseline during stance.",
            "Repeat the test and check the IMU attachment. If this appears repeatedly, consider foot-arch control and balance exercises or a professional gait check.",
        ),
        "Medial loading bias / possible toe-out-related loading": (
            "Possible outward-foot / medial-loading tendency",
            "More pressure shifted toward the big-toe side of the forefoot compared with your normal baseline.",
            "Repeat the test with a top-view video. If repeated, check shoe wear and consider foot alignment or arch-control training.",
        ),
        "Lateral loading bias / possible toe-in-related loading": (
            "Possible inward-foot / lateral-loading tendency",
            "More pressure shifted toward the little-toe side of the forefoot compared with your normal baseline.",
            "Repeat the test with a top-view video. If repeated, check shoe wear and consider ankle-stability or balance training.",
        ),
        "Forefoot-first landing tendency": (
            "Forefoot-first landing tendency",
            "Your forefoot loaded early in the step, before the heel became the main contact area.",
            "Repeat with a side-view video. If this pattern is repeated unintentionally, consider calf relaxation and controlled heel-to-toe walking practice.",
        ),
        "Rearfoot-heavy landing tendency": (
            "Heavy heel-landing tendency",
            "Your heel carried a large share of pressure at the beginning of the step.",
            "Repeat with a side-view video and confirm the heel sensor position. If repeated, consider ankle mobility and softer heel-to-toe walking practice.",
        ),
        "Reduced late-stance forefoot push-off warning": (
            "Reduced push-off tendency",
            "Your forefoot contributed less than expected near the end of stance.",
            "Repeat the test and confirm the forefoot sensors. If repeated, calf raises, toe-grip exercises, and ankle plantarflexion training may help as general exercise support.",
        ),
        "Reduced heel-to-forefoot pressure transfer": (
            "Reduced heel-to-forefoot pressure transfer",
            "The pressure center did not move forward strongly from heel to forefoot during the step.",
            "Repeat at a natural speed and inspect the pressure curve. If repeated, practice slow heel-to-toe walking and basic push-off exercises.",
        ),
        "Midfoot / arch loading warning": (
            "Higher arch/midfoot loading",
            "The arch sensor carried more pressure than expected compared with the baseline.",
            "Confirm the P2 sensor position. If repeated, compare with more baseline trials and consider a professional foot posture check.",
        ),
        "High stride timing variability": (
            "Irregular step timing",
            "The time between repeated steps varied more than expected in this short trial.",
            "Repeat on a straight path at a natural speed. A metronome-paced walk can be used for rhythm practice.",
        ),
    }
    return copy.get(
        title,
        (
            title,
            "This pattern was detected from pressure and foot-angle features.",
            "Repeat the test and compare with your baseline.",
        ),
    )


def user_report_overall(posture_cards: list[dict[str, str]]) -> tuple[str, str, str]:
    meaningful = [card for card in posture_cards if card["title"] != "No clear posture risk detected"]
    if not meaningful:
        return (
            "No obvious gait-risk pattern detected",
            "Your current walking trial looks generally balanced under the StepWise screening rules.",
            "Low",
        )
    if meaningful[0]["title"] == "Walking posture cannot be inferred from this file":
        return (
            "Not enough walking data to judge posture",
            "Please repeat the test with more valid walking steps.",
            "High",
        )
    main_title, main_msg, _ = user_friendly_copy(meaningful[0]["title"])
    return (main_title, main_msg, meaningful[0]["level"])


def report_css() -> str:
    return """
    body{margin:0;font-family:Arial,Helvetica,sans-serif;background:#f7f9fc;color:#17202a}
    header{background:#17324d;color:white;padding:30px 42px}
    header h1{margin:0 0 8px;font-size:30px} header p{margin:0;color:#d9e9f5}
    main{max-width:1180px;margin:0 auto;padding:28px}
    section{background:white;border:1px solid #d7e0ea;border-radius:10px;padding:22px;margin-bottom:22px}
    h2{margin:0 0 16px;color:#17324d} h3{margin:0 0 10px;color:#0f2f48}
    p,li{line-height:1.6}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
    .hero{display:flex;justify-content:space-between;align-items:center;gap:20px;border-left:8px solid #2f80ed}
    .hero h2{font-size:28px;margin:0 0 8px}.hero p{margin:0;color:#52616f;font-size:16px}
    .metric{border:1px solid #d7e0ea;border-radius:8px;padding:14px;background:#fbfdff}.metric span{display:block;color:#64748b;font-size:13px;margin-bottom:8px}.metric strong{font-size:22px;color:#0f2f48}
    .card{border:1px solid #d7e0ea;border-radius:8px;padding:16px;margin-bottom:14px;background:#fbfdff}.head{display:flex;justify-content:space-between;gap:12px;align-items:center}.pill{border-radius:999px;padding:5px 11px;font-weight:700;white-space:nowrap;background:#dbeafe;color:#17324d}
    .low{background:#dcfce7;color:#166534}.medium{background:#fef3c7;color:#92400e}.high{background:#fee2e2;color:#991b1b}.supported{background:#dcfce7;color:#166534}.not{background:#fee2e2;color:#991b1b}.partial{background:#fef3c7;color:#92400e}
    .plain{font-size:16px;color:#334155}
    table{width:100%;border-collapse:collapse;margin:14px 0;font-size:14px}th,td{border:1px solid #d7e0ea;padding:10px;text-align:left;vertical-align:top}th{background:#eef3f8;color:#17324d}
    img{width:100%;border:1px solid #d7e0ea;border-radius:8px;margin-bottom:16px;background:white}.note{color:#64748b;font-size:13px}
    """


def badge_class(text: str) -> str:
    t = text.lower()
    if "not" in t:
        return "not"
    if "partial" in t:
        return "partial"
    if "support" in t:
        return "supported"
    if t == "high":
        return "high"
    if t == "medium":
        return "medium"
    return "low"


def make_user_report(
    spec: TrialSpec,
    summary: dict,
    steps: pd.DataFrame,
    cards: list[dict[str, str]],
    posture_cards: list[dict[str, str]],
    flags: list[dict[str, str]],
    m: dict[str, float],
) -> str:
    overall_title, overall_msg, overall_level = user_report_overall(posture_cards)
    posture_html = ""
    for c in posture_cards:
        friendly_title, friendly_msg, friendly_action = user_friendly_copy(c["title"])
        posture_html += f"""
        <article class='card'>
          <div class='head'><h3>{html.escape(friendly_title)}</h3><span class='pill {c['level'].lower()}'>{html.escape(c['level'])}</span></div>
          <p class='plain'>{html.escape(friendly_msg)}</p>
          <p><strong>Why StepWise says this:</strong> {html.escape(c['evidence'])}</p>
          <p><strong>What to do next:</strong> {html.escape(friendly_action)}</p>
        </article>
        """
    label_status = cards[0]["severity"] if cards else "-"
    label_evidence = cards[0]["evidence"] if cards else "-"
    confidence = str(summary.get("data_quality", "-"))
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'><title>StepWise User Gait Screening Report</title><style>{report_css()}</style></head><body>
    <header><h1>StepWise Gait Screening Report</h1><p>Clear posture-risk feedback from one smart-insole walking test.</p></header><main>
    <section class='hero'><div><h2>{html.escape(overall_title)}</h2><p>{html.escape(overall_msg)}</p></div><span class='pill {overall_level.lower()}'>{html.escape(overall_level)} risk</span></section>
    <section><h2>Your posture-risk feedback</h2>{posture_html}</section>
    <section><h2>Test summary</h2><div class='grid'>
    <div class='metric'><span>Confidence</span><strong>{html.escape(confidence)}</strong></div>
    <div class='metric'><span>Valid stance phases</span><strong>{summary.get('detected_steps_single_foot','-')}</strong></div>
    <div class='metric'><span>Duration</span><strong>{fmt(summary.get('duration_s'))} s</strong></div>
    <div class='metric'><span>Sample rate</span><strong>{fmt(summary.get('estimated_sample_rate_hz'))} Hz</strong></div>
    <div class='metric'><span>Mean stance time</span><strong>{fmt(summary.get('mean_stance_time_s'))} s</strong></div>
    <div class='metric'><span>Mean stride time</span><strong>{fmt(summary.get('mean_stride_time_s'))} s</strong></div>
    </div></section>
    <section><h2>Demo-label check</h2><p>This dataset was labelled as <strong>{html.escape(spec.label)}</strong>. Evidence check: <strong>{html.escape(label_status)}</strong>.</p><p class='note'>{html.escape(label_evidence)}</p></section>
    <section><h2>Regional loading summary</h2><div class='grid'>
    <div class='metric'><span>Rearfoot ratio</span><strong>{fmt(m.get('RearRatio_mean'))}</strong></div>
    <div class='metric'><span>Forefoot ratio</span><strong>{fmt(m.get('FrontRatio_mean'))}</strong></div>
    <div class='metric'><span>Medial forefoot ratio</span><strong>{fmt(m.get('MedialRatio_mean'))}</strong></div>
    <div class='metric'><span>Lateral forefoot ratio</span><strong>{fmt(m.get('LateralRatio_mean'))}</strong></div>
    <div class='metric'><span>Early forefoot ratio</span><strong>{fmt(m.get('EarlyFrontRatio_mean'))}</strong></div>
    <div class='metric'><span>Late push-off proxy</span><strong>{fmt(m.get('PushOffRatio_late_stance_mean'))}</strong></div>
    </div></section>
    <section><h2>Charts</h2><img src='pressure_stance.png' alt='Pressure and stance'><img src='orientation.png' alt='Orientation'></section>
    <section><h2>Medical note</h2><p class='note'>This report is a screening aid, not a medical diagnosis. If pain, instability, or repeated abnormal results occur, consult a qualified professional.</p></section>
    </main></body></html>"""


def make_technical_report(spec: TrialSpec, summary: dict, steps: pd.DataFrame, cards: list[dict[str, str]], flags: list[dict[str, str]], m: dict[str, float], base: dict[str, float]) -> str:
    step_cols = ["Step","StartTime_s","EndTime_s","StanceTime_s","StrideTime_s","PeakPressure_N","RearRatio_mean","FrontRatio_mean","MedialRatio_mean","LateralRatio_mean","PushOffRatio_late_stance_mean","CoP_AP_progression","CoP_ML_mean","Pattern"]
    table = steps[[c for c in step_cols if c in steps.columns]].to_html(index=False, classes="steps", float_format=lambda x: f"{x:.3f}") if not steps.empty else "<p>No valid stance phases.</p>"
    cards_html = "".join(f"<li><strong>{html.escape(c['title'])} [{html.escape(c['severity'])}]</strong>: {html.escape(c['evidence'])}</li>" for c in cards)
    risk_table = pd.DataFrame(flags).to_html(index=False, escape=True) if flags else "<p>No flags.</p>"
    baseline_rows = "".join(f"<tr><td>{html.escape(k)}</td><td>{fmt(m.get(k))}</td><td>{fmt(base.get(k))}</td><td>{fmt(m.get(k, np.nan)-base.get(k, np.nan))}</td></tr>" for k in ["RearRatio_mean","FrontRatio_mean","MedialRatio_mean","LateralRatio_mean","EarlyFrontRatio_mean","EarlyRearRatio_mean","PushOffRatio_late_stance_mean","CoP_AP_progression","CoP_ML_mean","Roll_stance_mean","RollRange_deg","Pitch_stance_mean","StrideCV"])
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'><title>StepWise Technical Report</title><style>{report_css()}</style></head><body><header><h1>StepWise Technical Report</h1><p>Trial label: {html.escape(spec.label)}</p></header><main>
    <section><h2>Summary</h2><p>Quality={summary.get('data_quality')}; stance phases={summary.get('detected_steps_single_foot')}; sample rate={fmt(summary.get('estimated_sample_rate_hz'))} Hz.</p></section>
    <section><h2>Label evidence check</h2><ul>{cards_html}</ul></section>
    <section><h2>Comparison with normal baseline</h2><table><tr><th>Metric</th><th>This trial</th><th>Normal baseline</th><th>Delta</th></tr>{baseline_rows}</table></section>
    <section><h2>Risk flags</h2>{risk_table}</section>
    <section><h2>Step-level features</h2>{table}</section>
    <section><h2>Charts</h2><img src='pressure_stance.png'><img src='acceleration.png'><img src='orientation.png'></section>
    <section><h2>Boundary</h2><p class='note'>Four pressure points provide low-resolution plantar loading proxies. They support screening indicators but not clinical diagnosis.</p></section>
    </main></body></html>"""


def make_data_guide() -> str:
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'><title>StepWise Data Analysis Guide</title><style>{report_css()}</style></head><body><header><h1>StepWise Data Analysis Guide</h1><p>Formulas, sensor mapping, decision rules, and literature basis</p></header><main>
    <section><h2>Scope</h2><p>StepWise generates gait screening indicators, not clinical diagnoses. It can flag loading patterns, rhythm variability, landing tendencies, pressure-transfer issues, and toe-in/toe-out related loading candidates.</p></section>
    <section><h2>Sensor mapping</h2><table><tr><th>Channel</th><th>Placement</th><th>Variable</th><th>Supported interpretation</th></tr>
    <tr><td>P1</td><td>Heel</td><td>Rearfoot pressure</td><td>Heel contact, rearfoot loading</td></tr>
    <tr><td>P2</td><td>Arch / midfoot</td><td>Arch pressure</td><td>Midfoot/arch loading proxy</td></tr>
    <tr><td>P3</td><td>Big-toe root / first metatarsal</td><td>Medial forefoot pressure</td><td>Medial loading, toe-out related loading candidate</td></tr>
    <tr><td>P4</td><td>Little-toe root / fifth metatarsal</td><td>Lateral forefoot pressure</td><td>Lateral loading, toe-in related loading candidate</td></tr></table></section>
    <section><h2>Core formulas</h2><table><tr><th>Metric</th><th>Formula</th><th>Decision use</th><th>Basis</th></tr>
    <tr><td>Sample rate</td><td><code>fs=(N-1)/(t_last-t_first)</code></td><td>Data quality</td><td>Wearable gait event timing requires stable sampling.</td></tr>
    <tr><td>Total pressure</td><td><code>P_total=P1+P2+P3+P4</code></td><td>Stance detection</td><td>FSR insole studies use pressure thresholds for contact/step detection.</td></tr>
    <tr><td>Stance time</td><td><code>t_end_contact-t_start_contact</code></td><td>Temporal gait feature</td><td>Stance/swing/stride/cadence are standard spatiotemporal gait parameters.</td></tr>
    <tr><td>Rearfoot ratio</td><td><code>P1/P_total</code></td><td>Rearfoot-heavy loading</td><td>Heel sensors are used for heel contact and stance detection.</td></tr>
    <tr><td>Forefoot ratio</td><td><code>(P3+P4)/P_total</code></td><td>Forefoot-heavy loading and push-off proxy</td><td>Metatarsal regions are common plantar-pressure regions.</td></tr>
    <tr><td>Medial ratio</td><td><code>P3/P_total</code></td><td>Medial loading / toe-out related pattern</td><td>In-toeing/out-toeing studies show plantar loading shifts with foot angle changes.</td></tr>
    <tr><td>Lateral ratio</td><td><code>P4/P_total</code></td><td>Lateral loading / toe-in related pattern</td><td>Pressure evidence is indirect and requires FPA/video confirmation.</td></tr>
    <tr><td>CoP proxy</td><td><code>CoP=sum(P_i*x_i)/sum(P_i)</code></td><td>Pressure transfer and medial/lateral bias</td><td>Pedobarography commonly uses center-of-pressure progression.</td></tr>
    <tr><td>Stride variability</td><td><code>CV=std(stride time)/mean(stride time)</code></td><td>Rhythm risk flag</td><td>Gait variability is a common temporal gait feature.</td></tr></table></section>
    <section><h2>User-facing posture risk output</h2><p>For a single unknown input file, the system should not first try to match a file name. It computes posture risk warnings from pressure evidence: forefoot-first landing, rearfoot-heavy landing, lateral/medial loading bias, possible toe-in/toe-out related loading, reduced push-off, reduced pressure transfer, and stride timing variability. Data quality is shown as confidence only.</p></section>
    <section><h2>Why expected labels may not be detected</h2><p>These labels were collected as demonstrations, but the pressure evidence must still support the label. For example, a toe-in label should show increased lateral loading or lateral CoP shift compared with normal; if it does not, the rigorous report must say that the toe-in tendency is not strongly supported by this trial. This means the report is scientifically cautious, not that it blindly failed.</p></section>
    <section><h2>References</h2><ol>
    <li><a href='https://pmc.ncbi.nlm.nih.gov/articles/PMC10939318/'>Gaitmap: an open ecosystem for IMU-based gait analysis</a>.</li>
    <li><a href='https://gaitmap.readthedocs.io/en/stable/modules/event_detection.html'>gaitmap event detection documentation</a>.</li>
    <li><a href='https://www.mdpi.com/1424-8220/19/5/984'>Instrumented insole using pressure sensors for step count</a>.</li>
    <li><a href='https://pmc.ncbi.nlm.nih.gov/articles/PMC7506746/'>Foot-ground contact phase classification with wearable sensors</a>.</li>
    <li><a href='https://www.mdpi.com/2076-3417/11/22/11020'>Pedobarography review</a>.</li>
    <li><a href='https://pmc.ncbi.nlm.nih.gov/articles/PMC7888122/'>Foot progression angle estimation using a single foot-worn inertial sensor</a>.</li>
    <li><a href='https://www.sciencedirect.com/science/article/pii/S0966636213001902'>Foot loading patterns with in-toeing/out-toeing gait modifications</a>.</li>
    </ol></section></main></body></html>"""


def make_index(rows: list[dict]) -> str:
    trs = "".join(f"<tr><td>{html.escape(r['label'])}</td><td>{html.escape(r['top_risks'])}</td><td>{html.escape(r['support'])}</td><td>{html.escape(r['quality'])}</td><td>{r['steps']}</td><td>{fmt(r['hz'],1)}</td><td><a href='{r['folder']}/user_report.html'>User HTML</a> / <a href='{r['folder']}/user_report.pdf'>PDF</a><br><a href='{r['folder']}/data_guide.html'>Guide HTML</a> / <a href='{r['folder']}/data_guide.pdf'>PDF</a><br><a href='{r['folder']}/report.html'>Technical HTML</a> / <a href='{r['folder']}/report.pdf'>PDF</a></td></tr>" for r in rows)
    return f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>StepWise Batch Report Index</title><style>{report_css()}</style></head><body><header><h1>StepWise Batch Report Index</h1><p>English reports and PDFs for all labelled trials.</p></header><main><section><table><tr><th>Trial</th><th>Detected posture risk output</th><th>Label evidence</th><th>Confidence</th><th>Stance phases</th><th>Hz</th><th>Reports</th></tr>{trs}</table></section><section><p class='note'>Detected posture risk output is what a user should see for an unknown input. Label evidence is only a development check for these labelled demo files.</p></section></main></body></html>"


def main() -> None:
    normal_steps = load_steps("normal")
    baseline = add_imu_metrics(metric_means(normal_steps), load_processed("normal"))
    summary_rows = []
    guide = make_data_guide()
    all_rows = []

    for spec in TRIALS:
        d = ROOT / spec.folder
        steps = load_steps(spec.folder)
        summary = load_summary(spec.folder)
        m = add_imu_metrics(metric_means(steps), load_processed(spec.folder))
        status, evidence, note = support_assessment(spec.folder, m, baseline)
        cards = english_finding_cards(spec.folder, status, evidence, note, m, baseline)
        posture_cards = posture_risk_cards(m, baseline, summary)
        flags = risk_flags_en(steps, summary)

        write_html(d / "user_report.html", make_user_report(spec, summary, steps, cards, posture_cards, flags, m))
        write_html(d / "report.html", make_technical_report(spec, summary, steps, cards, flags, m, baseline))
        write_html(d / "data_guide.html", guide)
        pd.DataFrame(cards).to_csv(d / "screening_findings_en.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame(posture_cards).to_csv(d / "posture_risk_output.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame(flags).to_csv(d / "risk_flags_en.csv", index=False, encoding="utf-8-sig")
        top_risks = "; ".join(card["title"] for card in posture_cards[:3])

        row = {
            "folder": spec.folder,
            "label": spec.label,
            "expected": spec.expected,
            "support": status,
            "top_risks": top_risks,
            "evidence": evidence,
            "quality": summary.get("data_quality", "-"),
            "steps": summary.get("detected_steps_single_foot", "-"),
            "hz": summary.get("estimated_sample_rate_hz", np.nan),
        }
        summary_rows.append(row)
        all_rows.append(row)

    pd.DataFrame(summary_rows).to_csv(ROOT / "label_evidence_summary.csv", index=False, encoding="utf-8-sig")
    write_html(ROOT / "index.html", make_index(all_rows))
    print(f"English reports generated under {ROOT.resolve()}")


if __name__ == "__main__":
    main()
