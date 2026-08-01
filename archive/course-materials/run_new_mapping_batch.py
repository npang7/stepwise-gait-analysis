from __future__ import annotations

import html
import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("STEPWISE_BATCH_DATA_DIR", ROOT / "sample_data"))
OUT_ROOT = Path(os.environ.get("STEPWISE_OUTPUT_DIR", ROOT / "stepwise_new_mapping_reports"))
STANDING = DATA_DIR / "静态.txt"

TRIALS = [
    ("inversion", "内翻.txt", "Foot inversion trial"),
    ("eversion", "外翻.txt", "Foot eversion trial"),
    ("forefoot_landing", "前掌2.txt", "Forefoot landing trial"),
    ("rearfoot_landing", "后掌2.txt", "Rearfoot landing trial"),
    ("normal", "正常2.txt", "Normal walking reference/check"),
]


def main() -> None:
    OUT_ROOT.mkdir(exist_ok=True)
    rows: list[dict] = []

    for folder, filename, label in TRIALS:
        input_path = DATA_DIR / filename
        out_dir = OUT_ROOT / folder
        if not input_path.exists():
            raise FileNotFoundError(input_path)
        print(f"\n=== {label}: {input_path} ===")
        cmd = [
            sys.executable,
            str(ROOT / "stepwise_reference_pipeline.py"),
            str(input_path),
            "--output-dir",
            str(out_dir),
            "--standing-file",
            str(STANDING),
            "--heel",
            "P2",
            "--arch",
            "P3",
            "--medial-forefoot",
            "P4",
            "--lateral-forefoot",
            "P1",
        ]
        subprocess.run(cmd, cwd=str(ROOT), check=True)

        result = json.loads((out_dir / "reference_screening_result.json").read_text(encoding="utf-8"))
        summary = result["summary"]
        risks = result["posture_risks"]
        top = risks[0] if risks else {"title": "No output", "level": "-", "evidence": "-"}
        top_risks = "; ".join(f"{risk['level']}: {risk['title']}" for risk in risks[:3]) if risks else "No output"
        rows.append(
            {
                "folder": folder,
                "label": label,
                "file": filename,
                "top_title": top["title"],
                "top_risks": top_risks,
                "top_level": top["level"],
                "evidence": top["evidence"],
                "steps": summary.get("detected_steps_single_foot"),
                "hz": summary.get("estimated_sample_rate_hz"),
                "quality": summary.get("data_quality"),
            }
        )

    trs = "".join(
        (
            f"<tr><td>{html.escape(r['label'])}</td><td>{html.escape(r['file'])}</td>"
            f"<td>{html.escape(r['top_level'])}</td><td>{html.escape(r['top_risks'])}</td>"
            f"<td>{html.escape(str(r['quality']))}</td><td>{r['steps']}</td><td>{r['hz']:.1f}</td>"
            f"<td><a href='{r['folder']}/user_report.html'>User report</a><br>"
            f"<a href='{r['folder']}/technical_report.html'>Technical report</a><br>"
            f"<a href='{r['folder']}/data_guide.html'>Data guide</a></td></tr>"
        )
        for r in rows
    )
    index = f"""<!doctype html><html><head><meta charset="utf-8"><title>StepWise New Mapping Reports</title>
<style>body{{font-family:Arial,sans-serif;margin:32px;background:#f6f8fb;color:#17212b}}table{{border-collapse:collapse;width:100%;background:white}}th,td{{border:1px solid #d8e1ea;padding:10px;text-align:left;vertical-align:top}}th{{background:#eef4f8}}.note{{background:white;border:1px solid #d8e1ea;padding:16px;margin-bottom:18px}}</style>
</head><body><h1>StepWise New Sensor Mapping Reports</h1>
<div class="note"><strong>Sensor mapping used:</strong> P1 = lateral forefoot / little-toe root, P2 = heel, P3 = arch, P4 = medial forefoot / big-toe root.<br>
<strong>Static calibration file:</strong> {html.escape(str(STANDING))}</div>
<table><tr><th>Trial</th><th>Input file</th><th>Risk level</th><th>Top detected output</th><th>Quality</th><th>Stance phases</th><th>Hz</th><th>Reports</th></tr>{trs}</table></body></html>"""
    (OUT_ROOT / "index.html").write_text(index, encoding="utf-8")
    pd.DataFrame(rows).to_csv(OUT_ROOT / "batch_summary.csv", index=False, encoding="utf-8-sig")
    print(f"\nGenerated {OUT_ROOT}")


if __name__ == "__main__":
    main()
