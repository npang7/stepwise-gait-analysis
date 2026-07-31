from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("STEPWISE_BATCH_DATA_DIR", ROOT / "sample_data"))
OUTPUT_ROOT = Path(os.environ.get("STEPWISE_OUTPUT_DIR", ROOT / "stepwise_batch_reports"))
ANALYZER = ROOT / "stepwise_gait_analysis.py"

FILES = [
    ("normal", "正常.txt"),
    ("toe_in", "内八.txt"),
    ("toe_out", "外八.txt"),
    ("inversion", "足内翻.txt"),
    ("eversion", "足外翻.txt"),
    ("weak_pushoff", "推蹬不足.txt"),
    ("forefoot_landing", "前掌着地.txt"),
    ("rearfoot_landing", "后跟着地.txt"),
    ("unloaded_standing", "空载站立.txt"),
]


def main() -> None:
    if not ANALYZER.exists():
        raise FileNotFoundError(f"Cannot find analyzer: {ANALYZER.resolve()}")

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    for folder_name, filename in FILES:
        input_path = DATA_DIR / filename
        if not input_path.exists():
            raise FileNotFoundError(f"Cannot find input: {input_path}")

        output_dir = OUTPUT_ROOT / folder_name
        output_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n=== Running {filename} -> {output_dir} ===")
        subprocess.run(
            [
                sys.executable,
                str(ANALYZER),
                str(input_path),
                "--output-dir",
                str(output_dir),
            ],
            check=True,
        )

    print(f"\nAll StepWise HTML reports generated under: {OUTPUT_ROOT.resolve()}")


if __name__ == "__main__":
    main()
