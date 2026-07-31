from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PackageLayoutTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
