from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RepositoryHygieneTests(unittest.TestCase):
    def test_resume_ready_repository_files_exist(self) -> None:
        for name in ("README.md", "requirements.txt", ".gitignore"):
            with self.subTest(name=name):
                self.assertTrue((ROOT / name).is_file())

    def test_deployment_uses_canonical_analysis_sources(self) -> None:
        self.assertFalse((ROOT / "stepwise_cloudrun_flask" / "stepwise_gait_analysis.py").exists())
        self.assertFalse((ROOT / "stepwise_cloudrun_flask" / "stepwise_reference_pipeline.py").exists())
        dockerfile = (ROOT / "stepwise_cloudrun_flask" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY stepwise_gait_analysis.py", dockerfile)
        self.assertIn("COPY stepwise_reference_pipeline.py", dockerfile)

    def test_source_files_have_no_teammate_machine_paths(self) -> None:
        candidates = [
            *ROOT.glob("*.py"),
            *ROOT.glob("*.bat"),
            *ROOT.glob("*.tex"),
            *ROOT.glob("stepwise_cloudrun_flask/*.py"),
            *ROOT.glob("stepwise_miniprogram/**/*.js"),
        ]
        combined = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in candidates)
        self.assertNotIn("Xiaorui Zhang", combined)
        self.assertNotIn(r"C:\Users\Xiaorui", combined)

    def test_mini_program_configuration_has_no_stale_endpoint(self) -> None:
        app_js = (ROOT / "stepwise_miniprogram" / "app.js").read_text(encoding="utf-8")
        self.assertIn("require('./config')", app_js)
        self.assertNotIn("flask-8rw2-259045-6-1434112352", app_js)
        self.assertTrue((ROOT / "stepwise_miniprogram" / "config.js").is_file())


if __name__ == "__main__":
    unittest.main()
