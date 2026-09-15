from __future__ import annotations

from functools import cache
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pandas.api.types import (
    is_bool_dtype,
    is_float_dtype,
    is_integer_dtype,
    is_object_dtype,
    is_string_dtype,
)

from stepwise._reference import (
    reference_extract_stance_features,
    reference_parse_text,
    reference_processed_frame_roundtrip,
)
from stepwise.features import extract_stance_features
from stepwise.models import AnalysisConfig
from stepwise.parsing import parse_stepwise_text, parse_stepwise_txt
from stepwise.signal import (
    adaptive_threshold,
    build_basic_features,
    contact_intervals,
    hysteresis_contact,
)

FIXTURES = Path(__file__).parent / "fixtures"
PARSER_FIXTURES = (
    "minimal_walk.txt",
    "synthetic_1682.txt",
    "zero_stances.txt",
    "one_stance.txt",
    "all_zero_channel.txt",
    "repeated_timestamps.txt",
    "malformed_interleaved.txt",
)
PIPELINE_FIXTURES = tuple(name for name in PARSER_FIXTURES if name != "malformed_interleaved.txt")


def _assert_exact_structure(actual: pd.DataFrame, expected: pd.DataFrame) -> None:
    assert len(actual) == len(expected), "row count differs"
    pd.testing.assert_index_equal(actual.columns, expected.columns, exact=True)
    pd.testing.assert_index_equal(actual.index, expected.index, exact=True)


def _assert_frame_equivalent(
    actual: pd.DataFrame,
    expected: pd.DataFrame,
    *,
    float_atol: float,
) -> None:
    _assert_exact_structure(actual, expected)
    for column in expected.columns:
        actual_column = actual[column]
        expected_column = expected[column]
        assert actual_column.dtype == expected_column.dtype, f"{column}: dtype differs"
        exact = (
            is_integer_dtype(expected_column.dtype)
            or is_bool_dtype(expected_column.dtype)
            or is_string_dtype(expected_column.dtype)
            or is_object_dtype(expected_column.dtype)
        )
        if exact:
            pd.testing.assert_series_equal(actual_column, expected_column, check_exact=True)
            continue
        assert is_float_dtype(expected_column.dtype), f"{column}: unsupported comparison dtype"
        actual_nan = actual_column.isna().to_numpy()
        expected_nan = expected_column.isna().to_numpy()
        np.testing.assert_array_equal(actual_nan, expected_nan, err_msg=f"{column}: NaN mask")
        finite = ~expected_nan
        np.testing.assert_allclose(
            actual_column.to_numpy()[finite],
            expected_column.to_numpy()[finite],
            atol=float_atol,
            rtol=0.0,
            err_msg=column,
        )


@cache
def _processed_and_intervals(path: Path) -> tuple[pd.DataFrame, list[tuple[int, int]]]:
    config = AnalysisConfig()
    processed = build_basic_features(parse_stepwise_txt(path), config)
    enter, exit_ = adaptive_threshold(
        processed["TotalPressure"], config.min_threshold_n, config.threshold_ratio
    )
    processed["FootContact"] = hysteresis_contact(processed["TotalPressure"], enter, exit_)
    intervals = contact_intervals(processed["FootContact"], processed["Time_s"], config.min_stance_s)
    return processed, intervals


def _current_processed_frame_roundtrip(frame: pd.DataFrame, path: Path) -> pd.DataFrame:
    frame.to_csv(path, index=False)
    return pd.read_csv(path)


@pytest.mark.parametrize("fixture_name", PARSER_FIXTURES)
def test_reference_parser_matches_shipping_parser_exactly(fixture_name: str) -> None:
    text = (FIXTURES / fixture_name).read_text(encoding="utf-8")
    actual = parse_stepwise_text(text)
    expected = reference_parse_text(text)
    pd.testing.assert_frame_equal(actual, expected, check_exact=True)


@pytest.mark.parametrize("fixture_name", PIPELINE_FIXTURES)
def test_reference_stance_features_match_shipping_loop(fixture_name: str) -> None:
    processed, intervals = _processed_and_intervals(FIXTURES / fixture_name)
    actual = extract_stance_features(processed, intervals)
    expected = reference_extract_stance_features(processed, intervals)
    _assert_frame_equivalent(actual, expected, float_atol=1e-9)


def test_fixture_matrix_exercises_required_edge_cases() -> None:
    synthetic = parse_stepwise_txt(FIXTURES / "synthetic_1682.txt")
    zero_frame, zero_intervals = _processed_and_intervals(FIXTURES / "zero_stances.txt")
    one_frame, one_intervals = _processed_and_intervals(FIXTURES / "one_stance.txt")
    all_zero = parse_stepwise_txt(FIXTURES / "all_zero_channel.txt")
    repeated = parse_stepwise_txt(FIXTURES / "repeated_timestamps.txt")
    malformed = parse_stepwise_txt(FIXTURES / "malformed_interleaved.txt")

    assert len(synthetic) == 1_682
    assert len(zero_frame) > 0 and zero_intervals == []
    assert len(one_frame) > 0 and len(one_intervals) == 1
    assert (all_zero["P4"] == 0).all()
    assert int((repeated["Time_s"].diff() == 0).sum()) == 2
    assert len(malformed) == 8
    assert int(malformed["P1"].isna().sum()) == 1


def test_one_stance_preserves_first_stride_and_swing_nan() -> None:
    processed, intervals = _processed_and_intervals(FIXTURES / "one_stance.txt")
    assert len(intervals) == 1
    actual = extract_stance_features(processed, intervals)
    expected = reference_extract_stance_features(processed, intervals)
    assert pd.isna(actual.loc[0, "StrideTime_s"])
    assert pd.isna(actual.loc[0, "SwingTime_s"])
    _assert_frame_equivalent(actual, expected, float_atol=1e-9)


def test_stance_window_boundaries_match_reference_without_mutating_input() -> None:
    processed, _intervals = _processed_and_intervals(FIXTURES / "synthetic_1682.txt")
    frame = processed.iloc[:40].copy()
    before = frame.copy(deep=True)
    intervals = [(0, 0), (4, 8), (12, 21), (25, 37)]

    actual = extract_stance_features(frame, intervals)
    expected = reference_extract_stance_features(frame, intervals)

    pd.testing.assert_frame_equal(frame, before, check_exact=True)
    _assert_frame_equivalent(actual, expected, float_atol=1e-9)


@pytest.mark.parametrize("fixture_name", PIPELINE_FIXTURES)
def test_reference_processed_csv_roundtrip_matches_current_path(
    fixture_name: str, tmp_path: Path
) -> None:
    processed, _intervals = _processed_and_intervals(FIXTURES / fixture_name)
    actual = _current_processed_frame_roundtrip(processed, tmp_path / "actual.csv")
    expected = reference_processed_frame_roundtrip(processed, tmp_path / "reference.csv")
    _assert_frame_equivalent(actual, expected, float_atol=1e-9)


def test_parser_oracle_rejects_a_mutated_result() -> None:
    text = (FIXTURES / "malformed_interleaved.txt").read_text(encoding="utf-8")
    expected = reference_parse_text(text)
    missing_row = expected.iloc[1:].reset_index(drop=True)
    changed_ulp = expected.copy()
    changed_ulp.loc[0, "Time_s"] = np.nextafter(changed_ulp.loc[0, "Time_s"], np.inf)
    for mutant in (missing_row, changed_ulp):
        with pytest.raises(AssertionError):
            pd.testing.assert_frame_equal(mutant, expected, check_exact=True)


def test_stance_oracle_rejects_mutated_exact_and_nan_values() -> None:
    processed, intervals = _processed_and_intervals(FIXTURES / "one_stance.txt")
    expected = reference_extract_stance_features(processed, intervals)
    changed_step = expected.copy()
    changed_step.loc[0, "Step"] += 1
    changed_pattern = expected.copy()
    changed_pattern.loc[0, "Pattern"] = "mutated"
    changed_nan = expected.copy()
    changed_nan.loc[0, "StrideTime_s"] = 0.0
    for mutant in (changed_step, changed_pattern, changed_nan):
        with pytest.raises(AssertionError):
            _assert_frame_equivalent(mutant, expected, float_atol=1e-9)


def test_roundtrip_oracle_rejects_a_mutated_column_order(tmp_path: Path) -> None:
    processed, _intervals = _processed_and_intervals(FIXTURES / "minimal_walk.txt")
    expected = reference_processed_frame_roundtrip(processed, tmp_path / "reference.csv")
    mutant = expected[expected.columns[::-1]]
    with pytest.raises(AssertionError):
        _assert_frame_equivalent(mutant, expected, float_atol=1e-9)


def _assert_bucket_envelope(
    source: pd.DataFrame,
    emitted: pd.DataFrame,
    *,
    bucket_size: int,
    columns: tuple[str, ...],
) -> None:
    for start in range(0, len(source), bucket_size):
        bucket = source.iloc[start : start + bucket_size]
        emitted_bucket = emitted[
            (emitted.index >= bucket.index[0]) & (emitted.index <= bucket.index[-1])
        ]
        for column in columns:
            values = emitted_bucket[column].to_numpy()
            assert bucket[column].min() in values
            assert bucket[column].max() in values


def test_naive_plot_striding_fails_min_max_envelope_without_mutating_source() -> None:
    source = pd.DataFrame(
        {
            "Time_s": np.arange(12, dtype=float),
            "TotalPressure": [0.0, 8.0, -3.0, 1.0, 0.0, 9.0, -4.0, 1.0, 0.0, 7.0, -5.0, 1.0],
        }
    )
    before = source.copy(deep=True)
    naive = source.iloc[::4]
    pd.testing.assert_frame_equal(source, before, check_exact=True)
    with pytest.raises(AssertionError):
        _assert_bucket_envelope(
            source, naive, bucket_size=4, columns=("TotalPressure",)
        )


def _write_large_fixture(path: Path, samples: int) -> None:
    header = (
        "StepWise anonymous synthetic gait fixture\n"
        "Sample SystemTime P1 P2 P3 P4 AccX AccY AccZ GyrX GyrY GyrZ Pitch Roll Yaw\n"
    )
    with path.open("w", encoding="utf-8") as handle:
        handle.write(header)
        for sample in range(samples):
            milliseconds = sample * 10
            hours, remainder = divmod(milliseconds, 3_600_000)
            minutes, remainder = divmod(remainder, 60_000)
            seconds, millis = divmod(remainder, 1_000)
            active = sample % 110 < 65
            pressures = (40, 80, 30, 50) if active else (0, 0, 0, 0)
            handle.write(
                f"{sample} {hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d} "
                f"{pressures[0]} {pressures[1]} {pressures[2]} {pressures[3]} "
                "0.1 0.2 9.8 1.0 2.0 3.0 4.0 5.0 6.0\n"
            )


@pytest.mark.large_equivalence
def test_large_360000_sample_reference_comparison(tmp_path: Path) -> None:
    fixture = tmp_path / "synthetic_360000.txt"
    _write_large_fixture(fixture, 360_000)
    text = fixture.read_text(encoding="utf-8")
    actual_parsed = parse_stepwise_text(text)
    expected_parsed = reference_parse_text(text)
    pd.testing.assert_frame_equal(actual_parsed, expected_parsed, check_exact=True)

    processed, intervals = _processed_and_intervals(fixture)
    actual_steps = extract_stance_features(processed, intervals)
    expected_steps = reference_extract_stance_features(processed, intervals)
    _assert_frame_equivalent(actual_steps, expected_steps, float_atol=1e-9)

    actual_roundtrip = _current_processed_frame_roundtrip(processed, tmp_path / "actual.csv")
    expected_roundtrip = reference_processed_frame_roundtrip(
        processed, tmp_path / "reference.csv"
    )
    _assert_frame_equivalent(actual_roundtrip, expected_roundtrip, float_atol=1e-9)
