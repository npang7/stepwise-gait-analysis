from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path, PureWindowsPath

ROOT = Path(__file__).resolve().parents[1]
WINDOWS_USER_PROFILE_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9_])[A-Za-z]:(?:\\+|/)Users(?:\\+|/)[^\\/\s\"']+"
)
EMAIL_PATTERN = re.compile(
    r"(?i)\b[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+\b"
)
SYNTHETIC_EMAIL_DOMAINS = frozenset({"example.com", "example.org", "example.net"})


def _privacy_violations(text: str) -> list[str]:
    violations = [
        "local Windows user-profile path"
        for _match in WINDOWS_USER_PROFILE_PATTERN.finditer(text)
    ]
    violations.extend(
        "non-synthetic email address"
        for match in EMAIL_PATTERN.finditer(text)
        if match.group(0).rsplit("@", 1)[1].lower() not in SYNTHETIC_EMAIL_DOMAINS
    )
    return violations


def _synthetic_email(local_part: str, domain: str) -> str:
    return f"{local_part}{chr(64)}{domain}"


def _tracked_repository_files() -> tuple[Path, ...]:
    output = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    tracked = (ROOT / path.decode("utf-8") for path in output.split(b"\0") if path)
    return tuple(path for path in tracked if path.is_file())


class RepositoryHygieneTests(unittest.TestCase):
    def test_privacy_guard_rejects_synthetic_identity_patterns(self) -> None:
        local_path = str(
            PureWindowsPath("C:/", "Users", "fixture-account", "workspace", "source.py")
        )
        personal_email = _synthetic_email("fixture.account", "invalid.test")

        self.assertEqual(
            ["local Windows user-profile path", "non-synthetic email address"],
            _privacy_violations(f"{local_path}\n{personal_email}"),
        )

    def test_privacy_guard_allows_reserved_example_email(self) -> None:
        synthetic_email = _synthetic_email("fixture.account", "example.com")
        self.assertEqual([], _privacy_violations(synthetic_email))

    def test_required_repository_files_exist(self) -> None:
        for name in (
            "README.md",
            "pyproject.toml",
            "Dockerfile",
            ".dockerignore",
            ".gitattributes",
            ".gitignore",
            "package.json",
            ".github/workflows/ci.yml",
        ):
            with self.subTest(name=name):
                self.assertTrue((ROOT / name).is_file())

        self.assertFalse((ROOT / "docs" / "INTERVIEW_GUIDE_PRIVATE.md").exists())

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

    def test_ci_docker_job_runs_real_container_smoke_test(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        for token in (
            "docker run",
            "python -m stepwise.ci_smoke",
            "tests/fixtures/minimal_walk.txt",
            "docker logs stepwise-ci",
            "docker rm --force stepwise-ci",
            "if: always()",
        ):
            with self.subTest(token=token):
                self.assertIn(token, workflow)

    def test_readme_documents_remote_container_smoke_gate(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("python -m stepwise.ci_smoke", readme)
        self.assertIn("health, upload, polling, result, and artifact download", readme)

    def test_readme_documents_api_contract_and_evidence_commands(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for token in (
            "POST /api/v1/analyses",
            "GET /api/v1/analyses/{run_id}",
            "GET /api/v1/analyses/{run_id}/result",
            "GET /api/v1/analyses/{run_id}/artifacts/{name}",
            "GET /healthz",
            "upload_too_large",
            "invalid_mapping",
            "empty_input",
            "binary_input",
            "invalid_encoding",
            "no_data_rows",
            "invalid_timestamp",
            "queue_full",
            "result_not_ready",
            "analysis_not_found",
            "artifact_not_found",
            "job_supervisor_unavailable",
            "terminal_persistence_saturated",
            "artifact_generation_failed",
            "python -m bench.readme_metrics aba",
            "python -m bench.readme_metrics memory",
            "python -m bench.readme_metrics upload",
            "python -m bench.readme_metrics load",
        ):
            with self.subTest(token=token):
                self.assertIn(token, readme)

    def test_benchmark_evidence_is_declared_lf_text(self) -> None:
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertEqual(attributes.strip(), "bench-data/** text eol=lf")

    def test_all_tracked_files_have_no_local_paths_or_personal_emails(self) -> None:
        candidates = _tracked_repository_files()
        self.assertIn(ROOT / "tests" / "test_repository_hygiene.py", candidates)
        for path in candidates:
            with self.subTest(path=path.relative_to(ROOT)):
                text = path.read_bytes().decode("utf-8", errors="replace")
                violations = _privacy_violations(text)
                self.assertEqual(
                    [],
                    violations,
                    f"{path.relative_to(ROOT)}: {', '.join(violations)}",
                )

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
