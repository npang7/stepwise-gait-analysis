"""Command-line analyze and batch adapters."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from .models import AnalysisConfig, SensorMapping, strict_json_value
from .parsing import InputValidationError
from .service import AnalysisService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stepwise")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="analyze one walking recording")
    analyze.add_argument("walking", type=Path)
    analyze.add_argument("--standing", type=Path)
    analyze.add_argument("--output", type=Path, required=True)
    analyze.add_argument("--mapping-json", default="{}")

    batch = subparsers.add_parser("batch", help="analyze every TXT recording in a directory")
    batch.add_argument("input_dir", type=Path)
    batch.add_argument("--standing", type=Path)
    batch.add_argument("--output", type=Path, required=True)
    batch.add_argument("--mapping-json", default="{}")
    return parser


def _config(mapping_json: str) -> AnalysisConfig:
    payload = json.loads(mapping_json)
    if not isinstance(payload, dict):
        raise TypeError("mapping JSON must be an object")
    return AnalysisConfig(sensor_mapping=SensorMapping(**payload))


def _require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise InputValidationError("missing_input", f"{label} recording was not found")


def _print_json(payload: object, *, stream: TextIO | None = None) -> None:
    destination = stream or sys.stdout
    print(
        json.dumps(strict_json_value(payload), ensure_ascii=False, indent=2, allow_nan=False),
        file=destination,
    )


def _analyze(args: argparse.Namespace, config: AnalysisConfig) -> dict:
    _require_file(args.walking, "walking")
    if args.standing is not None:
        _require_file(args.standing, "standing")
    result = AnalysisService().analyze(
        args.walking,
        args.standing,
        args.output,
        config,
    )
    return result.to_dict()


def _batch(args: argparse.Namespace, config: AnalysisConfig) -> dict:
    if not args.input_dir.is_dir():
        raise InputValidationError("missing_input", "batch input directory was not found")
    if args.standing is not None:
        _require_file(args.standing, "standing")
    recordings = sorted(
        path
        for path in args.input_dir.glob("*.txt")
        if args.standing is None or path.resolve() != args.standing.resolve()
    )
    if not recordings:
        raise InputValidationError("no_input_files", "batch directory contains no TXT recordings")
    args.output.mkdir(parents=True, exist_ok=True)
    analyses = []
    for recording in recordings:
        result = AnalysisService().analyze(
            recording,
            args.standing,
            args.output / recording.stem,
            config,
        )
        analyses.append(
            {
                "name": recording.stem,
                "status": "succeeded",
                "summary": result.to_dict()["summary"],
            }
        )
    summary = {"count": len(analyses), "analyses": analyses}
    (args.output / "batch_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = _config(args.mapping_json)
        payload = _analyze(args, config) if args.command == "analyze" else _batch(args, config)
    except (InputValidationError, FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
        code = exc.code if isinstance(exc, InputValidationError) else "invalid_input"
        _print_json({"error": {"code": code, "message": str(exc)}}, stream=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 -- CLI boundary maps unexpected failures to exit code 1.
        _print_json(
            {
                "error": {
                    "code": "analysis_failed",
                    "message": "Analysis failed unexpectedly.",
                }
            },
            stream=sys.stderr,
        )
        return 1
    _print_json(payload)
    return 0
