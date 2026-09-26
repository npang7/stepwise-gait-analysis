"""Single application service for StepWise analysis."""

from __future__ import annotations

from pathlib import Path

from .features import compute_session_metrics, extract_stance_features, summarize_session
from .models import AnalysisConfig, AnalysisResult
from .parsing import parse_stepwise_txt
from .reporting import write_analysis_artifacts
from .screening import build_risk_cards
from .signal import adaptive_threshold, build_basic_features, contact_intervals, hysteresis_contact


class AnalysisService:
    """Run the complete analysis pipeline without transport-specific concerns."""

    def analyze(
        self,
        walking_path: str | Path,
        standing_path: str | Path | None,
        output_dir: str | Path,
        config: AnalysisConfig,
    ) -> AnalysisResult:
        walking = parse_stepwise_txt(Path(walking_path))
        processed = build_basic_features(walking, config)
        enter_threshold, exit_threshold = adaptive_threshold(
            processed["TotalPressure"], config.min_threshold_n, config.threshold_ratio
        )
        contact = hysteresis_contact(processed["TotalPressure"], enter_threshold, exit_threshold)
        processed["FootContact"] = contact
        intervals = contact_intervals(contact, processed["Time_s"], config.min_stance_s)
        steps = extract_stance_features(processed, intervals)
        summary = summarize_session(processed, steps, enter_threshold, exit_threshold)
        metrics = compute_session_metrics(steps, processed)

        calibration: dict[str, float] | None = None
        if standing_path is not None:
            standing = parse_stepwise_txt(Path(standing_path))
            calibration = {
                "Roll_neutral_deg": float(standing["Roll"].mean(skipna=True)),
                "Pitch_neutral_deg": float(standing["Pitch"].mean(skipna=True)),
                "Yaw_neutral_deg": float(standing["Yaw"].mean(skipna=True)),
            }
            pitch_neutral = calibration["Pitch_neutral_deg"]
            roll_neutral = calibration["Roll_neutral_deg"]
            metrics["PitchDelta_stance_from_standing"] = (
                metrics["Pitch_stance_mean"] - pitch_neutral
            )
            metrics["LandingPitchDelta_from_standing"] = (
                metrics["LandingPitch_mean_deg"] - pitch_neutral
            )
            metrics["LandingSoleGroundAngle_deg"] = metrics[
                "LandingPitchDelta_from_standing"
            ]
            metrics["LandingSoleGroundAngle_abs_deg"] = abs(
                metrics["LandingSoleGroundAngle_deg"]
            )
            metrics["RollDelta_stance_from_standing"] = (
                metrics["Roll_stance_mean"] - roll_neutral
            )
            summary["standing_calibration"] = calibration

        cards = build_risk_cards(
            metrics,
            summary,
            pitch_eversion_sign=config.sensor_mapping.pitch_eversion_sign,
            standing_calibration=calibration,
        )
        artifacts = write_analysis_artifacts(
            Path(output_dir), processed, steps, summary, metrics, cards
        )
        return AnalysisResult(
            summary=summary,
            metrics=metrics,
            risk_cards=cards,
            artifacts=artifacts,
        )
