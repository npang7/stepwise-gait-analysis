"""Render the showcase image from actual synthetic analysis output."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from stepwise.models import AnalysisConfig
from stepwise.service import AnalysisService
from stepwise.signal import contact_intervals

ROOT = Path(__file__).resolve().parents[1]


def generate_assets(destination: Path) -> dict[str, Any]:
    """Analyze the committed recording and publish a plot plus its numeric provenance."""
    config = AnalysisConfig()
    with tempfile.TemporaryDirectory(prefix="stepwise-example-") as temporary:
        output = Path(temporary)
        result = AnalysisService().analyze(
            ROOT / "tests/fixtures/synthetic_1682.txt", None, output, config
        )
        frame = pd.read_parquet(output / "processed_gait_data.parquet")
    intervals = contact_intervals(
        frame["FootContact"].to_numpy(), frame["Time_s"], config.min_stance_s
    )
    evidence = {
        "input": "tests/fixtures/synthetic_1682.txt",
        "workload": "synthetic",
        "samples": result.summary["samples"],
        "duration_s": result.summary["duration_s"],
        "estimated_sample_rate_hz": result.summary["estimated_sample_rate_hz"],
        "detected_contact_intervals": len(intervals),
        "contact_intervals_s": [
            [float(frame["Time_s"].iloc[start]), float(frame["Time_s"].iloc[end])]
            for start, end in intervals
        ],
    }
    destination.mkdir(parents=True, exist_ok=True)
    with plt.rc_context({"font.family": "DejaVu Sans", "font.size": 11}):
        figure, axis = plt.subplots(figsize=(12, 4.8), facecolor="#f8fafc")
        axis.set_facecolor("#ffffff")
        axis.plot(frame["Time_s"], frame["TotalPressure"], color="#0f766e",
                  linewidth=1.6, label="Total pressure")
        for index, (start, end) in enumerate(evidence["contact_intervals_s"]):
            axis.axvspan(start, end, color="#38bdf8", alpha=0.16,
                         label="Detected contact" if index == 0 else None)
        axis.set(xlabel="Time (s)", ylabel="Pressure (N)",
                 xlim=(float(frame["Time_s"].iloc[0]), float(frame["Time_s"].iloc[-1])),
                 ylim=(-10, float(frame["TotalPressure"].max()) * 1.18))
        axis.grid(axis="y", alpha=0.18)
        axis.spines[["top", "right"]].set_visible(False)
        axis.legend(loc="upper right", frameon=False, ncol=2)
        figure.suptitle("StepWise | Synthetic sensor analysis", x=0.07, ha="left",
                        fontsize=19, fontweight="bold", color="#0f172a")
        axis.set_title(
            f"{evidence['samples']:,} samples  ·  "
            f"{evidence['estimated_sample_rate_hz']:.0f} Hz  ·  "
            f"{evidence['duration_s']:.2f} s  ·  {len(intervals)} contact intervals",
            loc="left", color="#475569", pad=16,
        )
        figure.tight_layout(rect=(0, 0, 1, 0.93))
        figure.savefig(destination / "synthetic-pressure-stance.png", dpi=150)
        plt.close(figure)
    (destination / "synthetic-summary.json").write_text(
        json.dumps(evidence, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    return evidence


if __name__ == "__main__":
    print(json.dumps(generate_assets(ROOT / "docs/assets"), indent=2))
