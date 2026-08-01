from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stepwise.models import (
    AnalysisConfig,
    AnalysisResult,
    Artifact,
    RiskCard,
    SensorMapping,
)


class ModelTests(unittest.TestCase):
    def test_sensor_mapping_defaults_match_the_prototype(self) -> None:
        mapping = SensorMapping()
        self.assertEqual(
            (mapping.heel, mapping.arch, mapping.medial_forefoot, mapping.lateral_forefoot),
            ("P2", "P3", "P4", "P1"),
        )
        self.assertEqual(mapping.pitch_eversion_sign, "positive")

    def test_sensor_mapping_rejects_duplicate_channels(self) -> None:
        with self.assertRaisesRegex(ValueError, "unique"):
            SensorMapping(heel="P1", arch="P1", medial_forefoot="P3", lateral_forefoot="P4")

    def test_analysis_config_rejects_invalid_numeric_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "smooth_window"):
            AnalysisConfig(smooth_window=0)
        with self.assertRaisesRegex(ValueError, "min_stance_s"):
            AnalysisConfig(min_stance_s=0)

    def test_analysis_result_serializes_nonfinite_values_as_null(self) -> None:
        result = AnalysisResult(
            summary={"samples": 20, "mean_stride_time_s": math.nan},
            metrics={"score": math.inf},
            risk_cards=(
                RiskCard(
                    title="Insufficient data",
                    level="High",
                    evidence="one stance",
                    interpretation="not enough data",
                    action="collect more data",
                    limitation="prototype",
                ),
            ),
            artifacts=(Artifact(name="result.json", media_type="application/json", size_bytes=12),),
        )
        payload = result.to_dict()
        self.assertIsNone(payload["summary"]["mean_stride_time_s"])
        self.assertIsNone(payload["metrics"]["score"])
        self.assertEqual(payload["risk_cards"][0]["title"], "Insufficient data")
        self.assertEqual(payload["artifacts"][0]["name"], "result.json")


if __name__ == "__main__":
    unittest.main()
