# StepWise Gait Analysis

StepWise is a modular software prototype that converts plantar-pressure and IMU text recordings into explainable gait-screening reports. It combines a typed Python analysis package, an asynchronous FastAPI service, a CLI/batch adapter, and a WeChat Mini Program client.

This is an engineering screening prototype, not a medical device. Its rules have not been clinically validated and its outputs must not be used for diagnosis.

## Architecture

```text
CLI / batch ───────┐
                   │
FastAPI ─ queue ─ worker process ─┐
                   │              │
WeChat Mini Program┘              ▼
                         AnalysisService
                                │
          parse → smooth → segment → features → quality/rules → reports
                                │
                     atomic UUID manifest + artifacts
```

`AnalysisService.analyze(walking_path, standing_path, output_dir, config)` is the only business entry point. The API, CLI, batch runner, and workers do not duplicate the algorithm.

The active source is under `src/stepwise`:

- `parsing.py`, `signal.py`, and `features.py`: UTF-8 TXT parsing, preprocessing, adaptive-hysteresis stance detection, and feature extraction.
- `screening.py`: data-quality and evidence-conflict gates with non-diagnostic risk cards.
- `service.py` and `reporting.py`: the end-to-end analysis entry point and strict JSON/CSV/HTML/plot artifacts.
- `storage.py` and `jobs.py`: atomic filesystem manifests, TTL cleanup, a bounded queue, worker crash handling, and killable hard timeouts.
- `api.py`: versioned multipart HTTP API.
- `cli.py`: local analyze and batch commands.

The original course scripts and services are preserved under `archive/course-materials` and are not runtime dependencies.

## Install

Python 3.11 or 3.12 is supported.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

On macOS/Linux, use `.venv/bin/python`.

## CLI

Analyze one walking file:

```powershell
.venv\Scripts\python.exe -m stepwise analyze tests\fixtures\minimal_walk.txt --output stepwise-output\demo
```

Add an optional standing-calibration recording and sensor mapping:

```powershell
.venv\Scripts\python.exe -m stepwise analyze walking.txt --standing standing.txt --output stepwise-output\trial --mapping-json '{"heel":"P2","arch":"P3","medial_forefoot":"P4","lateral_forefoot":"P1","pitch_eversion_sign":"positive"}'
```

Batch every `.txt` file in a directory:

```powershell
.venv\Scripts\python.exe -m stepwise batch .\anonymous-trials --output stepwise-output\batch
```

Exit codes are stable: `0` success, `2` invalid input, and `1` unexpected analysis failure.

## FastAPI

Start one API service process; analyses still run in separate supervised worker processes:

```powershell
.venv\Scripts\python.exe -m uvicorn stepwise.api:create_app --factory --host 127.0.0.1 --port 8080 --workers 1
```

Manifest transitions are serialized by run-striped `threading.RLock` instances owned by the
single `JobRepository`. These locks coordinate only threads that share that repository instance;
they do not protect multiple Uvicorn service processes or replicas sharing one data directory.
Such a deployment requires an interprocess file lock or external coordination and is not
supported by this filesystem-backed service.

OpenAPI is available at `http://127.0.0.1:8080/docs` and health at `GET /healthz`.

Create an analysis:

```bash
curl -X POST http://127.0.0.1:8080/api/v1/analyses \
  -F "walking=@walking.txt;type=text/plain" \
  -F "standing=@standing.txt;type=text/plain" \
  -F 'sensor_mapping={"heel":"P2","arch":"P3","medial_forefoot":"P4","lateral_forefoot":"P1","pitch_eversion_sign":"positive"}'
```

Uploads stream in 64 KiB chunks to temporary files under `STEPWISE_DATA_DIR/.staging`.
Validation runs off the event loop before the server returns `202 Accepted`; successful inputs
move atomically into the UUID run directory. Poll the status resource until `succeeded` or
`failed`, then fetch strict JSON and the allowlisted artifacts.

The full-signal `processed_gait_data.csv` is backed by an eagerly persisted Parquet file
and is generated atomically on its first download, then cached until normal TTL cleanup
removes the run. Its manifest `size_bytes` is always `0`: this is a documented sentinel
meaning that the lazy artifact's size is not maintained in the terminal manifest, not that
the CSV is empty. Clients can read the materialized size from the download response's
`Content-Length` header.

### API routes

- `POST /api/v1/analyses`
- `GET /api/v1/analyses/{run_id}`
- `GET /api/v1/analyses/{run_id}/result`
- `GET /api/v1/analyses/{run_id}/artifacts/{name}`
- `GET /healthz`

### Error model

Errors use one stable shape:

```json
{
  "error": {
    "code": "binary_input",
    "message": "uploaded recording must be text"
  }
}
```

Important status codes include `413` for oversized uploads, `422` for invalid text/timestamps/mapping, `429` for a full queue, `409` when a result is not ready, and `404` for unknown jobs or unlisted artifacts. Worker failures use stable codes such as `analysis_timeout`, `worker_crashed`, and `service_restarted`.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `STEPWISE_DATA_DIR` | `./stepwise-data` | UUID run directories and artifacts |
| `STEPWISE_MAX_UPLOAD_BYTES` | `67108864` | Maximum bytes per uploaded recording |
| `STEPWISE_ANALYSIS_TIMEOUT_SECONDS` | `120` | Hard worker timeout |
| `STEPWISE_MAX_WORKERS` | `2` | Concurrent analysis processes |
| `STEPWISE_MAX_QUEUE` | `8` | Waiting jobs before `429` |
| `STEPWISE_RESULT_TTL_HOURS` | `24` | Terminal-result retention |

On startup, stale staged uploads are removed, successful jobs remain available, and incomplete
jobs become `failed/service_restarted`. Expired terminal jobs are removed at startup and when
the service creates work.

## WeChat Mini Program

`stepwise_miniprogram` uploads the multipart request, polls status, displays results, and stores compact local history. Update `stepwise_miniprogram/config.js` locally with either WeChat Cloud Run identifiers or an HTTPS API base URL. The checked-in file intentionally contains no live endpoint or private environment ID.

The API client/state machine is independent from the page and has Node tests for upload construction, polling, success, failure, and local history.

## Tests and quality gates

```powershell
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m mypy src\stepwise
.venv\Scripts\python.exe -m pytest --cov=stepwise --cov-report=term-missing
node --test tests\js\*.test.js
```

Run the reproducible performance baseline with `python bench_stepwise.py --compare`.
By default, each size discards one full warm-up before three recorded runs; use `--warmup 0`
to disable it. The command writes aggregate evidence to `bench-data/baseline-*.json`, appends
warm-up-aware median rows to `BENCHMARKS.md`, and records comparison stdout under `bench-data/`.
Reproduce it from a clean `pip install -e ".[dev]"` environment without overriding dependencies.

Run the concurrent HTTP suite with `python -m bench.load_test`. It records the environment,
then derives separate 30k and 360k memory gates from two single-job calibrations; use
`python -m bench.load_test --preflight-only` to record only the environment snapshot. The complete
method, invalidation rules, matrix, and fault scenarios are in
`docs/LOAD_TEST_BRIEF.md`. Session output is written under `bench-data/loadtest-<timestamp>/`;
large disposable service roots remain under the ignored `bench-data/loadtest-work/`.

Coverage must remain at least 80%. Tests include parsing and timestamp errors, smoothing, hysteresis and feature regression, quality/conflict rules, strict JSON, atomic manifest recovery, TTL cleanup, artifact traversal, queue capacity, worker crash, hard timeout, HTTP status contracts, CLI exit codes, and a real multipart-to-artifact worker run.

GitHub Actions runs the Python gates on 3.11 and 3.12, runs the Mini Program tests, builds the Docker image, and starts a real container. The container gate covers health, upload, polling, result, and artifact download. A workflow file existing locally does not prove that remote CI has run.

## Docker

```bash
docker build -t stepwise-api .
docker run --rm -p 8080:8080 stepwise-api
python -m stepwise.ci_smoke --base-url http://127.0.0.1:8080 --walking tests/fixtures/minimal_walk.txt
```

The image uses Python 3.11 slim, runs as the non-root `stepwise` user, exposes one Uvicorn service process, and includes a health check. Docker becomes a confirmed resume skill only after a real local or remote container smoke test succeeds.

## Deliberate scope boundaries

This version does not add authentication, a database, a shared distributed queue, long-term object storage, a public deployment, or new clinical algorithms. Those are future production-system extensions, not completed claims.
