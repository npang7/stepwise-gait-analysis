"""StepWise text parsing and input validation."""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

import pandas as pd

DEFAULT_COLUMNS = [
    "Sample",
    "SystemTime",
    "P1",
    "P2",
    "P3",
    "P4",
    "AccX",
    "AccY",
    "AccZ",
    "GyrX",
    "GyrY",
    "GyrZ",
    "Pitch",
    "Roll",
    "Yaw",
]
_SPECIAL_LINE_SEPARATOR_PATTERN = r"[\v\f\x1c\x1d\x1e\x85\u2028\u2029]"


class InputValidationError(ValueError):
    """A stable, client-safe validation failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def decode_stepwise_bytes(payload: bytes) -> str:
    if not payload:
        raise InputValidationError("empty_input", "uploaded recording is empty")
    if b"\x00" in payload:
        raise InputValidationError("binary_input", "uploaded recording must be text")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InputValidationError("invalid_encoding", "recording must be UTF-8 text") from exc
    if not text.strip():
        raise InputValidationError("empty_input", "uploaded recording is empty")
    return text


def _finish_stepwise_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        raise InputValidationError("no_data_rows", "no StepWise data rows were found")

    for column in DEFAULT_COLUMNS:
        if column != "SystemTime":
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["Sample"]).reset_index(drop=True)
    if frame.empty:
        raise InputValidationError("no_data_rows", "no StepWise data rows were found")

    timestamps = pd.to_datetime(frame["SystemTime"], format="%H:%M:%S.%f", errors="coerce")
    if timestamps.isna().any():
        raise InputValidationError(
            "invalid_timestamp", "SystemTime values must use HH:MM:SS.mmm"
        )
    elapsed = (timestamps - timestamps.iloc[0]).dt.total_seconds()
    elapsed = elapsed.mask(elapsed < 0, elapsed + 24 * 3600)
    frame["Time_s"] = elapsed
    return frame


def _parse_stepwise_text_line_filter(text: str) -> pd.DataFrame:
    rows: list[list[str]] = []
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == len(DEFAULT_COLUMNS) and parts[0].isdigit():
            rows.append(parts)
    return _finish_stepwise_frame(pd.DataFrame(rows, columns=DEFAULT_COLUMNS))


def _contains_special_line_separator(text: str) -> bool:
    return re.search(_SPECIAL_LINE_SEPARATOR_PATTERN, text) is not None


def _filter_stepwise_rows(frame: pd.DataFrame) -> pd.DataFrame:
    missing_fields = (frame == "").any(axis=1)
    numeric_sample = frame["Sample"].str.isdigit()
    return frame.loc[~missing_fields & numeric_sample].copy()


def parse_stepwise_text(text: str) -> pd.DataFrame:
    if _contains_special_line_separator(text):
        return _parse_stepwise_text_line_filter(text)
    if not text.strip():
        raise InputValidationError("no_data_rows", "no StepWise data rows were found")

    frame = pd.read_csv(
        io.StringIO(text),
        names=DEFAULT_COLUMNS,
        dtype=str,
        keep_default_na=False,
        na_values=[],
        on_bad_lines="skip",
        quoting=csv.QUOTE_NONE,
        sep=r"\s+",
        engine="c",
    )
    frame = _filter_stepwise_rows(frame)
    return _finish_stepwise_frame(frame)


def parse_stepwise_bytes(payload: bytes) -> pd.DataFrame:
    return parse_stepwise_text(decode_stepwise_bytes(payload))


def parse_stepwise_txt(path: Path) -> pd.DataFrame:
    return parse_stepwise_bytes(path.read_bytes())


def validate_stepwise_path(path: Path) -> None:
    """Validate a staged recording through the canonical parser."""
    parse_stepwise_txt(path)
