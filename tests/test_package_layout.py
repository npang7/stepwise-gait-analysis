from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PackageLayoutTests(unittest.TestCase):
    def test_tests_directory_is_an_importable_package(self) -> None:
        self.assertTrue(
            (ROOT / "tests" / "__init__.py").is_file(),
            "tests/__init__.py is required for portable test-helper imports",
        )

    def test_modular_package_layout_exists(self) -> None:
        expected = [
            ROOT / "pyproject.toml",
            ROOT / "src" / "stepwise" / "__init__.py",
            ROOT / "src" / "stepwise" / "models.py",
            ROOT / "src" / "stepwise" / "parsing.py",
            ROOT / "src" / "stepwise" / "signal.py",
            ROOT / "src" / "stepwise" / "features.py",
            ROOT / "src" / "stepwise" / "screening.py",
            ROOT / "src" / "stepwise" / "reporting.py",
            ROOT / "src" / "stepwise" / "service.py",
            ROOT / "src" / "stepwise" / "storage.py",
            ROOT / "src" / "stepwise" / "jobs.py",
            ROOT / "src" / "stepwise" / "api.py",
            ROOT / "src" / "stepwise" / "cli.py",
        ]
        missing = [path.relative_to(ROOT).as_posix() for path in expected if not path.is_file()]
        self.assertEqual(missing, [], f"missing package files: {missing}")

    def test_dependency_matrix_lowest_versions_match_declared_lower_bounds(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        lowest = re.search(
            r"boundary: lowest\s+numpy_spec: \"numpy==([^\"]+)\"\s+"
            r"pandas_spec: \"pandas==([^\"]+)\"",
            workflow,
        )
        self.assertIsNotNone(lowest, "dependency-bounds lowest matrix entry is missing")

        def lower_bound(package: str) -> tuple[int, ...]:
            requirement = next(
                dependency
                for dependency in project["dependencies"]
                if dependency.startswith(package)
            )
            match = re.search(r">=([0-9.]+)", requirement)
            self.assertIsNotNone(match, f"{package} lower bound is missing")
            return tuple(int(part) for part in match.group(1).split("."))

        def normalized(version: str) -> tuple[int, ...]:
            return tuple(int(part) for part in version.split("."))

        assert lowest is not None
        numpy_lowest, pandas_lowest = lowest.groups()
        self.assertEqual(normalized(numpy_lowest)[:2], lower_bound("numpy")[:2])
        self.assertEqual(normalized(pandas_lowest)[:2], lower_bound("pandas")[:2])


if __name__ == "__main__":
    unittest.main()
