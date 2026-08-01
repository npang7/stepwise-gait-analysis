"""Strict JSON, CSV, HTML, and plot artifact generation."""

from __future__ import annotations

import csv
import html
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from .models import Artifact, RiskCard, strict_json_value

ARTIFACT_MEDIA_TYPES = {
    ".csv": "text/csv",
    ".html": "text/html",
    ".json": "application/json",
    ".png": "image/png",
}


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(strict_json_value(payload), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def _plot(
    path: Path,
    frame: pd.DataFrame,
    columns: Iterable[str],
    title: str,
    ylabel: str,
) -> None:
    figure, axis = plt.subplots(figsize=(8.4, 4.2))
    for column in columns:
        if column in frame:
            axis.plot(frame["Time_s"], frame[column], label=column, linewidth=1.25)
    axis.set(title=title, xlabel="Time (s)", ylabel=ylabel)
    axis.grid(alpha=0.25)
    axis.legend(loc="best", ncol=2, fontsize=8)
    figure.tight_layout()
    figure.savefig(path, dpi=130)
    plt.close(figure)


def _card_rows(cards: tuple[RiskCard, ...]) -> list[dict[str, str]]:
    return [card.to_dict() for card in cards]


def _report_html(
    title: str,
    summary: dict[str, Any],
    metrics: dict[str, Any],
    cards: tuple[RiskCard, ...],
    *,
    technical: bool,
) -> str:
    summary_rows = "".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in strict_json_value(summary).items()
        if key != "warnings"
    )
    card_blocks = "".join(
        "<article>"
        f"<h2>{html.escape(card.title)} <small>({html.escape(card.level)})</small></h2>"
        f"<p><strong>Evidence:</strong> {html.escape(card.evidence)}</p>"
        f"<p>{html.escape(card.interpretation)}</p>"
        f"<p><strong>Next step:</strong> {html.escape(card.action)}</p>"
        f"<p class='limitation'>{html.escape(card.limitation)}</p>"
        "</article>"
        for card in cards
    )
    metric_table = ""
    if technical:
        metric_rows = "".join(
            f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
            for key, value in strict_json_value(metrics).items()
        )
        metric_table = f"<h2>Metrics</h2><table>{metric_rows}</table>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
body {{ font: 15px/1.45 Arial, sans-serif; max-width: 960px; margin: 32px auto; color: #172033; }}
h1 {{ border-bottom: 2px solid #284b63; padding-bottom: 8px; }}
article {{ border: 1px solid #ccd5df; border-radius: 8px; padding: 12px 16px; margin: 12px 0; }}
table {{ border-collapse: collapse; width: 100%; }} th, td {{ border: 1px solid #d9e0e8; padding: 6px; text-align: left; }}
.limitation {{ color: #5d6570; font-size: 0.92em; }} small {{ font-weight: normal; }}
</style></head><body><h1>{html.escape(title)}</h1><h2>Session</h2><table>{summary_rows}</table>
<h2>Screening results</h2>{card_blocks}{metric_table}
<p><strong>Boundary:</strong> StepWise is an engineering screening prototype and does not provide a medical diagnosis.</p>
</body></html>"""


def write_analysis_artifacts(
    output_dir: Path,
    processed: pd.DataFrame,
    steps: pd.DataFrame,
    summary: dict[str, Any],
    metrics: dict[str, Any],
    cards: tuple[RiskCard, ...],
) -> tuple[Artifact, ...]:
    """Write the stable artifact set and return its download manifest."""
    output_dir.mkdir(parents=True, exist_ok=True)
    processed.to_csv(output_dir / "processed_gait_data.csv", index=False)
    steps.to_csv(output_dir / "gait_steps_analysis.csv", index=False)
    _write_json(output_dir / "session_summary.json", summary)
    _write_json(
        output_dir / "reference_screening_result.json",
        {
            "summary": summary,
            "metrics": metrics,
            "risk_cards": _card_rows(cards),
        },
    )

    _plot(
        output_dir / "pressure_stance.png",
        processed,
        ("P1_smooth", "P2_smooth", "P3_smooth", "P4_smooth", "TotalPressure"),
        "Plantar pressure and detected stance",
        "Pressure (N)",
    )
    _plot(
        output_dir / "orientation.png",
        processed,
        ("Roll", "Pitch", "Yaw"),
        "Foot orientation",
        "Angle (deg)",
    )
    _plot(
        output_dir / "acceleration.png",
        processed,
        ("AccX", "AccY", "AccZ", "AccMag"),
        "Acceleration",
        "Acceleration",
    )

    (output_dir / "user_report.html").write_text(
        _report_html("StepWise gait screening report", summary, metrics, cards, technical=False),
        encoding="utf-8",
    )
    (output_dir / "technical_report.html").write_text(
        _report_html("StepWise technical analysis report", summary, metrics, cards, technical=True),
        encoding="utf-8",
    )
    (output_dir / "data_guide.html").write_text(
        """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>StepWise data guide</title></head>
<body><h1>StepWise data guide</h1><p>Walking input is UTF-8 text containing a sample index, SystemTime,
four pressure channels, accelerometer, gyroscope, and orientation values.</p>
<p>Artifacts are engineering screening outputs and are not clinical measurements or diagnoses.</p></body></html>""",
        encoding="utf-8",
    )

    with (output_dir / "posture_risk_output.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("title", "level", "evidence", "interpretation", "action", "limitation"),
        )
        writer.writeheader()
        writer.writerows(_card_rows(cards))
    with (output_dir / "reference_metric_comparison.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        metric_writer = csv.writer(handle)
        metric_writer.writerow(("metric", "value"))
        for key, value in strict_json_value(metrics).items():
            metric_writer.writerow((key, value))

    artifacts: list[Artifact] = []
    for path in sorted(output_dir.iterdir(), key=lambda item: item.name):
        if path.is_file():
            artifacts.append(
                Artifact(
                    name=path.name,
                    media_type=ARTIFACT_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
                    size_bytes=path.stat().st_size,
                )
            )
    return tuple(artifacts)
