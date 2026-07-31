from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RepositoryHygieneTests(unittest.TestCase):
    def test_resume_ready_repository_files_exist(self) -> None:
        for name in (
            "README.md",
            "pyproject.toml",
            "Dockerfile",
            ".dockerignore",
            ".gitignore",
            "package.json",
            "docs/INTERVIEW_GUIDE_PRIVATE.md",
            ".github/workflows/ci.yml",
        ):
            with self.subTest(name=name):
                self.assertTrue((ROOT / name).is_file())

    def test_deployment_uses_canonical_analysis_sources(self) -> None:
        self.assertFalse((ROOT / "app.py").exists())
        self.assertFalse((ROOT / "stepwise_cloudrun_flask").exists())
        self.assertFalse((ROOT / "stepwise_gait_analysis.py").exists())
        self.assertFalse((ROOT / "stepwise_reference_pipeline.py").exists())
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY src ./src", dockerfile)
        self.assertIn("USER stepwise", dockerfile)
        self.assertIn("stepwise.api:create_app", dockerfile)
        self.assertIn('"--factory"', dockerfile)

    def test_legacy_course_materials_are_archived_not_discarded(self) -> None:
        archive = ROOT / "archive" / "course-materials"
        for name in (
            "legacy-prototype/stepwise_gait_analysis.py",
            "legacy-prototype/stepwise_reference_pipeline.py",
            "legacy-services/app.py",
            "StepWise_lab_notebook_software_part.tex",
            "ece445_guidelines_extracted.txt",
        ):
            with self.subTest(name=name):
                self.assertTrue((archive / name).is_file())

    def test_ci_runs_python_quality_gates_node_tests_and_docker_build(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        for token in (
            'python-version: ["3.11", "3.12"]',
            "ruff check src tests",
            "mypy src/stepwise",
            "pytest --cov=stepwise",
            "node --test tests/js/*.test.js",
            "docker build",
        ):
            with self.subTest(token=token):
                self.assertIn(token, workflow)

    def test_source_files_have_no_teammate_machine_paths(self) -> None:
        candidates = [
            *ROOT.glob("src/**/*.py"),
            *ROOT.glob("stepwise_miniprogram/**/*.js"),
        ]
        combined = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in candidates)
        self.assertNotIn("Xiaorui Zhang", combined)
        self.assertNotIn(r"C:\Users\Xiaorui", combined)

    def test_worker_logging_does_not_emit_tracebacks_with_local_paths(self) -> None:
        jobs_source = (ROOT / "src" / "stepwise" / "jobs.py").read_text(encoding="utf-8")
        self.assertNotIn("LOGGER.exception", jobs_source)

    def test_mini_program_configuration_has_no_stale_endpoint(self) -> None:
        app_js = (ROOT / "stepwise_miniprogram" / "app.js").read_text(encoding="utf-8")
        self.assertIn("require('./config')", app_js)
        self.assertNotIn("flask-8rw2-259045-6-1434112352", app_js)
        self.assertTrue((ROOT / "stepwise_miniprogram" / "config.js").is_file())


if __name__ == "__main__":
    unittest.main()
