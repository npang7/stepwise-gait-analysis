from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stepwise.settings import Settings


class SettingsTests(unittest.TestCase):
    def test_defaults_match_the_public_service_contract(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.max_upload_bytes, 64 * 1024 * 1024)
        self.assertEqual(settings.analysis_timeout_seconds, 120.0)
        self.assertEqual(settings.max_workers, 2)
        self.assertEqual(settings.max_queue, 8)
        self.assertEqual(settings.result_ttl_hours, 24.0)

    def test_environment_names_override_every_setting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory, patch.dict(
            os.environ,
            {
                "STEPWISE_DATA_DIR": temporary_directory,
                "STEPWISE_MAX_UPLOAD_BYTES": "4096",
                "STEPWISE_ANALYSIS_TIMEOUT_SECONDS": "4.5",
                "STEPWISE_MAX_WORKERS": "3",
                "STEPWISE_MAX_QUEUE": "6",
                "STEPWISE_RESULT_TTL_HOURS": "12",
            },
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.data_dir, Path(temporary_directory).resolve())
        self.assertEqual(settings.max_upload_bytes, 4096)
        self.assertEqual(settings.analysis_timeout_seconds, 4.5)
        self.assertEqual(settings.max_workers, 3)
        self.assertEqual(settings.max_queue, 6)
        self.assertEqual(settings.result_ttl_hours, 12.0)


if __name__ == "__main__":
    unittest.main()
