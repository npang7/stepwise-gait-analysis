from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bench.demo_assets import generate_assets

ROOT = Path(__file__).resolve().parents[1]


def test_showcase_summary_matches_actual_analysis(tmp_path: Path) -> None:
    actual = generate_assets(tmp_path)
    committed = json.loads((ROOT / "docs/assets/synthetic-summary.json").read_text())
    assert actual == committed
    assert actual["samples"] == 1682
    assert actual["duration_s"] == pytest.approx(16.81)
    assert actual["estimated_sample_rate_hz"] == pytest.approx(100)
    assert actual["detected_contact_intervals"] == 16
    assert all(start < end for start, end in actual["contact_intervals_s"])
    assert (tmp_path / "synthetic-pressure-stance.png").read_bytes().startswith(b"\x89PNG")


def test_documentation_relative_links_resolve() -> None:
    for document in [ROOT / "README.md", *ROOT.glob("docs/**/*.md")]:
        text = document.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)]+)\)", text):
            if "://" in target or target.startswith("#"):
                continue
            path = target.split("#", 1)[0]
            assert (document.parent / path).exists(), f"{document.name}: {target}"


def test_readme_quickstart_is_executed_in_ci() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for command in (
        "python -m venv .venv",
        ".venv/bin/python -m pip install -e .",
        (
            ".venv/bin/python -m stepwise analyze tests/fixtures/synthetic_1682.txt "
            "--output stepwise_output/demo"
        ),
    ):
        assert command in readme and command in workflow
