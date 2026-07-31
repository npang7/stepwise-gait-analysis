"""StepWise text parsing and input validation."""

from __future__ import annotations

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


def parse_stepwise_text(text: str) -> pd.DataFrame:
    rows: list[list[str]] = []
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == len(DEFAULT_COLUMNS) and parts[0].isdigit():
            rows.append(parts)
    if not rows:
        raise InputValidationError("no_data_rows", "no StepWise data rows were found")

    frame = pd.DataFrame(rows, columns=DEFAULT_COLUMNS)
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


def parse_stepwise_bytes(payload: bytes) -> pd.DataFrame:
    return parse_stepwise_text(decode_stepwise_bytes(payload))


def parse_stepwise_txt(path: Path) -> pd.DataFrame:
    return parse_stepwise_bytes(path.read_bytes())
