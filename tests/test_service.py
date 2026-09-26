from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stepwise.models import AnalysisConfig
from stepwise.service import AnalysisService

FIXTURE = ROOT / "tests" / "fixtures" / "minimal_walk.txt"


class AnalysisServiceTests(unittest.TestCase):
    def test_analysis_service_matches_the_golden_fixture_and_writes_manifested_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory) / "result"
            result = AnalysisService().analyze(FIXTURE, None, output_dir, AnalysisConfig())

            self.assertEqual(result.summary["samples"], 20)
            self.assertEqual(result.summary["data_quality"], "Low")
            self.assertAlmostEqual(result.summary["estimated_sample_rate_hz"], 100.0)
            self.assertAlmostEqual(result.metrics["RearRatio_mean"], 0.4036973023481836)
            self.assertAlmostEqual(result.metrics["CoP_AP_progression"], -0.0015674641012249912)
            self.assertEqual(
                result.risk_cards[0].title,
                "Walking posture cannot be inferred from this file",
            )

            expected = {
                "processed_gait_data.csv",
                "gait_steps_analysis.csv",
                "session_summary.json",
                "reference_screening_result.json",
                "pressure_stance.png",
                "orientation.png",
                "acceleration.png",
                "user_report.html",
                "technical_report.html",
                "data_guide.html",
                "posture_risk_output.csv",
                "reference_metric_comparison.csv",
            }
            self.assertEqual({artifact.name for artifact in result.artifacts}, expected)
            expected_eager_files = (expected - {"processed_gait_data.csv"}) | {
                "processed_gait_data.parquet"
            }
            self.assertEqual({path.name for path in output_dir.iterdir()}, expected_eager_files)
            artifact_sizes = {
                artifact.name: artifact.size_bytes for artifact in result.artifacts
            }
            self.assertEqual(artifact_sizes["processed_gait_data.csv"], 0)
            self.assertTrue(
                all(
                    size > 0
                    for name, size in artifact_sizes.items()
                    if name != "processed_gait_data.csv"
                )
            )

            serialized = json.dumps(result.to_dict(), allow_nan=False)
            self.assertNotIn("NaN", serialized)
            self.assertNotIn("Infinity", serialized)

    def test_standing_calibration_populates_relative_orientation_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = AnalysisService().analyze(
                FIXTURE,
                FIXTURE,
                Path(temporary_directory) / "result",
                AnalysisConfig(),
            )
        self.assertTrue(math.isfinite(result.metrics["PitchDelta_stance_from_standing"]))
        self.assertTrue(math.isfinite(result.metrics["LandingSoleGroundAngle_deg"]))


if __name__ == "__main__":
    unittest.main()
