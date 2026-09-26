from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stepwise.features import (
    _select_trapezoid,
    compute_session_metrics,
    extract_stance_features,
    summarize_session,
)
from stepwise.models import AnalysisConfig, SensorMapping
from stepwise.parsing import (
    InputValidationError,
    parse_stepwise_bytes,
    parse_stepwise_text,
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

    def test_parser_preserves_all_five_validation_codes(self) -> None:
        for payload, code in (
            (b"", "empty_input"),
            (b"\x00\x01\x02", "binary_input"),
            (b"\xff\xfe", "invalid_encoding"),
            (b"StepWise header only\n", "no_data_rows"),
            (b"1 not-a-time 1 2 3 4 0 0 1 0 0 0 0 0 0\n", "invalid_timestamp"),
        ):
            with self.subTest(code=code), self.assertRaises(InputValidationError) as context:
                parse_stepwise_bytes(payload)
            self.assertEqual(context.exception.code, code)

    def test_well_formed_input_uses_the_pandas_fast_path(self) -> None:
        text = FIXTURE.read_text(encoding="utf-8")
        with patch(
            "stepwise.parsing._parse_stepwise_text_line_filter",
            side_effect=AssertionError("line filter should not run"),
        ):
            parsed = parse_stepwise_text(text)
        self.assertEqual(len(parsed), 20)

    def test_vectorized_filter_matches_legacy_malformed_semantics(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "malformed_interleaved.txt"
        with patch(
            "stepwise.parsing._parse_stepwise_text_line_filter",
            side_effect=AssertionError("line filter should not run"),
        ):
            parsed = parse_stepwise_text(fixture.read_text(encoding="utf-8"))
        self.assertEqual(len(parsed), 8)
        self.assertEqual(int(parsed["P1"].isna().sum()), 1)

    def test_short_extra_junk_and_non_digit_sample_rows_are_dropped(self) -> None:
        valid = "0 00:00:00.000 1 2 3 4 0 0 1 0 0 0 0 0 0"
        valid_two = "1 00:00:00.010 1 2 3 4 0 0 1 0 0 0 0 0 0"
        text = "\n".join(
            (
                "junk header",
                valid,
                "2 00:00:00.020 1 2 3",
                "3 00:00:00.030 1 2 3 4 0 0 1 0 0 0 0 0 0 EXTRA",
                "4.5 00:00:00.040 1 2 3 4 0 0 1 0 0 0 0 0 0",
                "-1 00:00:00.050 1 2 3 4 0 0 1 0 0 0 0 0 0",
                valid_two + "   \t",
            )
        )
        parsed = parse_stepwise_text(text)
        pd.testing.assert_series_equal(
            parsed["Sample"], pd.Series([0, 1], name="Sample"), check_exact=True
        )

    def test_special_line_separators_use_the_legacy_splitlines_path(self) -> None:
        first = "0 00:00:00.000 1 2 3 4 0 0 1 0 0 0 0 0 0"
        second = "1 00:00:00.010 1 2 3 4 0 0 1 0 0 0 0 0 0"
        for separator in "\v\f\x1c\x1d\x1e\x85\u2028\u2029":
            with self.subTest(separator=repr(separator)), patch(
                "stepwise.parsing.pd.read_csv", side_effect=AssertionError("fast path used")
            ):
                parsed = parse_stepwise_text(first + separator + second)
            self.assertEqual(parsed["Sample"].tolist(), [0, 1])

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
    def test_trapezoid_selection_is_lazy_and_supports_both_numpy_apis(self) -> None:
        modern = object()
        legacy = object()

        self.assertIs(_select_trapezoid(SimpleNamespace(trapezoid=modern)), modern)
        self.assertIs(_select_trapezoid(SimpleNamespace(trapz=legacy)), legacy)

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
