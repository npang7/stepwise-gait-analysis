"""Recompute the measured values cited by README.md from committed evidence."""

from __future__ import annotations

import argparse
import json
import statistics
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> dict[str, Any]:
    return json.loads(
        (ROOT / relative_path).read_text(encoding="utf-8"),
        parse_float=Decimal,
    )


def _result_for_samples(payload: dict[str, Any], samples: int) -> dict[str, Any]:
    return next(result for result in payload["results"] if result["samples"] == samples)


def _four_places_half_up(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def aba_metrics() -> str:
    a1 = _result_for_samples(_read("bench-data/baseline-20260916-043413.json"), 360_000)
    optimized = _result_for_samples(
        _read("bench-data/baseline-20260916-043810.json"), 360_000
    )
    a2 = _result_for_samples(_read("bench-data/baseline-20260916-043945.json"), 360_000)

    control_total = statistics.median(a1["raw_total_seconds"] + a2["raw_total_seconds"])
    control_difference = (
        abs(a1["stage_seconds"]["TOTAL"] - a2["stage_seconds"]["TOTAL"])
        / statistics.mean(
            (a1["stage_seconds"]["TOTAL"], a2["stage_seconds"]["TOTAL"])
        )
        * 100
    )

    def control_stage(stage: str) -> float:
        return statistics.mean((a1["stage_seconds"][stage], a2["stage_seconds"][stage]))

    return (
        f"total={_four_places_half_up(control_total)}"
        f"->{optimized['stage_seconds']['TOTAL']:.4f}s "
        f"control_difference={control_difference:.4f}% "
        f"artifacts={control_stage('artifacts'):.4f}"
        f"->{optimized['stage_seconds']['artifacts']:.4f}s "
        f"stance_features={control_stage('stance_features'):.4f}"
        f"->{optimized['stage_seconds']['stance_features']:.4f}s "
        f"parse={control_stage('parse'):.4f}->{optimized['stage_seconds']['parse']:.4f}s"
    )


def memory_metrics() -> str:
    evidence = _read("bench-data/parser-memory-20260915-212028.json")["api_peak_rss"]
    before = evidence["before"]["peak_working_set_bytes"]
    after = evidence["after"]["peak_working_set_bytes"]
    return f"peak_rss={before / 1e9:.2f}GB->{after / 1e6:.0f}MB"


def upload_metrics() -> str:
    recording = _read("bench-data/baseline-phase1-upload-20260915-005951.json")[
        "recording"
    ]
    hours = recording["samples"] / 100 / 3600
    return (
        f"limit={recording['limit_bytes'] / 2**20:.0f}MiB "
        f"samples={recording['samples']:,} duration_at_100Hz={hours:.4f}h"
    )


def load_metrics() -> str:
    evidence = _read("bench-data/loadtest-20260917T040248Z/raw-results.json")
    points = {
        (point["sample_count"], point["concurrency"]): point
        for point in evidence["points"]
    }
    c10 = points[(30_000, 10)]
    c20 = points[(30_000, 20)]
    formal = c10["repetitions"][0]["formal"]
    queue = formal["queue_seconds"]["p50"]
    end_to_end = formal["end_to_end_seconds"]["p50"]
    consistent = sum(
        item["successful_jobs_checked"] for item in evidence["correctness"].values()
    )
    return (
        f"workers={evidence['configuration']['max_workers']} "
        f"C10={c10['throughput_median_jobs_per_minute']:.1f}/min "
        f"C20={c20['throughput_median_jobs_per_minute']:.1f}/min "
        f"queue_share={queue / end_to_end * 100:.1f}% "
        f"({queue:.3f}/{end_to_end:.3f}s) consistent={consistent:,}"
    )


COMMANDS = {
    "aba": aba_metrics,
    "memory": memory_metrics,
    "upload": upload_metrics,
    "load": load_metrics,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metric", choices=(*COMMANDS, "all"))
    args = parser.parse_args()
    selected = COMMANDS if args.metric == "all" else {args.metric: COMMANDS[args.metric]}
    for name, function in selected.items():
        print(f"{name}: {function()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
