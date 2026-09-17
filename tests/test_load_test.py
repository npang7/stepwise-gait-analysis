from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psutil
import pytest

from bench.load_test import (
    CalibrationMonitor,
    GiB,
    MeasurementState,
    MiB,
    ResourceSample,
    UnsafeStoragePath,
    _service_path_budget,
    _status_poll_retry_allowed,
    calibration_threshold,
    classify_resource_gate,
    compare_drift,
    decide_memory_subsets,
    effective_concurrency,
    generator_cpu_statistics,
    generator_is_limited,
    latency_statistics,
    memory_inventory,
    read_available_memory_samples,
    safe_move_tree,
    safe_rmtree,
    select_work_roots,
    stable_result_digest,
    start_service,
    stop_service,
    summarize_repetition_quantiles,
    validate_storage_root,
    write_rejection_evidence,
)


def _result(metric: float) -> dict:
    return {
        "summary": {"samples": 2, "duration_s": 1.0},
        "metrics": {"metric": metric, "missing": None},
        "risk_cards": [],
        "artifacts": [
            {
                "name": "gait_steps_analysis.csv",
                "media_type": "text/csv",
                "size_bytes": 10,
            }
        ],
    }


def test_stable_result_digest_normalizes_floats_without_rounding() -> None:
    csv_text = "StartTime_s,Pattern\n0.1,Neutral\n,Unknown\n"

    digest = stable_result_digest(_result(0.1), csv_text)
    reordered = {
        "artifacts": _result(0.1)["artifacts"],
        "risk_cards": [],
        "metrics": {"missing": None, "metric": 0.1},
        "summary": {"duration_s": 1.0, "samples": 2},
    }

    assert digest == stable_result_digest(reordered, csv_text)
    assert digest != stable_result_digest(_result(0.10000000000000002), csv_text)

    expected_float = 0.1.hex()
    payload = json.dumps(
        {
            "result": {
                "artifacts": _result(0.1)["artifacts"],
                "metrics": {"metric": expected_float, "missing": None},
                "risk_cards": [],
                "summary": {"duration_s": 1.0.hex(), "samples": 2},
            },
            "steps": {
                "columns": ["StartTime_s", "Pattern"],
                "rows": [[0.1.hex(), "Neutral"], [None, "Unknown"]],
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    # Recompute from the documented canonical payload so a future rounding change fails loudly.
    assert digest == hashlib.sha256(payload).hexdigest()


def test_stable_result_digest_rejects_non_finite_values() -> None:
    with pytest.raises(ValueError, match="finite"):
        stable_result_digest(_result(float("nan")), "Value,Pattern\n1.0,Neutral\n")


def test_latency_statistics_never_emits_p99() -> None:
    large = latency_statistics([float(value) for value in range(1, 101)])
    small = latency_statistics([float(value) for value in range(1, 100)])

    assert large == {"n": 100, "p50": 50.0, "p95": 95.0, "quantile": "p95"}
    assert small == {
        "n": 99,
        "p50": 50.0,
        "p90": 90.0,
        "max": 99.0,
        "quantile": "p90+max",
    }
    assert "p99" not in large
    assert "p99" not in small


def test_mixed_repetition_quantiles_are_not_merged() -> None:
    summary = summarize_repetition_quantiles(
        [
            latency_statistics([float(value) for value in range(100)]),
            latency_statistics([float(value) for value in range(99)]),
            latency_statistics([float(value) for value in range(101)]),
        ]
    )

    assert summary == "mixed; see repetitions"


def test_effective_concurrency_is_time_weighted_and_clipped() -> None:
    intervals = [(-1.0, 3.0), (1.0, 2.0), (4.0, 7.0)]
    assert effective_concurrency(intervals, window_start=0.0, window_end=5.0) == pytest.approx(
        1.0
    )


def test_generator_limit_uses_single_core_and_ten_second_window() -> None:
    assert generator_is_limited([81.0] * 10)
    assert not generator_is_limited([81.0] * 5 + [0.0] * 5)
    assert not generator_is_limited([79.9] * 20)
    stats = generator_cpu_statistics([float(value) for value in range(1, 101)])
    assert stats == {
        "n": 100,
        "p50": 50.0,
        "p95": 95.0,
        "max": 100.0,
        "rolling_10_second_median_max": 95.5,
    }


def test_resource_gate_uses_available_memory_pagefile_growth_and_disk_floor() -> None:
    samples = [
        ResourceSample(
            monotonic_s=0.0,
            available_memory_bytes=9 * GiB,
            pagefile_used_bytes=4 * GiB,
            disk_free_bytes=30 * GiB,
        ),
        ResourceSample(
            monotonic_s=1.0,
            available_memory_bytes=1 * GiB,
            pagefile_used_bytes=4 * GiB + 201 * MiB,
            disk_free_bytes=30 * GiB,
        ),
    ]
    gate = classify_resource_gate(samples)
    assert gate["memory_pressure"]
    assert gate["available_memory_below_2_gib"]
    assert gate["pagefile_growth_over_200_mib"]
    assert not gate["disk_below_20_gib"]

    disk_gate = classify_resource_gate(
        [
            ResourceSample(
                monotonic_s=0.0,
                available_memory_bytes=9 * GiB,
                pagefile_used_bytes=4 * GiB,
                disk_free_bytes=19 * GiB,
            )
        ]
    )
    assert disk_gate["abort_suite"]


def test_calibration_uses_10ms_for_upload_and_50ms_otherwise() -> None:
    monitor = object.__new__(CalibrationMonitor)
    monitor.phase = "upload"
    assert monitor.interval_seconds == 0.010
    monitor.phase = "analysis"
    assert monitor.interval_seconds == 0.050


def test_dual_memory_thresholds_use_separate_generator_estimates() -> None:
    calibration_30k = {
        "generator_baseline_rss_bytes": 100,
        "generator_upload_increment_peak_bytes": 30,
        "input_bytes": 20,
        "worker_peak_rss_bytes": 200,
        "parent_component_peak_rss_bytes": 300,
    }
    calibration_360k = {
        "generator_baseline_rss_bytes": 110,
        "generator_upload_increment_peak_bytes": 900,
        "input_bytes": 800,
        "worker_peak_rss_bytes": 2_000,
        "parent_component_peak_rss_bytes": 350,
    }
    threshold_30k = calibration_threshold(calibration_30k, 20)
    threshold_360k = calibration_threshold(calibration_360k, 2)

    assert threshold_30k["generator_scenario_peak_rss_bytes"] == 700
    assert threshold_360k["generator_scenario_peak_rss_bytes"] == 1_910
    assert threshold_30k["threshold_bytes"] == 400 + 300 + 700 + 2 * GiB
    assert threshold_360k["threshold_bytes"] == 4_000 + 350 + 1_910 + 2 * GiB

    only_30k = decide_memory_subsets(
        threshold_30k["threshold_bytes"], threshold_30k, threshold_360k
    )
    assert only_30k["run_30k"]
    assert not only_30k["run_360k"]
    assert only_30k["any_subset"]
    neither = decide_memory_subsets(1, threshold_30k, threshold_360k)
    assert not neither["any_subset"]


def test_available_memory_samples_are_one_second_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    values = iter((9, 8, 7))
    sleeps: list[float] = []

    monkeypatch.setattr(
        "bench.load_test.psutil.virtual_memory",
        lambda: SimpleNamespace(available=next(values)),
    )

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("bench.load_test.asyncio.sleep", fake_sleep)
    samples = asyncio.run(read_available_memory_samples())

    assert [row["available_bytes"] for row in samples] == [9, 8, 7]
    assert sleeps == [1.0, 1.0]


def test_memory_inventory_keeps_basic_rows_when_uss_is_denied() -> None:
    inventory = memory_inventory(limit=20)
    assert inventory["process_count"] >= len(inventory["top_processes"])
    assert len(inventory["top_processes"]) <= 20
    assert all(
        {"pid", "name", "rss_bytes", "private_bytes", "uss_bytes"} <= set(row)
        for row in inventory["top_processes"]
    )
    assert inventory["top_pid_order_by_private"]
    assert "System/cache/kernel" in inventory["note"]


def test_drift_comparison_uses_inclusive_a1_range_and_reports_amplitude() -> None:
    assert not compare_drift([10.0, 12.0, 11.0], 10.0)["drift_detected"]
    drift = compare_drift([10.0, 12.0, 11.0], 13.0)
    assert drift["drift_detected"]
    assert drift["signed_change_from_median_percent"] == pytest.approx(18.18181818)
    assert drift["outside_boundary_absolute"] == pytest.approx(1.0)
    assert drift["outside_boundary_percent_of_median"] == pytest.approx(9.09090909)


def test_brief_contains_in_place_errata_and_revision_record() -> None:
    brief = (Path(__file__).resolve().parents[1] / "docs" / "LOAD_TEST_BRIEF.md").read_text(
        encoding="utf-8"
    )
    instructions = brief.split("## 12. 修订记录", maxsplit=1)[0]
    assert "p99" not in instructions
    assert "8 GiB" not in instructions
    assert "threshold_30k" in instructions and "threshold_360k" in instructions
    assert "mixed; see repetitions" in brief
    assert "300 s" in brief
    assert "修订记录" in brief
    assert "有效并发" in brief
    assert "每个 repetition" in brief and "全新服务进程" in brief


def test_each_repetition_uses_a_fresh_service_process(tmp_path: Path) -> None:
    async def exercise() -> tuple[tuple[int, float], tuple[int, float]]:
        identities: list[tuple[int, float]] = []
        for index in range(2):
            service = await start_service(
                data_root=tmp_path / f"data-{index}",
                log_path=tmp_path / f"service-{index}.log",
            )
            identities.append((service.pid, service.create_time))
            await stop_service(service)
            assert service.stopped_at is not None
            assert not psutil.pid_exists(service.pid)
        return identities[0], identities[1]

    first, second = asyncio.run(exercise())
    assert first != second


def test_safe_rmtree_uses_path_components_not_sibling_prefix(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    work_root = tmp_path / "swlt"
    sibling = tmp_path / "swlt-rejected"
    target = work_root / "session" / "scenario"
    repository.mkdir()
    target.mkdir(parents=True)
    sibling.mkdir()

    with patch("bench.load_test.shutil.rmtree") as remove:
        safe_rmtree(target, work_root, repository)
        remove.assert_called_once_with(target.resolve())

        with pytest.raises(UnsafeStoragePath, match="strict descendant"):
            safe_rmtree(sibling, work_root, repository)
        assert remove.call_count == 1


def test_safe_rmtree_rejects_root_home_repository_and_resolution_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    work_root = tmp_path / "swlt"
    target = work_root / "target"
    repository.mkdir()
    target.mkdir(parents=True)

    with patch("bench.load_test.shutil.rmtree") as remove:
        with pytest.raises(UnsafeStoragePath, match="work root itself"):
            safe_rmtree(work_root, work_root, repository)
        with pytest.raises(UnsafeStoragePath, match="repository"):
            validate_storage_root(repository, repository)
        with pytest.raises(UnsafeStoragePath, match="home"):
            validate_storage_root(Path.home(), repository)
        with pytest.raises(UnsafeStoragePath, match="drive root"):
            validate_storage_root(Path(Path.cwd().anchor), repository)

        def fail_resolve(_path: Path) -> Path:
            raise OSError("simulated path composition/resolve failure")

        monkeypatch.setattr("bench.load_test._strict_resolve", fail_resolve)
        with pytest.raises(UnsafeStoragePath, match="resolve"):
            safe_rmtree(target, work_root, repository)
        remove.assert_not_called()


def test_safe_rmtree_rejects_resolved_junction_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    work_root = tmp_path / "swlt"
    target = work_root / "junction" / "scenario"
    outside = tmp_path / "outside" / "scenario"
    repository.mkdir()
    target.mkdir(parents=True)
    outside.mkdir(parents=True)
    real_resolve = Path.resolve

    def junction_resolve(path: Path) -> Path:
        if path == target:
            return real_resolve(outside, strict=True)
        return real_resolve(path, strict=True)

    monkeypatch.setattr("bench.load_test._strict_resolve", junction_resolve)
    with patch("bench.load_test.shutil.rmtree") as remove:
        with pytest.raises(UnsafeStoragePath, match="strict descendant"):
            safe_rmtree(target, work_root, repository)
        remove.assert_not_called()


def test_safe_move_tree_checks_both_component_boundaries(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    work_root = tmp_path / "swlt"
    rejected_root = tmp_path / "swlt-rejected"
    source = work_root / "session" / "scenario"
    destination = rejected_root / "scenario"
    repository.mkdir()
    source.mkdir(parents=True)
    rejected_root.mkdir()

    moved = safe_move_tree(source, destination, work_root, rejected_root, repository)
    assert moved == destination.resolve()
    assert moved.is_dir()
    assert not source.exists()

    bad_source = rejected_root / "sibling-source"
    bad_source.mkdir()
    with pytest.raises(UnsafeStoragePath, match="strict descendant"):
        safe_move_tree(bad_source, rejected_root / "other", work_root, rejected_root, repository)


def test_work_root_selection_falls_back_and_recomputes_path_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    first = (tmp_path / "blocked" / "swlt", tmp_path / "blocked" / "swlt-rejected")
    second = (tmp_path / "available" / "swlt", tmp_path / "available" / "swlt-rejected")
    module = __import__("bench.load_test", fromlist=["_prepare_root_pair"])
    real_probe = module._prepare_root_pair

    def probe(work: Path, rejected: Path, repo: Path) -> tuple[Path, Path]:
        if work == first[0]:
            raise PermissionError("simulated permission denial")
        return real_probe(work, rejected, repo)

    monkeypatch.setattr("bench.load_test._prepare_root_pair", probe)
    selection = select_work_roots(repository, candidates=[first, second])

    assert selection.work_root == second[0].resolve()
    assert selection.rejected_root == second[1].resolve()
    assert selection.attempts[0]["usable"] is False
    assert selection.attempts[1]["usable"] is True
    assert selection.path_budget["max_chars"] == _service_path_budget(
        selection.work_root / "s-12345678" / "c360-2-3-12345678" / "data"
    )["max_chars"]


def test_work_root_selection_keeps_220_character_limit(tmp_path: Path) -> None:
    short = _service_path_budget(tmp_path / "swlt" / "s-12345678" / "c30-1-1-a1b2" / "data")
    long = _service_path_budget(
        tmp_path / ("x" * 120) / "swlt" / "s-12345678" / "c30-1-1-a1b2" / "data"
    )
    assert short["limit_chars"] == 220
    assert long["limit_chars"] == 220
    assert long["max_chars"] > short["max_chars"]


def test_rejection_evidence_keeps_small_files_and_external_index(tmp_path: Path) -> None:
    repository_rejected = tmp_path / "repository" / "bench-data" / "rejected"
    stdout = tmp_path / "stdout.log"
    service_log = tmp_path / "service.log"
    external = tmp_path / "swlt-rejected" / "c30-20-1-a1b2"
    stdout.write_text("complete harness output\n", encoding="utf-8")
    service_log.write_text("prefix\nTraceback (most recent call last):\nboom\n", encoding="utf-8")
    external.mkdir(parents=True)

    evidence = write_rejection_evidence(
        short_id="c30-20-1-a1b2",
        full_label="n30000-c20-r1",
        aggregate={"citable": False, "invalid_reasons": ["memory-pressure"]},
        repository_rejected_root=repository_rejected,
        external_artifact_root=external,
        stdout_path=stdout,
        service_log_paths=[service_log],
    )

    evidence_path = Path(evidence["repository_evidence_path"])
    index = json.loads((evidence_path / "index.json").read_text(encoding="utf-8"))
    assert (evidence_path / "aggregate.json").is_file()
    assert (evidence_path / "stdout.log").read_text(encoding="utf-8") == stdout.read_text(
        encoding="utf-8"
    )
    assert "Traceback" in (evidence_path / "traceback.txt").read_text(encoding="utf-8")
    assert index["external_artifact_root"] == str(external.resolve())
    assert index["files"]["aggregate.json"]["sha256"]
    assert index["files"]["stdout.log"]["sha256"]


def test_status_poll_500_is_recorded_and_bounded_without_becoming_fatal() -> None:
    state = MeasurementState(window_seconds=120.0, target=200)
    state.status_poll_anomalies.append(
        {
            "run_id": "00000000-0000-0000-0000-000000000000",
            "status_code": 500,
            "response": "Internal Server Error",
        }
    )

    assert _status_poll_retry_allowed(500, 1)
    assert not _status_poll_retry_allowed(500, 21)
    assert not _status_poll_retry_allowed(404, 1)
    assert state.fatal_error is None
    assert state.status_poll_anomalies[0]["status_code"] == 500
