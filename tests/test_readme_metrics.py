from __future__ import annotations

import unittest

from bench.readme_metrics import aba_metrics, load_metrics, memory_metrics, upload_metrics


class ReadmeMetricsTests(unittest.TestCase):
    def test_aba_metrics_match_committed_evidence(self) -> None:
        self.assertEqual(
            aba_metrics(),
            "total=21.4765->6.6074s control_difference=0.6564% "
            "artifacts=15.5545->2.7678s stance_features=2.6447->0.3403s "
            "parse=2.5756->2.7544s",
        )

    def test_memory_metrics_match_committed_evidence(self) -> None:
        self.assertEqual(memory_metrics(), "peak_rss=1.52GB->899MB")

    def test_upload_metrics_match_committed_evidence(self) -> None:
        self.assertEqual(
            upload_metrics(),
            "limit=64MiB samples=713,650 duration_at_100Hz=1.9824h",
        )

    def test_load_metrics_match_committed_evidence(self) -> None:
        self.assertEqual(
            load_metrics(),
            "workers=2 C10=45.0/min C20=31.5/min "
            "queue_share=74.8% (9.735/13.016s) consistent=2,111",
        )


if __name__ == "__main__":
    unittest.main()
