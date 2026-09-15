"""StepWise pipeline benchmark harness.

Drop this at the repo root and run:

    python bench_stepwise.py                  # default sizes
    python bench_stepwise.py --sizes 1682 360000
    python bench_stepwise.py --compare        # also run the optimisation prototypes
    python bench_stepwise.py --warmup 0       # disable the default discarded warm-up

Writes bench-data/baseline-<date>.json and appends a row to BENCHMARKS.md so every
number you later put on a resume is traceable to a commit and a machine.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import platform
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

sys.path.insert(0, "src")

from stepwise.features import (  # noqa: E402
    compute_session_metrics,
    extract_stance_features,
    summarize_session,
)
from stepwise.models import AnalysisConfig  # noqa: E402
from stepwise.parsing import DEFAULT_COLUMNS, parse_stepwise_txt  # noqa: E402
from stepwise.reporting import write_analysis_artifacts  # noqa: E402
from stepwise.screening import build_risk_cards  # noqa: E402
from stepwise.signal import (  # noqa: E402
    adaptive_threshold,
    build_basic_features,
    contact_intervals,
    hysteresis_contact,
)

ROOT = Path("bench-data")
STAGES = ["parse", "features_basic", "segment", "stance_features",
          "screening", "artifacts", "TOTAL"]
COMPARE_NOTICE = (
    "DIRECTIONAL REFERENCE ONLY: the optimisation prototypes below run once, so their "
    "variance is unknown and their speedup factors are not citable results. Phase 2 must "
    "remeasure before/after for every optimisation with --repeat 3; do not reuse these "
    "Phase 0 comparison numbers.\n"
)
HEADER = (
    "StepWise synthetic benchmark recording\n"
    "Sample SystemTime P1 P2 P3 P4 AccX AccY AccZ GyrX GyrY GyrZ Pitch Roll Yaw\n"
)
BENCHMARK_APPEND_MARKER = (
    "<!-- bench_stepwise.py inserts new per-size pipeline rows immediately above this line. -->\n"
)


def timed(fn, *args, **kwargs):
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    return time.perf_counter() - start, result


# --------------------------------------------------------------------------- generate
def generate(n_samples: int, path: Path, fs: float = 100.0, seed: int = 7) -> Path:
    """Gait-like recording: ~1.1 s stride, ~0.65 s stance, 4 FSR channels + IMU."""
    rng = np.random.default_rng(seed)
    t = np.arange(n_samples) / fs
    stride, stance = 1.1, 0.65
    phase = np.mod(t, stride)
    profile = np.where(phase < stance, np.sin(np.pi * np.clip(phase / stance, 0, 1)), 0.0)

    channels = [
        np.clip(amp * profile + rng.normal(0, 2.0, n_samples), 0, None)
        for amp in (140.0, 260.0, 120.0, 180.0)
    ]
    acc = [rng.normal(0, 0.4, n_samples), rng.normal(0, 0.4, n_samples),
           9.81 + rng.normal(0, 0.5, n_samples)]
    gyr = [rng.normal(0, 8, n_samples) for _ in range(3)]
    pitch = 6 * np.sin(2 * np.pi * t / stride) + rng.normal(0, 0.4, n_samples)
    roll = 2.5 * np.sin(2 * np.pi * t / stride + 0.6) + rng.normal(0, 0.3, n_samples)
    yaw = np.cumsum(rng.normal(0, 0.01, n_samples))

    ms = np.round(t * 1000).astype(np.int64)
    hh, rem = np.divmod(ms, 3_600_000)
    mm, rem = np.divmod(rem, 60_000)
    ss, mmm = np.divmod(rem, 1000)

    series = channels + acc + gyr + [pitch, roll, yaw]
    line = "{} {:02d}:{:02d}:{:02d}.{:03d} " + " ".join(["{:.2f}"] * 13) + "\n"
    parts = [HEADER]
    for i in range(n_samples):
        parts.append(line.format(i, hh[i], mm[i], ss[i], mmm[i], *(s[i] for s in series)))
    path.write_text("".join(parts), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- baseline
def bench_one(n_samples: int, out_root: Path) -> dict:
    source = out_root / f"walk_{n_samples}.txt"
    if not source.exists():
        generate(n_samples, source)
    config = AnalysisConfig()
    stage: dict[str, float] = {}

    stage["parse"], frame = timed(parse_stepwise_txt, source)
    stage["features_basic"], processed = timed(build_basic_features, frame, config)

    start = time.perf_counter()
    low, high = adaptive_threshold(
        processed["TotalPressure"], config.min_threshold_n, config.threshold_ratio
    )
    processed["FootContact"] = hysteresis_contact(processed["TotalPressure"], low, high)
    intervals = contact_intervals(processed["FootContact"], processed["Time_s"],
                                  config.min_stance_s)
    stage["segment"] = time.perf_counter() - start

    start = time.perf_counter()
    steps = extract_stance_features(processed, intervals)
    summary = summarize_session(processed, steps, low, high)
    metrics = compute_session_metrics(steps, processed)
    stage["stance_features"] = time.perf_counter() - start

    stage["screening"], cards = timed(
        build_risk_cards, metrics, summary,
        pitch_eversion_sign="positive", standing_calibration=None,
    )

    art_dir = out_root / f"artifacts_{n_samples}"
    art_dir.mkdir(exist_ok=True)
    stage["artifacts"], _ = timed(
        write_analysis_artifacts, art_dir, processed, steps, summary, metrics, cards
    )

    stage["TOTAL"] = sum(stage.values())
    return {
        "samples": n_samples,
        "duration_s": n_samples / 100.0,
        "input_mb": round(source.stat().st_size / 1e6, 2),
        "stance_phases": len(intervals),
        "stage_seconds": {k: round(v, 4) for k, v in stage.items()},
    }


# ------------------------------------------------------------------- optimisation demo
def parse_fast(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, sep=r"\s+", skiprows=2, names=DEFAULT_COLUMNS,
                        engine="c", dtype={"SystemTime": "string"})
    for column in DEFAULT_COLUMNS:
        if column != "SystemTime":
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    stamps = pd.to_datetime(frame["SystemTime"], format="%H:%M:%S.%f",
                            errors="coerce", cache=True)
    elapsed = (stamps - stamps.iloc[0]).dt.total_seconds()
    frame["Time_s"] = elapsed.mask(elapsed < 0, elapsed + 86400)
    return frame


SEG_MEAN = ["RearRatio", "ArchRatio", "FrontRatio", "MedialRatio", "LateralRatio",
            "MedialLateralBalance", "CoP_ML"]


def stance_features_vectorised(frame: pd.DataFrame, intervals) -> pd.DataFrame:
    """groupby + cumulative-trapezoid equivalent of the per-step loop."""
    n = len(frame)
    starts = np.fromiter((s for s, _ in intervals), np.int64, len(intervals))
    ends = np.fromiter((e for _, e in intervals), np.int64, len(intervals))

    seg = np.full(n, -1, np.int64)
    early = np.full(n, -1, np.int64)
    late = np.full(n, -1, np.int64)
    for label, (s, e) in enumerate(zip(starts, ends)):
        length = e - s + 1
        seg[s:e + 1] = label
        early[s:s + max(1, int(length * 0.20)) + 1] = label
        late[s + int(length * 0.65):e + 1] = label

    tagged = frame.assign(_seg=seg, _early=early, _late=late)
    grouped = tagged[tagged._seg >= 0].groupby("_seg", sort=True)
    out = grouped.agg(**{f"{c}_mean": (c, "mean") for c in SEG_MEAN})
    out["PeakPressure_N"] = grouped["TotalPressure"].max()
    out["MeanPressure_N"] = grouped["TotalPressure"].mean()
    out["StartTime_s"] = grouped["Time_s"].first()
    out["EndTime_s"] = grouped["Time_s"].last()
    out["StanceTime_s"] = out["EndTime_s"] - out["StartTime_s"]
    out["StrideTime_s"] = out["StartTime_s"].diff()
    out["SwingTime_s"] = out["StrideTime_s"] - out["StanceTime_s"]

    times = frame["Time_s"].to_numpy()
    pressure = frame["TotalPressure"].to_numpy()
    cumulative = np.concatenate(
        [[0.0], np.cumsum(np.diff(times) * (pressure[1:] + pressure[:-1]) / 2)]
    )
    out["PressureImpulse_Ns"] = cumulative[ends] - cumulative[starts]
    return out.reset_index(drop=True)


def decimate(frame: pd.DataFrame, target: int = 4000) -> pd.DataFrame:
    return frame.iloc[:: max(1, len(frame) // target)]


def compare(n_samples: int, out_root: Path) -> None:
    source = out_root / f"walk_{n_samples}.txt"
    if not source.exists():
        generate(n_samples, source)
    scratch = out_root / "compare"
    scratch.mkdir(exist_ok=True)

    t_loop, frame = timed(parse_stepwise_txt, source)
    t_fast, frame_fast = timed(parse_fast, source)
    print(f"parse            {t_loop:7.2f}s -> {t_fast:6.2f}s  "
          f"({t_loop / t_fast:4.1f}x)  rows_match={len(frame) == len(frame_fast)}")

    config = AnalysisConfig()
    processed = build_basic_features(frame, config)
    low, high = adaptive_threshold(processed["TotalPressure"], config.min_threshold_n,
                                   config.threshold_ratio)
    processed["FootContact"] = hysteresis_contact(processed["TotalPressure"], low, high)
    intervals = contact_intervals(processed["FootContact"], processed["Time_s"],
                                  config.min_stance_s)

    t_steps_loop, steps_loop = timed(extract_stance_features, processed, intervals)
    t_steps_vec, steps_vec = timed(stance_features_vectorised, processed, intervals)
    delta_peak = np.abs(steps_loop["PeakPressure_N"].to_numpy()
                        - steps_vec["PeakPressure_N"].to_numpy()).max()
    delta_impulse = np.abs(steps_loop["PressureImpulse_Ns"].to_numpy()
                           - steps_vec["PressureImpulse_Ns"].to_numpy()).max()
    print(f"stance features  {t_steps_loop:7.2f}s -> {t_steps_vec:6.2f}s  "
          f"({t_steps_loop / t_steps_vec:4.1f}x)  max|delta| peak={delta_peak:.1e} "
          f"impulse={delta_impulse:.1e}")

    t_csv, _ = timed(processed.to_csv, scratch / "processed.csv", index=False)
    size_csv = (scratch / "processed.csv").stat().st_size / 1e6
    compact = processed.copy()
    for column in compact.columns:
        if compact[column].dtype == np.float64:
            compact[column] = compact[column].astype(np.float32)
    t_bin, _ = timed(compact.to_pickle, scratch / "processed.pkl")
    size_bin = (scratch / "processed.pkl").stat().st_size / 1e6
    print(f"processed dump   {t_csv:7.2f}s -> {t_bin:6.2f}s  ({t_csv / t_bin:4.1f}x)  "
          f"{size_csv:.0f} MB -> {size_bin:.0f} MB   "
          f"[use parquet in production, not pickle]")

    def draw(frame_in: pd.DataFrame, path: Path) -> None:
        figure, axis = plt.subplots(figsize=(8.4, 4.2))
        for column in ("P1_smooth", "P2_smooth", "P3_smooth", "P4_smooth", "TotalPressure"):
            if column in frame_in:
                axis.plot(frame_in["Time_s"], frame_in[column], label=column, linewidth=1.25)
        axis.legend(loc="best", ncol=2, fontsize=8)
        figure.tight_layout()
        figure.savefig(path, dpi=130)
        plt.close(figure)

    t_plot_full, _ = timed(draw, processed, scratch / "full.png")
    small = decimate(processed)
    t_plot_dec, _ = timed(draw, small, scratch / "decimated.png")
    print(f"one plot         {t_plot_full:7.2f}s -> {t_plot_dec:6.2f}s  "
          f"({t_plot_full / t_plot_dec:4.1f}x)  {len(processed)} -> {len(small)} points")


# --------------------------------------------------------------------------- reporting
def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=5).stdout.strip() or "?"
    except Exception:
        return "?"


def summarize_runs(runs: list[list[dict]]) -> list[dict]:
    """Aggregate corresponding sample sizes without changing the measured raw rows."""
    summaries: list[dict] = []
    for row_index in range(len(runs[0])):
        source_rows = [run[row_index] for run in runs]
        samples = source_rows[0]["samples"]
        if any(row["samples"] != samples for row in source_rows):
            raise ValueError("benchmark runs contain different sample sizes or ordering")
        stage_medians = {
            stage: round(statistics.median(row["stage_seconds"][stage] for row in source_rows), 4)
            for stage in STAGES
        }
        raw_totals = [row["stage_seconds"]["TOTAL"] for row in source_rows]
        total_median = stage_medians["TOTAL"]
        spread = 0.0 if total_median == 0 else (max(raw_totals) - min(raw_totals)) / total_median * 100
        summaries.append({
            "samples": samples,
            "duration_s": source_rows[0]["duration_s"],
            "input_mb": source_rows[0]["input_mb"],
            "stance_phases": source_rows[0]["stance_phases"],
            "stage_seconds": stage_medians,
            "raw_total_seconds": raw_totals,
            "total_relative_range_percent": round(spread, 4),
        })
    return summaries


def print_table(rows: list[dict]) -> None:
    width = f"{'samples':>9} {'dur':>7} {'MB':>6} {'steps':>6} | " + \
            " ".join(f"{stage[:9]:>9}" for stage in STAGES)
    print("\n" + "=" * len(width))
    print(width)
    print("-" * len(width))
    for row in rows:
        cells = " ".join(f"{row['stage_seconds'][stage]:>9.3f}" for stage in STAGES)
        print(f"{row['samples']:>9} {row['duration_s']:>6.0f}s {row['input_mb']:>6.2f} "
              f"{row['stance_phases']:>6} | {cells}")
    print("=" * len(width))


def benchmark_markdown_rows(
    stamp: str, commit: str, summaries: list[dict], repeat: int, warmup: int
) -> list[str]:
    lines = []
    for row in summaries:
        note = "" if repeat == 1 and warmup == 0 else (
            f"median of {repeat} after {warmup} warmup, "
            f"spread {row['total_relative_range_percent']:.1f}%"
        )
        lines.append(
            f"| {stamp} | {commit} | {row['samples']} | "
            f"{row['stage_seconds']['TOTAL']:.2f} | {note} |\n"
        )
    return lines


def append_benchmark_rows(log: Path, rows: list[str]) -> None:
    """Insert pipeline rows immediately before the stable end-of-file marker."""
    if not log.exists():
        log.write_text(
            "# StepWise benchmarks\n\n"
            "## Pipeline measurements\n\n"
            "| date | commit | samples | total s | note |\n"
            "|---|---|---:|---:|---|\n"
            + BENCHMARK_APPEND_MARKER,
            encoding="utf-8",
        )
    content = log.read_text(encoding="utf-8")
    if content.count(BENCHMARK_APPEND_MARKER) != 1:
        raise RuntimeError("BENCHMARKS.md must contain exactly one append marker")
    insertion = "".join(rows)
    content = content.replace(BENCHMARK_APPEND_MARKER, insertion + BENCHMARK_APPEND_MARKER)
    log.write_text(content, encoding="utf-8")


class Tee(io.TextIOBase):
    def __init__(self, *streams: io.TextIOBase) -> None:
        self.streams = streams

    def write(self, text: str) -> int:
        for stream in self.streams:
            stream.write(text)
            stream.flush()
        return len(text)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def run(args: argparse.Namespace, stamp: str) -> None:
    runs: list[list[dict]] = [[] for _ in range(args.repeat)]
    for size in args.sizes:
        for warmup_index in range(args.warmup):
            bench_one(size, ROOT)
            print(
                f"\nDiscarded warmup {warmup_index + 1}/{args.warmup} "
                f"for {size} samples"
            )
        for run_index in range(args.repeat):
            runs[run_index].append(bench_one(size, ROOT))

    for run_index, rows in enumerate(runs):
        if args.repeat > 1:
            print(f"\nRaw run {run_index + 1}/{args.repeat}")
        print_table(rows)

    summaries = summarize_runs(runs)
    if args.repeat > 1:
        print("\nMedian summary")
        print_table(summaries)

    if args.compare:
        print()
        compare(max(args.sizes), ROOT)

    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "commit": git_commit(),
        "machine": {
            "platform": platform.platform(),
            "processor": platform.processor() or platform.machine(),
            "python": platform.python_version(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
        },
        "repeat": args.repeat,
        "warmup": args.warmup,
        "runs": [
            {"run": run_index + 1, "results": rows}
            for run_index, rows in enumerate(runs)
        ],
        "summary": summaries,
        # Preserve the original single-run results key for --repeat 1 callers.
        "results": runs[0] if args.repeat == 1 else summaries,
    }
    target = ROOT / f"baseline-{stamp}.json"
    target.write_text(json.dumps(record, indent=2), encoding="utf-8")

    log = Path("BENCHMARKS.md")
    append_benchmark_rows(
        log,
        benchmark_markdown_rows(
            stamp, record["commit"], summaries, args.repeat, args.warmup
        ),
    )

    print(f"\nwrote {target} and appended {len(summaries)} row(s) to BENCHMARKS.md")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", type=int, nargs="+",
                        default=[1682, 6000, 30000, 180000, 360000])
    parser.add_argument("--repeat", type=int, default=3,
                        help="number of raw measurements per sample size (default: 3)")
    parser.add_argument("--warmup", type=int, default=1,
                        help="discarded full runs per sample size (default: 1)")
    parser.add_argument("--compare", action="store_true",
                        help="also run the optimisation prototypes on the largest size")
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.warmup < 0:
        parser.error("--warmup must be at least 0")
    return args


def main() -> None:
    args = parse_args()

    ROOT.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    if args.compare:
        stdout_target = ROOT / f"compare-{stamp}.txt"
        with stdout_target.open("w", encoding="utf-8") as stdout_file:
            stdout_file.write(COMPARE_NOTICE + "\n")
            with contextlib.redirect_stdout(Tee(sys.stdout, stdout_file)):
                run(args, stamp)
    else:
        run(args, stamp)


if __name__ == "__main__":
    main()
