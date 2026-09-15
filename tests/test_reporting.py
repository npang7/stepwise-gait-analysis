from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stepwise.reporting import (
    PROCESSED_CSV_NAME,
    PROCESSED_PARQUET_NAME,
    materialize_processed_csv,
)


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
