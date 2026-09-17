# StepWise Gait Analysis

StepWise turns text recordings from a pressure-sensing insole and an inertial measurement unit (IMU) into an explainable gait-screening report. In ordinary terms, it finds when the foot is on the ground, summarizes timing and regional pressure loading, checks whether the recording is usable, and produces risk cards and downloadable tables and plots.

StepWise is an engineering screening prototype, not a medical device. Its rules have not been clinically validated, and its output is not a diagnosis.

## Quick start

### Install

Python 3.11 and 3.12 are supported.

```bash
python -m venv .venv
# macOS/Linux
.venv/bin/python -m pip install -e ".[dev]"
# Windows PowerShell
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

The remaining examples assume the virtual environment is active, so `python` resolves to its interpreter.

### Start the local service

Run exactly one Uvicorn service process. Analysis work still runs in separate supervised processes.

```bash
python -m uvicorn stepwise.api:create_app --factory --host 127.0.0.1 --port 8080 --workers 1
```

In another terminal, submit the included synthetic fixture:

```bash
curl -X POST http://127.0.0.1:8080/api/v1/analyses \
  -F "walking=@tests/fixtures/minimal_walk.txt;type=text/plain" \
  -F 'sensor_mapping={}'
```

The service returns `202 Accepted` with a `run_id`, status URL, and result URL. Poll the returned status URL until `status` is `succeeded` or `failed`, then fetch the result:

```bash
curl http://127.0.0.1:8080/api/v1/analyses/RUN_ID
curl http://127.0.0.1:8080/api/v1/analyses/RUN_ID/result
```

Replace `RUN_ID` with the UUID returned by the first request. OpenAPI is available at <http://127.0.0.1:8080/docs>.

### Start with Docker

```bash
docker build -t stepwise-api .
docker run --rm -p 8080:8080 stepwise-api
```

From a host environment where StepWise is installed, exercise health, upload, polling, result, and artifact download against that container:

```bash
python -m stepwise.ci_smoke \
  --base-url http://127.0.0.1:8080 \
  --walking tests/fixtures/minimal_walk.txt
```

### Run locally without HTTP

```bash
python -m stepwise analyze \
  tests/fixtures/minimal_walk.txt \
  --output stepwise-output/demo
```

Batch mode analyzes every `.txt` file in a directory:

```bash
python -m stepwise batch anonymous-trials --output stepwise-output/batch
```

## HTTP API

Errors use the stable envelope:

```json
{
  "error": {
    "code": "binary_input",
    "message": "uploaded recording must be text"
  }
}
```

Unavailable-service responses add persistence-backlog context:

```json
{
  "error": {
    "code": "job_supervisor_unavailable",
    "message": "The job supervisor is unavailable."
  },
  "terminal_persistence": {
    "pending_count": 0,
    "oldest_wait_seconds": null
  }
}
```

### `POST /api/v1/analyses`

Accepts multipart form data:

- `walking`: required StepWise UTF-8 text recording;
- `standing`: optional standing-calibration recording;
- `sensor_mapping`: optional JSON object mapping heel, arch, medial/lateral forefoot, and pitch sign.

A successful request returns `202` and a status document shaped as follows:

```json
{
  "run_id": "UUID",
  "status": "queued",
  "created_at": "ISO-8601 timestamp",
  "updated_at": "ISO-8601 timestamp",
  "status_url": "/api/v1/analyses/UUID",
  "result_url": "/api/v1/analyses/UUID/result"
}
```

### `GET /api/v1/analyses/{run_id}`

Returns the same status document. `status` is one of `queued`, `running`, `succeeded`, or `failed`. A failed job adds:

```json
{
  "error": {
    "code": "analysis_timeout",
    "message": "Analysis timed out."
  }
}
```

Other stable worker outcome codes include `worker_crashed`, `analysis_failed`, and `service_restarted`.

### `GET /api/v1/analyses/{run_id}/result`

After success, returns strict JSON with this top-level shape:

```json
{
  "summary": {},
  "metrics": {},
  "risk_cards": [],
  "artifacts": []
}
```

Non-finite values are recursively converted to JSON `null`; serialization uses `allow_nan=False`.

```bash
python -m pytest \
  tests/test_service.py::AnalysisServiceTests::test_analysis_service_matches_the_golden_fixture_and_writes_manifested_artifacts \
  tests/test_storage.py::JobManifestTests::test_manifest_round_trip_uses_strict_json_values -q
```

### `GET /api/v1/analyses/{run_id}/artifacts/{name}`

Downloads only a basename present in the successful result's artifact allowlist. The full-signal `processed_gait_data.csv` is materialized atomically on first download and then cached until normal retention cleanup.

### `GET /healthz`

A healthy service returns:

```json
{
  "status": "ok",
  "terminal_persistence": {
    "pending_count": 0,
    "oldest_wait_seconds": null
  }
}
```

The health check reads only supervisor and in-memory persistence-backlog state; it does not perform filesystem I/O.

### Error contract

| HTTP status | Stable code | Trigger |
|---:|---|---|
| `413` | `upload_too_large` | Either uploaded recording exceeds the configured per-file byte limit. |
| `422` | `invalid_mapping` | Mapping JSON is malformed, has unknown fields, repeats a pressure channel, or uses an unsupported value. |
| `422` | `empty_input` | An uploaded recording is empty or contains only whitespace. |
| `422` | `binary_input` | An uploaded recording contains a NUL byte. |
| `422` | `invalid_encoding` | An uploaded recording is not UTF-8 text. |
| `422` | `no_data_rows` | No valid StepWise data rows remain after parsing. |
| `422` | `invalid_timestamp` | `SystemTime` is not in the required time-of-day format. |
| `429` | `queue_full` | Validation and staging succeeded, but the bounded pending queue was full at enqueue time. |
| `409` | `result_not_ready` | The job is queued, running, or failed rather than succeeded. |
| `404` | `analysis_not_found` | The run ID is unknown or its retained result has expired. |
| `404` | `artifact_not_found` | The requested name is not in the successful manifest's allowlist. |
| `503` | `job_supervisor_unavailable` | The job supervisor thread stopped or exited unexpectedly. |
| `503` | `terminal_persistence_saturated` | The supervisor is alive, but active work plus terminal results awaiting persistence reached capacity. |
| `500` | `artifact_generation_failed` | Lazy generation of `processed_gait_data.csv` failed. |

The contract tests execute all of these HTTP status families and stable envelopes:

```bash
python -m pytest tests/test_api.py -q
```

## Architecture

```text
CLI / batch ───────────────┐
                           │
FastAPI ─ bounded queue ─ spawn worker process
                           │
Mini Program ──────────────┘
                           ▼
                    AnalysisService
                           │
      parse → smooth → stance segmentation → features
                           │
          quality/conflict gates → reports and artifacts
                           │
          atomic UUID manifest + allowlisted files
```

`AnalysisService.analyze(walking_path, standing_path, output_dir, config)` is the single business entry point. CLI, batch, HTTP, and worker adapters call it instead of maintaining separate algorithm implementations.

### Request lifecycle

1. FastAPI streams multipart uploads into staging files and validates byte limits, UTF-8 text, timestamps, and the one-to-one sensor mapping.
2. `JobRepository` creates a UUID run directory and atomically writes a queued `manifest.json`.
3. `JobManager` starts a spawn-based worker when the configured worker capacity is available.
4. The worker invokes `AnalysisService` and writes the standard artifact set.
5. The supervisor records success, analysis failure, worker crash, or hard timeout in the manifest.
6. The client polls status, retrieves strict JSON, and downloads only allowlisted artifacts.

### Why it is designed this way

- **One application service:** every adapter shares one analysis implementation, preventing transport-specific algorithm drift.
- **Processes rather than background threads:** pandas, NumPy, or plotting work can be CPU-heavy or hang. A worker process can be terminated at a hard timeout; a Python thread cannot be safely killed. The deterministic timeout test uses a deliberately slow runner:

  ```bash
  python -m pytest \
    tests/test_jobs.py::JobManagerTests::test_timeout_terminates_worker_instead_of_leaving_it_running -q
  ```

- **Filesystem repository with atomic manifests:** the prototype remains reproducible without a database, while same-directory replacement prevents readers from observing a partially written manifest.
- **One Uvicorn service process:** the queue and supervisor are in memory. Multiple service processes or replicas require a shared broker/state store and interprocess locking.
- **Artifact allowlist:** the download route resolves only names recorded by a successful analysis, preventing a request from selecting an arbitrary filesystem path.
- **`202` plus polling:** analysis does not hold an HTTP connection open while CPU-bound work runs, and status survives a client disconnect.
- **Quality and conflict gates:** low-quality or contradictory evidence suppresses unsupported conclusions instead of forcing every recording into a posture label.

Manifest transitions use run-striped `threading.RLock` instances inside one `JobRepository`. They prevent same-process lost updates and Windows replace/read races, but do not protect multiple service processes sharing a data directory. That deployment needs a file lock or external coordinator.

## Configuration

The supported environment variables and defaults are:

| Variable | Default | Purpose |
|---|---:|---|
| `STEPWISE_DATA_DIR` | `./stepwise-data` | UUID run directories and artifacts |
| `STEPWISE_MAX_UPLOAD_BYTES` | `67108864` | Maximum bytes per uploaded recording |
| `STEPWISE_ANALYSIS_TIMEOUT_SECONDS` | `120` | Hard worker timeout in seconds |
| `STEPWISE_MAX_WORKERS` | `2` | Concurrent worker processes |
| `STEPWISE_MAX_QUEUE` | `8` | Waiting jobs before `429` |
| `STEPWISE_RESULT_TTL_HOURS` | `24` | Terminal-result retention in hours |

Print the effective defaults from the installed source:

```bash
python -c "from stepwise.settings import Settings; print(Settings())"
```

At startup, successful manifests remain readable, queued/running manifests become `failed/service_restarted`, stale staging files are removed, and expired terminal runs are deleted according to the retention setting.

## Reproduce the reported measurements

The commands in this section do not substitute a new timing session for historical evidence. They recompute the displayed values directly from committed raw JSON. Run them from the repository root after installation.

### Single-analysis latency and stage timing

For a 360,000-sample synthetic recording, the A-B-A session measured end-to-end time from **21.4765 s to 6.6074 s**. The two control runs differed by **0.6564%**. The dominant stage changes were `artifacts` **15.5545 → 2.7678 s**, `stance_features` **2.6447 → 0.3403 s**, and `parse` **2.5756 → 2.7544 s** (a regression retained in the result).

```bash
python -m bench.readme_metrics aba
# aba: total=21.4765->6.6074s control_difference=0.6564% artifacts=15.5545->2.7678s stance_features=2.6447->0.3403s parse=2.5756->2.7544s
```

Raw evidence:

- `bench-data/baseline-20260916-043413.json`
- `bench-data/baseline-20260916-043810.json`
- `bench-data/baseline-20260916-043945.json`

To create a new, machine-specific timing session rather than recompute the committed one:

```bash
python bench_stepwise.py --sizes 360000 --repeat 5 --warmup 1
```

### Peak resident memory

Peak API-process RSS for the parser comparison fell from **1.52 GB to 899 MB**.

```bash
python -m bench.readme_metrics memory
# memory: peak_rss=1.52GB->899MB
```

Raw evidence: `bench-data/parser-memory-20260915-212028.json`.

### Upload capacity

The per-recording upload limit is **64 MiB**. The exact-cap synthetic fixture contained **713,650 samples**, which is **1.9824 hours (about 2 hours) at 100 Hz**.

```bash
python -m bench.readme_metrics upload
# upload: limit=64MiB samples=713,650 duration_at_100Hz=1.9824h
```

Raw evidence: `bench-data/baseline-phase1-upload-20260915-005951.json`.

### Concurrent capacity and correctness

With **2 workers** and 30,000-sample jobs, throughput saturated at concurrency **10** at about **45.0 analyses/minute**. At concurrency **20**, it fell to **31.5 analyses/minute**. At the saturation point, median queue time was **9.735 s of 13.016 s** end-to-end, or **74.8%**. Across the committed concurrent suite, **2,111 successful analyses** produced identical normalized result summaries for their respective input and configuration.

```bash
python -m bench.readme_metrics load
# load: workers=2 C10=45.0/min C20=31.5/min queue_share=74.8% (9.735/13.016s) consistent=2,111
```

Raw evidence: `bench-data/loadtest-20260917T040248Z/raw-results.json`.

To run a new session, use:

```bash
python -m bench.load_test
```

The full protocols and interpretation are in `BENCHMARKS.md` and `LOADTEST.md`.

## Known limitations

- Queue-full backpressure is late. A request that receives `429` has already completed multipart parsing, copied and parsed the full input, acquired the submission lock, run retention cleanup, created a manifest/run directory, and moved the input before enqueue discovers that the queue is full. Under overload this rejected work raises cost rather than protecting the service early; see `LOADTEST.md`.
- Run-striped manifest locks are process-local. Multiple Uvicorn service processes or replicas must not share one data directory without file locking or external coordination.
- The queue is in memory, storage is the local filesystem, the supported deployment is a single service instance, and the API has no authentication.
- Throughput was measured on one laptop with the load generator and workers sharing processors. Treat the saturation point, queue growth, and overload decline as evidence; do not treat the absolute analyses/minute value as a production capacity guarantee.
- CI and benchmarks use only synthetic fixtures. The repository contains no participant recordings.
- This is a screening prototype. Quality/conflict gates reduce unsupported output but do not establish clinical accuracy.

## Evidence and engineering records

- `BENCHMARKS.md` summarizes reproducible single-analysis timing and memory measurements and links their raw JSON.
- `LOADTEST.md` records concurrent throughput, latency, correctness, resource gates, failure scenarios, and interpretation.
- `docs/adr/` contains architectural decision records and their consequences.
- `bench-data/` contains committed machine-readable evidence. Evidence SHA-256 values refer to Git repository bytes, normalized to LF, rather than platform-translated working-tree bytes.
- `docs/OPTIMIZATION_BRIEF.md` and `docs/LOAD_TEST_BRIEF.md` preserve the internal measurement requirements and revision history used to produce the evidence.

## Quality gates

```bash
python -m ruff check src tests
python -m mypy src/stepwise
python -m pytest --cov=stepwise --cov-report=term-missing
node --test tests/js/*.test.js
```

GitHub Actions runs the Python gates on both supported Python versions, tests declared dependency bounds, runs the Mini Program tests, builds the Docker image, and performs the real container flow shown above. A workflow file by itself is not evidence that the remote run succeeded.

The active package is under `src/stepwise`. Historical course material is retained under `archive/course-materials` and is not a runtime dependency.
