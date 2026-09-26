from __future__ import annotations

import argparse
import json

import pytest

import bench_stepwise


def _row(samples: int, total: float, parse: float | None = None) -> dict:
    stages = {stage: 0.0 for stage in bench_stepwise.STAGES}
    stages["parse"] = total if parse is None else parse
    stages["TOTAL"] = total
    return {
        "samples": samples,
        "duration_s": samples / 100.0,
        "input_mb": 1.0,
        "stance_phases": 2,
        "stage_seconds": stages,
    }


def test_single_repeat_preserves_raw_measurement() -> None:
    raw = [[_row(100, 1.25)]]

    summary = bench_stepwise.summarize_runs(raw)

    assert summary[0]["stage_seconds"]["TOTAL"] == 1.25
    assert summary[0]["raw_total_seconds"] == [1.25]
    assert summary[0]["total_relative_range_percent"] == 0.0


def test_three_repeats_report_median_and_relative_range() -> None:
    runs = [[_row(100, value)] for value in (8.0, 10.0, 11.0)]

    summary = bench_stepwise.summarize_runs(runs)

    assert summary[0]["stage_seconds"]["TOTAL"] == 10.0
    assert summary[0]["raw_total_seconds"] == [8.0, 10.0, 11.0]
    assert summary[0]["total_relative_range_percent"] == 30.0


def test_markdown_has_one_median_row_per_size() -> None:
    runs = [
        [_row(100, 8.0), _row(200, 18.0)],
        [_row(100, 10.0), _row(200, 20.0)],
        [_row(100, 11.0), _row(200, 22.0)],
    ]
    summary = bench_stepwise.summarize_runs(runs)

    lines = bench_stepwise.benchmark_markdown_rows("STAMP", "abc123", summary, 3, 1)

    assert len(lines) == 2
    assert "| 100 | 10.00 | median of 3 after 1 warmup, spread 30.0% |" in lines[0]
    assert "| 200 | 20.00 | median of 3 after 1 warmup, spread 20.0% |" in lines[1]


def test_marker_append_keeps_pipeline_table_last_and_preserves_phase_one(tmp_path) -> None:
    log = tmp_path / "BENCHMARKS.md"
    phase_one = "## Phase 1 upload measurements\n\n| upload | value |\n|---|---:|\n| rss | 1 |\n\n"
    historical_note = "> Historical TOTAL rows are not directly comparable.\n\n"
    pipeline = (
        "## Back-to-back pipeline measurements (section 5.3 onward)\n\n"
        "| date | commit | samples | total s | note |\n"
        "|---|---|---:|---:|---|\n"
        "| OLD | abc | 100 | 1.00 | baseline |\n"
    )
    log.write_text(
        "# StepWise benchmarks\n\n"
        + phase_one
        + historical_note
        + pipeline
        + bench_stepwise.BENCHMARK_APPEND_MARKER,
        encoding="utf-8",
    )

    bench_stepwise.append_benchmark_rows(log, ["| NEW1 | def | 100 | 0.50 | first |\n"])
    bench_stepwise.append_benchmark_rows(log, ["| NEW2 | ghi | 100 | 0.40 | second |\n"])

    content = log.read_text(encoding="utf-8")
    assert content.count(phase_one) == 1
    assert content.count(historical_note) == 1
    assert content.index("## Phase 1 upload measurements") < content.index(
        "## Back-to-back pipeline measurements"
    )
    assert content.index("| OLD |") < content.index("| NEW1 |") < content.index("| NEW2 |")
    assert content.endswith(bench_stepwise.BENCHMARK_APPEND_MARKER)
    assert content.count(bench_stepwise.BENCHMARK_APPEND_MARKER) == 1


def test_run_discards_one_warmup_per_size_and_records_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    calls: list[int] = []

    def fake_bench_one(samples: int, _root) -> dict:
        calls.append(samples)
        return _row(samples, float(len(calls)))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bench_stepwise, "ROOT", tmp_path / "bench-data")
    monkeypatch.setattr(bench_stepwise, "bench_one", fake_bench_one)
    bench_stepwise.ROOT.mkdir()
    args = argparse.Namespace(sizes=[100, 200], repeat=2, warmup=1, compare=False)

    bench_stepwise.run(args, "STAMP")

    assert calls == [100, 100, 100, 200, 200, 200]
    record = json.loads((tmp_path / "bench-data" / "baseline-STAMP.json").read_text())
    assert record["warmup"] == 1
    assert [row["stage_seconds"]["TOTAL"] for row in record["runs"][0]["results"]] == [2.0, 5.0]
    assert [row["stage_seconds"]["TOTAL"] for row in record["runs"][1]["results"]] == [3.0, 6.0]


def test_zero_warmup_preserves_one_measurement(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    calls: list[int] = []

    def fake_bench_one(samples: int, _root) -> dict:
        calls.append(samples)
        return _row(samples, 1.0)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bench_stepwise, "ROOT", tmp_path / "bench-data")
    monkeypatch.setattr(bench_stepwise, "bench_one", fake_bench_one)
    bench_stepwise.ROOT.mkdir()
    args = argparse.Namespace(sizes=[100], repeat=1, warmup=0, compare=False)

    bench_stepwise.run(args, "STAMP")

    assert calls == [100]
    record = json.loads((tmp_path / "bench-data" / "baseline-STAMP.json").read_text())
    assert record["warmup"] == 0
    assert record["results"] == record["runs"][0]["results"]


def test_parser_defaults_to_one_warmup() -> None:
    args = bench_stepwise.parse_args([])

    assert args.warmup == 1


def test_parser_rejects_negative_warmup() -> None:
    with pytest.raises(SystemExit) as error:
        bench_stepwise.parse_args(["--warmup", "-1"])

    assert error.value.code == 2


def test_compare_notice_requires_phase_two_remeasurement() -> None:
    assert "DIRECTIONAL REFERENCE ONLY" in bench_stepwise.COMPARE_NOTICE
    assert "variance is unknown" in bench_stepwise.COMPARE_NOTICE
    assert "Phase 2" in bench_stepwise.COMPARE_NOTICE
    assert "--repeat 3" in bench_stepwise.COMPARE_NOTICE
