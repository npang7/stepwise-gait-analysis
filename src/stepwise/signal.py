"""Signal preprocessing and stance segmentation."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .models import AnalysisConfig, SensorMapping


def rolling_smooth(series: pd.Series, window: int) -> pd.Series:
    normalized_window = max(1, int(window))
    if normalized_window % 2 == 0:
        normalized_window += 1
    return (
        series.rolling(window=normalized_window, center=True, min_periods=1)
        .median()
        .rolling(window=normalized_window, center=True, min_periods=1)
        .mean()
    )


def _sum_channels(frame: pd.DataFrame, channels: tuple[str, ...]) -> pd.Series:
    return frame[[f"{channel}_smooth" for channel in channels]].sum(axis=1)


def build_basic_features(frame: pd.DataFrame, config: AnalysisConfig) -> pd.DataFrame:
    output = frame.copy()
    pressure_columns = ("P1", "P2", "P3", "P4")
    mapping: SensorMapping = config.sensor_mapping
    for column in pressure_columns:
        output[f"{column}_smooth"] = rolling_smooth(
            output[column].clip(lower=0), config.smooth_window
        )

    output["TotalPressure"] = output[[f"{column}_smooth" for column in pressure_columns]].sum(
        axis=1
    )
    output["RearPressure"] = _sum_channels(output, (mapping.heel,))
    output["ArchPressure"] = _sum_channels(output, (mapping.arch,))
    output["FrontPressure"] = _sum_channels(
        output, (mapping.medial_forefoot, mapping.lateral_forefoot)
    )
    output["MedialPressure"] = _sum_channels(output, (mapping.medial_forefoot,))
    output["LateralPressure"] = _sum_channels(output, (mapping.lateral_forefoot,))
    output["ToePressure"] = 0.0
    output["PushOffPressure"] = output["FrontPressure"]

    denominator = output["TotalPressure"].replace(0, np.nan)
    for label in ("Rear", "Arch", "Front", "Medial", "Lateral"):
        output[f"{label}Ratio"] = output[f"{label}Pressure"] / denominator
    output["ToeRatio"] = np.nan
    output["PushOffRatio"] = output["PushOffPressure"] / denominator
    output["MedialLateralBalance"] = (
        output["MedialPressure"] - output["LateralPressure"]
    ) / (output["MedialPressure"] + output["LateralPressure"]).replace(0, np.nan)

    ap_weights = {column: 0.0 for column in pressure_columns}
    ml_weights = {column: 0.0 for column in pressure_columns}
    ap_weights[mapping.arch] = 0.45
    ap_weights[mapping.medial_forefoot] = 1.0
    ap_weights[mapping.lateral_forefoot] = 1.0
    ml_weights[mapping.medial_forefoot] = 0.45
    ml_weights[mapping.lateral_forefoot] = -0.45
    output["CoP_AP"] = sum(
        output[f"{column}_smooth"] * ap_weights[column] for column in pressure_columns
    ) / denominator
    output["CoP_ML"] = sum(
        output[f"{column}_smooth"] * ml_weights[column] for column in pressure_columns
    ) / denominator

    if config.body_weight_n is not None:
        output["TotalPressure_BW"] = output["TotalPressure"] / config.body_weight_n
    output["AccMag"] = np.sqrt(output["AccX"] ** 2 + output["AccY"] ** 2 + output["AccZ"] ** 2)
    output["GyrMag"] = np.sqrt(output["GyrX"] ** 2 + output["GyrY"] ** 2 + output["GyrZ"] ** 2)
    return output


def adaptive_threshold(
    total_pressure: pd.Series, min_threshold_n: float, ratio: float
) -> tuple[float, float]:
    enter = max(min_threshold_n, float(total_pressure.max()) * ratio)
    return enter, enter * 0.55


def hysteresis_contact(
    total_pressure: pd.Series, enter_threshold: float, exit_threshold: float
) -> np.ndarray:
    contact = np.zeros(len(total_pressure), dtype=bool)
    state = False
    for index, value in enumerate(total_pressure.to_numpy()):
        if not state and value >= enter_threshold:
            state = True
        elif state and value <= exit_threshold:
            state = False
        contact[index] = state
    return contact


def contact_intervals(
    contact: np.ndarray, time_s: pd.Series, min_stance_s: float
) -> list[tuple[int, int]]:
    intervals: list[tuple[int, int]] = []
    start: int | None = None
    for index, is_contact in enumerate(contact):
        if is_contact and start is None:
            start = index
        if start is not None and (not is_contact or index == len(contact) - 1):
            end = index - 1 if not is_contact else index
            if float(time_s.iloc[end] - time_s.iloc[start]) >= min_stance_s:
                intervals.append((start, end))
            start = None
    return intervals
