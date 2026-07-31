# StepWise Gait Analysis Prototype

StepWise is an end-to-end engineering prototype that turns plantar-pressure and
IMU TXT recordings into explainable gait-screening outputs. It parses recordings,
smooths signals, segments stance phases with adaptive hysteresis, computes
regional-loading, gait-timing, and center-of-pressure proxy features, and applies
data-quality and evidence-conflict gates before emitting conclusions.

This repository is a software screening prototype, not a medical device. Its
rules and outputs are not clinically validated and must not be used for diagnosis.

## Architecture

- `stepwise_gait_analysis.py`: parsing, preprocessing, segmentation, features,
  quality checks, plots, and the base report pipeline.
- `stepwise_reference_pipeline.py`: standing calibration, evidence comparison,
  strict JSON results, and user/technical HTML reports.
- `stepwise_cloudrun_flask/app.py`: `POST /api/analyze-text` Flask adapter used by
  the mini-program prototype.
- `app.py`: local FastAPI demo and report-file server.
- `stepwise_miniprogram/`: upload, result, education, and local-history views for
  the WeChat Mini Program prototype.
- `tests/fixtures/minimal_walk.txt`: anonymous synthetic fixture; it contains no
  participant recording.

Both web adapters execute the two canonical analysis files at the repository
root. The Docker build does not maintain a second copy.

## Install

Python 3.11 is recommended.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
```

On macOS/Linux, activate or call `.venv/bin/python` instead.

## Run the analysis pipeline

```bash
python stepwise_reference_pipeline.py tests/fixtures/minimal_walk.txt --output-dir stepwise_output
```

For standing calibration and an explicit sensor map:

```bash
python stepwise_reference_pipeline.py walking.txt --standing-file standing.txt --output-dir stepwise_output --heel P2 --arch P3 --medial-forefoot P4 --lateral-forefoot P1
```

The TXT parser expects a numeric time column followed by pressure and IMU
measurements. Generated outputs include processed CSV data, feature tables,
strict JSON summaries, plots, and HTML reports. Non-finite values are serialized
as JSON `null`, never non-standard `NaN` tokens.

## Run the Flask API

```bash
python stepwise_cloudrun_flask/app.py
```

Health check: `GET /healthz`. Analysis request:

```json
{
  "walkingText": "contents of walking.txt",
  "standingText": "optional contents of standing.txt"
}
```

The response preserves the mini-program fields `run_id`, `top_result`, `summary`,
`metrics`, `cards`, and `urls`. Each request receives a UUID run ID. Empty or
oversized inputs return 4xx errors; analysis timeouts and execution failures return
structured 5xx errors. Configure storage and request limits with:

- `STEPWISE_DATA_DIR` (default: repository root)
- `STEPWISE_MAX_INPUT_CHARS` (default: `2000000` per TXT field)

## WeChat Mini Program

Edit `stepwise_miniprogram/config.js` locally to select either a cloud container
or an HTTPS API. The checked-in file intentionally contains no deployment URL or
private environment ID. `project.private.config.json` is ignored.

The prototype stores compact history records in the mini-program's local storage.
It does not provide authentication, a database, durable cloud storage, or a
guaranteed live public endpoint.

## Batch analysis

Set the input and output directories instead of editing source paths:

```powershell
$env:STEPWISE_BATCH_DATA_DIR = "C:\path\to\anonymous-trials"
$env:STEPWISE_OUTPUT_DIR = "C:\path\to\reports"
python batch_stepwise_reports.py
```

Historical project artifacts include outputs for nine labeled trial conditions.
Those reports are generated evidence and are intentionally excluded from Git.

## Test

```bash
python -m unittest discover -s tests -v
```

The suite covers TXT parsing, invalid timestamps, smoothing, adaptive-hysteresis
stance detection, NumPy 1.26 compatibility, strict JSON, empty/oversized API
inputs, timeouts, end-to-end API output, configuration, and repository hygiene.

To build the Flask image from the canonical source tree:

```bash
docker build -f stepwise_cloudrun_flask/Dockerfile -t stepwise-api .
docker run --rm -p 8080:80 stepwise-api
```

The prototype scope deliberately excludes clinical validation, production-scale
load testing, authentication, a database, and long-term cloud storage.
