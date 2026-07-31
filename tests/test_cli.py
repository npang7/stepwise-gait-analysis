from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stepwise.cli import main

FIXTURE = ROOT / "tests" / "fixtures" / "minimal_walk.txt"


class CliTests(unittest.TestCase):
    def test_analyze_writes_the_standard_result_and_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "analysis"
            with redirect_stdout(StringIO()) as stdout:
                code = main(["analyze", str(FIXTURE), "--output", str(output)])
            payload = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["summary"]["samples"], 20)
            self.assertTrue((output / "reference_screening_result.json").is_file())

    def test_input_error_returns_two_and_runtime_error_returns_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with redirect_stderr(StringIO()) as stderr:
                input_code = main(
                    ["analyze", str(root / "missing.txt"), "--output", str(root / "out")]
                )
            self.assertEqual(input_code, 2)
            self.assertIn("missing_input", stderr.getvalue())

            output_file = root / "not-a-directory"
            output_file.write_text("occupied", encoding="utf-8")
            with redirect_stderr(StringIO()) as stderr:
                runtime_code = main(
                    ["analyze", str(FIXTURE), "--output", str(output_file)]
                )
            self.assertEqual(runtime_code, 1)
            self.assertIn("analysis_failed", stderr.getvalue())

    def test_batch_uses_one_output_directory_per_recording(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            inputs = root / "inputs"
            inputs.mkdir()
            shutil.copyfile(FIXTURE, inputs / "trial-a.txt")
            shutil.copyfile(FIXTURE, inputs / "trial-b.txt")
            output = root / "batch"
            with redirect_stdout(StringIO()):
                code = main(["batch", str(inputs), "--output", str(output)])
            summary = json.loads((output / "batch_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(code, 0)
            self.assertEqual([item["name"] for item in summary["analyses"]], ["trial-a", "trial-b"])
            self.assertTrue((output / "trial-a" / "session_summary.json").is_file())
            self.assertTrue((output / "trial-b" / "session_summary.json").is_file())


if __name__ == "__main__":
    unittest.main()
