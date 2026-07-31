from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from flask import Flask, jsonify, request, send_from_directory


BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent if (BASE_DIR.parent / "stepwise_reference_pipeline.py").is_file() else BASE_DIR
PIPELINE = PROJECT_ROOT / "stepwise_reference_pipeline.py"
DATA_ROOT = Path(os.environ.get("STEPWISE_DATA_DIR", "/tmp/stepwise_cloudrun"))
UPLOAD_ROOT = DATA_ROOT / "uploads"
REPORT_ROOT = DATA_ROOT / "reports"
MAX_INPUT_CHARS = int(os.environ.get("STEPWISE_MAX_INPUT_CHARS", "2000000"))

DEFAULT_SENSOR_ARGS = [
    "--heel",
    "P2",
    "--arch",
    "P3",
    "--medial-forefoot",
    "P4",
    "--lateral-forefoot",
    "P1",
    "--pitch-eversion-sign",
    "positive",
]
VALID_CHANNELS = {"P1", "P2", "P3", "P4"}

UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
REPORT_ROOT.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)


def sensor_args_from_payload(payload: dict) -> list[str]:
    def channel(key: str, default: str) -> str:
        value = str(payload.get(key, default)).upper()
        return value if value in VALID_CHANNELS else default

    sign = str(payload.get("pitchEversionSign", "positive")).lower()
    if sign not in {"positive", "negative"}:
        sign = "positive"

    return [
        "--heel",
        channel("heel", "P2"),
        "--arch",
        channel("arch", "P3"),
        "--medial-forefoot",
        channel("medialForefoot", "P4"),
        "--lateral-forefoot",
        channel("lateralForefoot", "P1"),
        "--pitch-eversion-sign",
        sign,
    ]


def run_stepwise_pipeline(
    walking_path: Path,
    output_dir: Path,
    standing_path: Path | None = None,
    sensor_args: list[str] | None = None,
) -> tuple[dict, str]:
    command = [
        sys.executable,
        str(PIPELINE),
        str(walking_path),
        "--output-dir",
        str(output_dir),
        *(sensor_args or DEFAULT_SENSOR_ARGS),
    ]
    if standing_path:
        command.extend(["--standing-file", str(standing_path)])

    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        timeout=120,
    )
    if completed.returncode != 0:
        return {}, completed.stdout + completed.stderr

    result_path = output_dir / "reference_screening_result.json"
    if not result_path.exists():
        return {}, f"Analysis finished but no result JSON was generated: {result_path}"

    return json.loads(result_path.read_text(encoding="utf-8")), ""


def result_payload(run_id: str, result: dict) -> dict:
    summary = result.get("summary", {})
    metrics = result.get("metrics", {})
    cards = result.get("posture_risks", [])
    top = cards[0] if cards else {"title": "No clear posture-risk card", "level": "Low"}
    base = request.host_url.rstrip("/")
    return {
        "ok": True,
        "run_id": run_id,
        "top_result": top.get("title", "No clear posture-risk card"),
        "top_level": top.get("level", "Low"),
        "summary": summary,
        "metrics": {
            "PitchDelta": metrics.get("PitchDelta_stance_from_standing"),
            "LandingAngle": metrics.get("LandingSoleGroundAngle_deg"),
            "ArchRatio": metrics.get("ArchRatio_mean"),
            "MedialRatio": metrics.get("MedialRatio_mean"),
            "LateralRatio": metrics.get("LateralRatio_mean"),
            "CoP_AP_progression": metrics.get("CoP_AP_progression"),
            "CoP_ML": metrics.get("CoP_ML_mean"),
        },
        "cards": cards,
        "urls": {
            "user_report": f"{base}/reports/{run_id}/user_report.html",
            "data_guide": f"{base}/reports/{run_id}/data_guide.html",
            "technical_report": f"{base}/reports/{run_id}/technical_report.html",
        },
    }


@app.get("/")
def index():
    return jsonify(
        {
            "service": "StepWise cloud backend",
            "status": "ok",
            "usage": "POST /api/analyze-text with walkingText and optional standingText",
        }
    )


@app.get("/healthz")
def healthz():
    return "ok"


@app.post("/api/analyze-text")
def analyze_text_api():
    payload = request.get_json(silent=True) or {}
    walking_text = payload.get("walkingText", "")
    standing_text = payload.get("standingText", "")

    if not isinstance(walking_text, str) or not walking_text.strip():
        return jsonify({"ok": False, "error": "walkingText is required"}), 400
    if len(walking_text) > MAX_INPUT_CHARS:
        return jsonify({"ok": False, "error": "walkingText exceeds the 2,000,000-character limit"}), 413
    if not isinstance(standing_text, str):
        return jsonify({"ok": False, "error": "standingText must be a string"}), 400
    if len(standing_text) > MAX_INPUT_CHARS:
        return jsonify({"ok": False, "error": "standingText exceeds the 2,000,000-character limit"}), 413

    run_id = f"api_{uuid4().hex}"
    upload_dir = UPLOAD_ROOT / run_id
    output_dir = REPORT_ROOT / run_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    walking_path = upload_dir / "walking.txt"
    walking_path.write_text(walking_text, encoding="utf-8")

    standing_path = None
    if isinstance(standing_text, str) and standing_text.strip():
        standing_path = upload_dir / "standing.txt"
        standing_path.write_text(standing_text, encoding="utf-8")

    sensor_args = sensor_args_from_payload(payload)
    try:
        result, error_text = run_stepwise_pipeline(walking_path, output_dir, standing_path, sensor_args)
    except subprocess.TimeoutExpired:
        shutil.rmtree(output_dir, ignore_errors=True)
        return jsonify({"ok": False, "error": "Analysis timed out after 120 seconds"}), 504
    except OSError as exc:
        shutil.rmtree(output_dir, ignore_errors=True)
        return jsonify({"ok": False, "error": f"Analysis process could not start: {exc}"}), 500
    if error_text:
        shutil.rmtree(output_dir, ignore_errors=True)
        return jsonify({"ok": False, "error": error_text[-2000:]}), 500
    return jsonify(result_payload(run_id, result))


@app.get("/reports/<run_id>/<path:filename>")
def reports(run_id: str, filename: str):
    report_dir = REPORT_ROOT / run_id
    return send_from_directory(report_dir, filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "80"))
    app.run(host="0.0.0.0", port=port)
