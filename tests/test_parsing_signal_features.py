from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stepwise.features import (
    compute_session_metrics,
    extract_stance_features,
    summarize_session,
)
from stepwise.models import AnalysisConfig, SensorMapping
from stepwise.parsing import (
    InputValidationError,
    parse_stepwise_bytes,
    parse_stepwise_txt,
)
from stepwise.signal import (
    adaptive_threshold,
    build_basic_features,
    contact_intervals,
    hysteresis_contact,
    rolling_smooth,
)

FIXTURE = ROOT / "tests" / "fixtures" / "minimal_walk.txt"


class ParsingTests(unittest.TestCase):
    def test_parser_reads_utf8_and_utf8_bom(self) -> None:
        raw = FIXTURE.read_bytes()
        plain = parse_stepwise_bytes(raw)
        bom = parse_stepwise_bytes(b"\xef\xbb\xbf" + raw)
        self.assertEqual(len(plain), 20)
        self.assertEqual(len(bom), 20)
        self.assertAlmostEqual(float(plain["Time_s"].iloc[-1]), 0.19)

    def test_parser_rejects_empty_binary_and_invalid_timestamp(self) -> None:
        for payload, code in (
            (b"", "empty_input"),
            (b"\x00\x01\x02", "binary_input"),
            (b"1 not-a-time 1 2 3 4 0 0 1 0 0 0 0 0 0\n", "invalid_timestamp"),
        ):
            with self.subTest(code=code), self.assertRaises(InputValidationError) as context:
                parse_stepwise_bytes(payload)
            self.assertEqual(context.exception.code, code)

    def test_parser_accepts_duplicate_timestamps_for_quality_gating(self) -> None:
        lines = FIXTURE.read_text(encoding="utf-8").splitlines()
        parts = lines[3].split()
        parts[1] = lines[2].split()[1]
        lines[3] = " ".join(parts)
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "duplicates.txt"
            path.write_text("\n".join(lines), encoding="utf-8")
            parsed = parse_stepwise_txt(path)
        self.assertEqual(int((parsed["Time_s"].diff() == 0).sum()), 1)


class SignalAndFeatureTests(unittest.TestCase):
    def _analyze_fixture(self):
        config = AnalysisConfig(sensor_mapping=SensorMapping())
        parsed = parse_stepwise_txt(FIXTURE)
        processed = build_basic_features(parsed, config)
        enter, exit_ = adaptive_threshold(
            processed["TotalPressure"], config.min_threshold_n, config.threshold_ratio
        )
        contact = hysteresis_contact(processed["TotalPressure"], enter, exit_)
        processed["FootContact"] = contact
        intervals = contact_intervals(contact, processed["Time_s"], config.min_stance_s)
        steps = extract_stance_features(processed, intervals)
        summary = summarize_session(processed, steps, enter, exit_)
        metrics = compute_session_metrics(steps, processed)
        return processed, steps, summary, metrics

    def test_rolling_smooth_normalizes_even_windows(self) -> None:
        source = parse_stepwise_txt(FIXTURE)["P1"]
        np.testing.assert_allclose(rolling_smooth(source, 2), rolling_smooth(source, 3))

    def test_fixture_matches_golden_segmentation_and_metrics(self) -> None:
        _processed, steps, summary, metrics = self._analyze_fixture()
        self.assertEqual(len(steps), 1)
        self.assertEqual(summary["samples"], 20)
        self.assertEqual(summary["data_quality"], "Low")
        self.assertAlmostEqual(summary["estimated_sample_rate_hz"], 100.0)
        self.assertAlmostEqual(metrics["RearRatio_mean"], 0.4036973023481836)
        self.assertAlmostEqual(metrics["CoP_AP_progression"], -0.0015674641012249912)

    def test_low_quality_summary_records_duplicate_timestamp_warning(self) -> None:
        processed, steps, _summary, _metrics = self._analyze_fixture()
        processed.loc[2, "Time_s"] = processed.loc[1, "Time_s"]
        summary = summarize_session(processed, steps, 5.0, 2.75)
        self.assertEqual(summary["duplicate_timestamp_count"], 1)
        self.assertTrue(any("repeated timestamps" in warning for warning in summary["warnings"]))


if __name__ == "__main__":
    unittest.main()
