from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stepwise.reporting import (
    PROCESSED_CSV_NAME,
    PROCESSED_PARQUET_NAME,
    _min_max_envelope,
    _plot,
    materialize_processed_csv,
    write_analysis_artifacts,
)


def _assert_bucket_extrema_are_retained(
    source: pd.Series,
    emitted: pd.Series,
    *,
    target_buckets: int,
) -> None:
    bucket_size = max(1, (len(source) + target_buckets - 1) // target_buckets)
    for start in range(0, len(source), bucket_size):
        bucket = source.iloc[start : start + bucket_size]
        finite = bucket[np.isfinite(bucket.to_numpy(dtype=float, copy=False))]
        if finite.empty:
            continue
        assert finite.idxmin() in emitted.index
        assert finite.idxmax() in emitted.index


def test_min_max_envelope_keeps_short_series_exact_and_does_not_mutate() -> None:
    time = pd.Series([0.0, 0.1, 0.2, 0.3], name="Time_s")
    values = pd.Series([2.0, 1.0, 3.0, 2.5], name="pressure")
    before_time = time.copy(deep=True)
    before_values = values.copy(deep=True)

    emitted_time, emitted_values = _min_max_envelope(
        time, values, target_buckets=4
    )

    pd.testing.assert_series_equal(emitted_time, time)
    pd.testing.assert_series_equal(emitted_values, values)
    pd.testing.assert_series_equal(time, before_time)
    pd.testing.assert_series_equal(values, before_values)


def test_min_max_envelope_keeps_each_bucket_extrema_in_time_order() -> None:
    time = pd.Series(np.arange(11, dtype=float), name="Time_s")
    values = pd.Series(
        [5.0, -4.0, 9.0, 1.0, 7.0, 8.0, -6.0, 2.0, 3.0, 3.0, 3.0],
        name="pressure",
    )
    before = values.copy(deep=True)

    emitted_time, emitted_values = _min_max_envelope(
        time, values, target_buckets=3
    )

    assert emitted_values.index.is_monotonic_increasing
    assert emitted_values.index.is_unique
    assert emitted_values.index[0] == values.index[0]
    assert emitted_values.index[-1] == values.index[-1]
    assert emitted_time.index.equals(emitted_values.index)
    _assert_bucket_extrema_are_retained(values, emitted_values, target_buckets=3)
    pd.testing.assert_series_equal(values, before)


def test_min_max_envelope_preserves_nan_gap_markers() -> None:
    time = pd.Series(np.arange(12, dtype=float), name="Time_s")
    values = pd.Series(
        [1.0, 2.0, np.nan, np.nan, 3.0, 4.0, 5.0, np.nan, 6.0, 7.0, 8.0, 9.0],
        name="signal",
    )

    _, emitted = _min_max_envelope(time, values, target_buckets=3)

    assert 2 in emitted.index
    assert 7 in emitted.index
    assert emitted.loc[[2, 7]].isna().all()


def test_min_max_envelope_selects_each_series_independently() -> None:
    time = pd.Series(np.arange(12, dtype=float), name="Time_s")
    first = pd.Series(
        [0.0, 9.0, -3.0, 1.0, 0.0, 8.0, -2.0, 1.0, 0.0, 7.0, -1.0, 1.0]
    )
    second = pd.Series(
        [9.0, 0.0, 1.0, -3.0, 8.0, 0.0, 1.0, -2.0, 7.0, 0.0, 1.0, -1.0]
    )

    _, emitted_first = _min_max_envelope(time, first, target_buckets=3)
    _, emitted_second = _min_max_envelope(time, second, target_buckets=3)

    assert not emitted_first.index.equals(emitted_second.index)
    _assert_bucket_extrema_are_retained(first, emitted_first, target_buckets=3)
    _assert_bucket_extrema_are_retained(second, emitted_second, target_buckets=3)


def test_plot_passes_decimated_series_to_matplotlib_without_mutating_source(
    tmp_path: Path,
) -> None:
    sample_count = 12_001
    frame = pd.DataFrame(
        {
            "Time_s": np.arange(sample_count, dtype=float) / 100,
            "first": np.sin(np.arange(sample_count) / 10),
            "second": np.cos(np.arange(sample_count) / 17),
        }
    )
    before = frame.copy(deep=True)
    figure = MagicMock()
    axis = MagicMock()

    with (
        patch("stepwise.reporting.plt.subplots", return_value=(figure, axis)),
        patch("stepwise.reporting.plt.close"),
    ):
        _plot(tmp_path / "plot.png", frame, ("first", "second"), "title", "value")

    assert axis.plot.call_count == 2
    first_time, first_values = axis.plot.call_args_list[0].args
    second_time, second_values = axis.plot.call_args_list[1].args
    assert len(first_values) < sample_count
    assert len(second_values) < sample_count
    assert first_time.index.equals(first_values.index)
    assert second_time.index.equals(second_values.index)
    assert not first_values.index.equals(second_values.index)
    pd.testing.assert_frame_equal(frame, before)


def test_write_analysis_artifacts_stores_the_complete_processed_frame(
    tmp_path: Path,
) -> None:
    processed = pd.DataFrame(
        {
            "Time_s": np.arange(5_001, dtype=float) / 100,
            "P1_smooth": np.arange(5_001, dtype=float),
        }
    )
    steps = pd.DataFrame({"Step": [1]})

    with patch("stepwise.reporting._plot"):
        write_analysis_artifacts(tmp_path, processed, steps, {}, {}, ())

    stored = pd.read_parquet(tmp_path / PROCESSED_PARQUET_NAME)
    pd.testing.assert_frame_equal(stored, processed)


def test_materialize_processed_csv_is_atomic_and_cached(tmp_path: Path) -> None:
    frame = pd.DataFrame({"sample": [1, 2], "pressure": [1.25, float("nan")]})
    frame.to_parquet(tmp_path / PROCESSED_PARQUET_NAME, index=False)

    destination = materialize_processed_csv(tmp_path)

    expected = tmp_path / "expected.csv"
    frame.to_csv(expected, index=False)
    assert destination.name == PROCESSED_CSV_NAME
    assert destination.read_bytes() == expected.read_bytes()
    assert list(tmp_path.glob(f".{PROCESSED_CSV_NAME}.*.tmp")) == []
    with patch("stepwise.reporting.pd.read_parquet") as read_parquet:
        assert materialize_processed_csv(tmp_path) == destination
    read_parquet.assert_not_called()


def test_materialize_processed_csv_removes_temporary_file_on_failure(tmp_path: Path) -> None:
    frame = pd.DataFrame({"sample": [1]})
    frame.to_parquet(tmp_path / PROCESSED_PARQUET_NAME, index=False)
    with (
        patch.object(pd.DataFrame, "to_csv", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        materialize_processed_csv(tmp_path)

    assert not (tmp_path / PROCESSED_CSV_NAME).exists()
    assert list(tmp_path.glob(f".{PROCESSED_CSV_NAME}.*.tmp")) == []


def test_materialize_processed_csv_cleans_up_after_atomic_replace_failure(
    tmp_path: Path,
) -> None:
    pd.DataFrame({"sample": [1]}).to_parquet(
        tmp_path / PROCESSED_PARQUET_NAME, index=False
    )
    with (
        patch("pathlib.Path.replace", side_effect=OSError("replace failed")),
        pytest.raises(OSError, match="replace failed"),
    ):
        materialize_processed_csv(tmp_path)

    assert not (tmp_path / PROCESSED_CSV_NAME).exists()
    assert list(tmp_path.glob(f".{PROCESSED_CSV_NAME}.*.tmp")) == []
