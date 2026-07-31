from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

os.environ.setdefault("MPLBACKEND", "Agg")

from stepwise_gait_analysis import (  # noqa: E402
    SensorLayout,
    add_basic_features,
    adaptive_threshold,
    contact_intervals,
    extract_step_features,
    hysteresis_contact,
    parse_stepwise_txt,
    rolling_smooth,
)
from stepwise_reference_pipeline import write_json_outputs  # noqa: E402


FIXTURE = Path(__file__).parent / "fixtures" / "minimal_walk.txt"


class GaitAnalysisTests(unittest.TestCase):
    def test_parser_reads_anonymous_fixture(self) -> None:
        frame = parse_stepwise_txt(FIXTURE)
        self.assertEqual(len(frame), 20)
        self.assertEqual(frame.loc[2, "P2"], 40)
        self.assertAlmostEqual(frame["Time_s"].iloc[-1], 0.19)

    def test_parser_rejects_invalid_timestamp(self) -> None:
        row = "1 invalid 1 1 1 1 0 0 9.81 0 0 0 0 0 0\n"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "invalid.txt"
            path.write_text(row, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SystemTime"):
                parse_stepwise_txt(path)

    def test_smoothing_and_hysteresis_find_one_stance(self) -> None:
        frame = parse_stepwise_txt(FIXTURE)
        processed = add_basic_features(frame, SensorLayout(), None, 3)
        enter, exit_ = adaptive_threshold(processed["TotalPressure"], 5.0, 0.08)
        contact = hysteresis_contact(processed["TotalPressure"], enter, exit_)
        intervals = contact_intervals(contact, processed["Time_s"], 0.08)
        self.assertEqual(len(intervals), 1)
        self.assertGreater(float(rolling_smooth(pd.Series([0, 3, 0]), 3).iloc[1]), 0)

    def test_feature_extraction_supports_numpy_126(self) -> None:
        frame = parse_stepwise_txt(FIXTURE)
        processed = add_basic_features(frame, SensorLayout(), None, 3)
        enter, exit_ = adaptive_threshold(processed["TotalPressure"], 5.0, 0.08)
        contact = hysteresis_contact(processed["TotalPressure"], enter, exit_)
        intervals = contact_intervals(contact, processed["Time_s"], 0.08)
        steps = extract_step_features(processed, intervals)
        self.assertEqual(len(steps), 1)
        self.assertTrue(np.isfinite(steps.loc[0, "PressureImpulse_Ns"]))

    def test_reference_json_is_strict_and_replaces_nan_with_null(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            write_json_outputs(
                output,
                metrics={"RearRatio_mean": np.nan},
                baseline={"RearRatio_mean": np.nan},
                summary={"data_quality": "Low"},
                cards=[],
                baseline_source="none",
                reference_mode="public-only",
                standing_calibration=None,
            )
            text = (output / "reference_screening_result.json").read_text(encoding="utf-8")

            def reject_constant(value: str) -> None:
                raise ValueError(value)

            parsed = json.loads(text, parse_constant=reject_constant)
            self.assertIsNone(parsed["metrics"]["RearRatio_mean"])
            self.assertIsNone(parsed["baseline_metrics"]["RearRatio_mean"])


if __name__ == "__main__":
    unittest.main()
