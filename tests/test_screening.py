from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stepwise.screening import build_risk_cards  # noqa: E402


def neutral_metrics() -> dict[str, float]:
    return {
        "MedialRatio_mean": 0.50,
        "LateralRatio_mean": 0.50,
        "ArchRatio_mean": 0.08,
        "CoP_ML_mean": 0.00,
        "PitchDelta_stance_from_standing": math.nan,
        "LandingSoleGroundAngle_deg": math.nan,
        "EarlyFrontRatio_mean": 0.50,
        "EarlyRearRatio_mean": 0.50,
        "PushOffRatio_late_stance_mean": 0.80,
        "CoP_AP_progression": 0.20,
        "StrideCV": 0.05,
    }


class ScreeningTests(unittest.TestCase):
    def test_too_few_stances_suppresses_posture_inference(self) -> None:
        cards = build_risk_cards(
            neutral_metrics(),
            {
                "data_quality": "Low",
                "detected_steps_single_foot": 2,
                "estimated_sample_rate_hz": 100.0,
            },
            pitch_eversion_sign="positive",
            standing_calibration=None,
        )
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].title, "Walking posture cannot be inferred from this file")
        self.assertEqual(cards[0].level, "High")

    def test_conflicting_pressure_and_calibrated_imu_evidence_is_explicit(self) -> None:
        metrics = neutral_metrics()
        metrics.update(
            {
                "MedialRatio_mean": 0.30,
                "LateralRatio_mean": 0.70,
                "CoP_ML_mean": -0.08,
                "PitchDelta_stance_from_standing": 7.0,
            }
        )
        cards = build_risk_cards(
            metrics,
            {"data_quality": "High", "detected_steps_single_foot": 10},
            pitch_eversion_sign="positive",
            standing_calibration={"Pitch_neutral_deg": 0.0},
        )
        self.assertEqual(cards[0].title, "Mixed inversion/eversion evidence")
        self.assertIn("do not point cleanly", cards[0].interpretation)

    def test_neutral_metrics_produce_a_low_risk_card(self) -> None:
        cards = build_risk_cards(
            neutral_metrics(),
            {"data_quality": "High", "detected_steps_single_foot": 10},
            pitch_eversion_sign="positive",
            standing_calibration=None,
        )
        self.assertEqual([card.title for card in cards], ["No clear posture risk detected"])
        self.assertEqual(cards[0].level, "Low")


if __name__ == "__main__":
    unittest.main()
