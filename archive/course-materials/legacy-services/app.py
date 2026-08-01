from __future__ import annotations

import html
import json
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


BASE_DIR = Path(__file__).resolve().parent
PIPELINE = BASE_DIR / "stepwise_reference_pipeline.py"
UPLOAD_ROOT = BASE_DIR / "miniapp_uploads"
REPORT_ROOT = BASE_DIR / "miniapp_reports"
MAX_INPUT_CHARS = 2_000_000

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

UPLOAD_ROOT.mkdir(exist_ok=True)
REPORT_ROOT.mkdir(exist_ok=True)

app = FastAPI(title="StepWise Local Demo")
app.mount("/reports", StaticFiles(directory=REPORT_ROOT), name="reports")


class AnalyzeTextRequest(BaseModel):
    walkingText: str
    standingText: str | None = None
    heel: str | None = None
    arch: str | None = None
    medialForefoot: str | None = None
    lateralForefoot: str | None = None
    pitchEversionSign: str | None = None


def sensor_args_from_payload(payload: AnalyzeTextRequest) -> list[str]:
    def channel(value: str | None, default: str) -> str:
        candidate = str(value or default).upper()
        return candidate if candidate in VALID_CHANNELS else default

    sign = str(payload.pitchEversionSign or "positive").lower()
    if sign not in {"positive", "negative"}:
        sign = "positive"

    return [
        "--heel",
        channel(payload.heel, "P2"),
        "--arch",
        channel(payload.arch, "P3"),
        "--medial-forefoot",
        channel(payload.medialForefoot, "P4"),
        "--lateral-forefoot",
        channel(payload.lateralForefoot, "P1"),
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
        cwd=BASE_DIR,
        text=True,
        capture_output=True,
        timeout=120,
    )
    if completed.returncode != 0:
        return {}, completed.stdout + completed.stderr
    return load_result(output_dir), ""


def result_payload(run_id: str, result: dict) -> dict:
    summary = result.get("summary", {})
    metrics = result.get("metrics", {})
    cards = result.get("posture_risks", [])
    top = cards[0] if cards else {"title": "No clear posture-risk card", "level": "Low"}
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
            "user_report": f"/reports/{run_id}/user_report.html",
            "data_guide": f"/reports/{run_id}/data_guide.html",
            "technical_report": f"/reports/{run_id}/technical_report.html",
        },
    }


def page(title: str, body: str) -> HTMLResponse:
    css = """
    body { margin: 0; font-family: Arial, "Microsoft YaHei", sans-serif; background: #f4f7fb; color: #102a43; }
    main { max-width: 940px; margin: 0 auto; padding: 28px; }
    section { background: white; border: 1px solid #d9e2ec; border-radius: 10px; padding: 22px; margin-bottom: 18px; }
    h1 { margin: 0 0 8px; font-size: 30px; }
    h2 { margin: 0 0 14px; font-size: 22px; }
    p, li { line-height: 1.65; }
    .note { color: #52616f; }
    label { display: block; margin: 14px 0 6px; font-weight: 700; }
    input[type=file] { width: 100%; padding: 10px; background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 8px; }
    button, .button { display: inline-block; margin-top: 18px; padding: 11px 16px; border: 0; border-radius: 8px; background: #1769aa; color: white; font-weight: 700; text-decoration: none; cursor: pointer; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
    .metric, .card { border: 1px solid #d9e2ec; border-radius: 8px; padding: 14px; background: #fbfdff; }
    .metric span { display: block; color: #64748b; margin-bottom: 6px; font-size: 13px; }
    .metric strong { font-size: 20px; }
    .badge { display: inline-block; padding: 5px 10px; border-radius: 999px; background: #e0f2fe; color: #075985; font-weight: 700; }
    pre { white-space: pre-wrap; background: #0f172a; color: #e2e8f0; padding: 14px; border-radius: 8px; overflow: auto; }
    """
    return HTMLResponse(
        f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>{css}</style>
</head>
<body><main>{body}</main></body>
</html>"""
    )


@app.get("/", response_class=HTMLResponse)
def home() -> RedirectResponse:
    return RedirectResponse("/analyze")


@app.get("/analyze", response_class=HTMLResponse)
def analyze_form() -> HTMLResponse:
    body = """
    <section>
      <h1>StepWise 本地上传分析</h1>
      <p class="note">这是给你练手的小程序前置版本：先在网页里上传 TXT，确认后端分析能跑通。后面微信小程序会调用同一个接口。</p>
    </section>

    <section>
      <h2>1. 上传数据</h2>
      <form action="/analyze" method="post" enctype="multipart/form-data">
        <label>Walking TXT 数据文件，例如 normal.txt / forefoot.txt</label>
        <input name="walking_file" type="file" accept=".txt" required>

        <label>Standing TXT 站立校准文件，例如 standing.txt，可选但强烈建议上传</label>
        <input name="standing_file" type="file" accept=".txt">

        <input name="use_default_mapping" type="hidden" value="yes">
        <button type="submit">开始分析</button>
      </form>
    </section>

    <section>
      <h2>2. 当前默认传感器映射</h2>
      <ul>
        <li>P1 = 小拇指根部 / lateral forefoot</li>
        <li>P2 = 脚后跟 / heel</li>
        <li>P3 = 足弓 / arch</li>
        <li>P4 = 大脚趾根部 / medial forefoot</li>
        <li>PitchDelta 正方向 = 足外翻/过度旋前方向</li>
      </ul>
      <p class="note">第一版先固定这个映射。等你会用了，再把 P1-P4 做成小程序里的下拉选择。</p>
    </section>
    """
    return page("StepWise Analyze", body)


async def save_upload(upload: UploadFile, target: Path) -> None:
    with target.open("wb") as out:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)


def load_result(output_dir: Path) -> dict:
    result_path = output_dir / "reference_screening_result.json"
    if not result_path.exists():
        raise FileNotFoundError(f"Analysis finished but no result JSON was generated: {result_path}")
    return json.loads(result_path.read_text(encoding="utf-8"))


def cards_to_html(cards: list[dict]) -> str:
    if not cards:
        return "<p>No posture-risk card was generated.</p>"
    rows = []
    for card in cards:
        rows.append(
            f"""
            <article class="card">
              <h3>{html.escape(str(card.get("title", "-")))}</h3>
              <p><span class="badge">{html.escape(str(card.get("level", "-")))}</span></p>
              <p><strong>What this suggests:</strong> {html.escape(str(card.get("interpretation", "-")))}</p>
              <p><strong>Sensor evidence:</strong> {html.escape(str(card.get("evidence", "-")))}</p>
              <p><strong>Recommended next step:</strong> {html.escape(str(card.get("action", "-")))}</p>
            </article>
            """
        )
    return "\n".join(rows)


@app.post("/analyze", response_class=HTMLResponse)
async def analyze_upload(
    walking_file: UploadFile = File(...),
    standing_file: UploadFile | None = File(None),
    use_default_mapping: str = Form("yes"),
) -> HTMLResponse:
    run_id = uuid4().hex
    upload_dir = UPLOAD_ROOT / run_id
    output_dir = REPORT_ROOT / run_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    walking_path = upload_dir / "walking.txt"
    await save_upload(walking_file, walking_path)

    command = [
        sys.executable,
        str(PIPELINE),
        str(walking_path),
        "--output-dir",
        str(output_dir),
        *DEFAULT_SENSOR_ARGS,
    ]

    if standing_file and standing_file.filename:
        standing_path = upload_dir / "standing.txt"
        await save_upload(standing_file, standing_path)
        command.extend(["--standing-file", str(standing_path)])

    completed = subprocess.run(
        command,
        cwd=BASE_DIR,
        text=True,
        capture_output=True,
        timeout=120,
    )
    if completed.returncode != 0:
        body = f"""
        <section>
          <h1>分析失败</h1>
          <p>先别慌，下面是 Python 分析脚本返回的错误。把这一页截图给我，我继续帮你修。</p>
          <pre>{html.escape(completed.stdout + completed.stderr)}</pre>
          <a class="button" href="/analyze">返回重新上传</a>
        </section>
        """
        shutil.rmtree(output_dir, ignore_errors=True)
        return page("Analysis Failed", body)

    result = load_result(output_dir)
    summary = result.get("summary", {})
    metrics = result.get("metrics", {})
    cards = result.get("posture_risks", [])
    report_url = f"/reports/{run_id}/user_report.html"
    guide_url = f"/reports/{run_id}/data_guide.html"
    tech_url = f"/reports/{run_id}/technical_report.html"

    top_title = cards[0]["title"] if cards else "No clear posture-risk card"
    top_level = cards[0]["level"] if cards else "Low"
    body = f"""
    <section>
      <h1>分析完成</h1>
      <p><strong>Top result:</strong> {html.escape(str(top_title))} <span class="badge">{html.escape(str(top_level))}</span></p>
      <p>
        <a class="button" href="{report_url}" target="_blank">打开 User Report</a>
        <a class="button" href="{guide_url}" target="_blank">打开 Data Guide</a>
        <a class="button" href="{tech_url}" target="_blank">打开 Technical Report</a>
      </p>
    </section>

    <section>
      <h2>关键指标</h2>
      <div class="grid">
        <div class="metric"><span>Data quality</span><strong>{html.escape(str(summary.get("data_quality", "-")))}</strong></div>
        <div class="metric"><span>Valid stance phases</span><strong>{html.escape(str(summary.get("detected_steps_single_foot", "-")))}</strong></div>
        <div class="metric"><span>Sample rate</span><strong>{html.escape(str(round(summary.get("estimated_sample_rate_hz", 0), 1)))} Hz</strong></div>
        <div class="metric"><span>PitchDelta</span><strong>{html.escape(str(round(metrics.get("PitchDelta_stance_from_standing", 0), 2)))} deg</strong></div>
        <div class="metric"><span>Landing angle</span><strong>{html.escape(str(round(metrics.get("LandingSoleGroundAngle_deg", 0), 2)))} deg</strong></div>
        <div class="metric"><span>ArchRatio</span><strong>{html.escape(str(round(metrics.get("ArchRatio_mean", 0), 3)))}</strong></div>
      </div>
    </section>

    <section>
      <h2>用户风险卡</h2>
      {cards_to_html(cards)}
    </section>

    <section>
      <h2>下一步你要做什么</h2>
      <ol>
        <li>先点上面的 <strong>打开 User Report</strong>，看报告页面是不是你想给用户看的样子。</li>
        <li>再点 <strong>打开 Data Guide</strong>，看里面的公式和文献依据。</li>
        <li>确认这个网页流程没问题后，我们再把它搬到微信小程序。</li>
      </ol>
      <a class="button" href="/analyze">继续上传下一份数据</a>
    </section>
    """
    return page("StepWise Result", body)


@app.post("/api/analyze-text")
async def analyze_text_api(payload: AnalyzeTextRequest) -> dict | JSONResponse:
    if not payload.walkingText.strip():
        return JSONResponse(status_code=400, content={"ok": False, "error": "walkingText is required"})
    if len(payload.walkingText) > MAX_INPUT_CHARS:
        return JSONResponse(
            status_code=413,
            content={"ok": False, "error": "walkingText exceeds the 2,000,000-character limit"},
        )
    if payload.standingText is not None and len(payload.standingText) > MAX_INPUT_CHARS:
        return JSONResponse(
            status_code=413,
            content={"ok": False, "error": "standingText exceeds the 2,000,000-character limit"},
        )

    run_id = f"api_{uuid4().hex}"
    upload_dir = UPLOAD_ROOT / run_id
    output_dir = REPORT_ROOT / run_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    walking_path = upload_dir / "walking.txt"
    walking_path.write_text(payload.walkingText, encoding="utf-8")

    standing_path = None
    if payload.standingText and payload.standingText.strip():
        standing_path = upload_dir / "standing.txt"
        standing_path.write_text(payload.standingText, encoding="utf-8")

    try:
        result, error_text = run_stepwise_pipeline(
            walking_path,
            output_dir,
            standing_path,
            sensor_args_from_payload(payload),
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(output_dir, ignore_errors=True)
        return JSONResponse(
            status_code=504,
            content={"ok": False, "error": "Analysis timed out after 120 seconds"},
        )
    except OSError as exc:
        shutil.rmtree(output_dir, ignore_errors=True)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": f"Analysis process could not start: {exc}"},
        )
    if error_text:
        shutil.rmtree(output_dir, ignore_errors=True)
        return JSONResponse(status_code=500, content={"ok": False, "error": error_text[-2000:]})
    return result_payload(run_id, result)
