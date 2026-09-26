# Engineering guide

## Shared core and adapters

`src/stepwise/service.py` contains `AnalysisService`: parse a 15-field sensor recording, derive pressure/orientation features, detect contact intervals with hysteresis, compute metrics, and render artifacts. `models.py` defines typed configuration and strict JSON results. Optional standing calibration supplies neutral orientation values; walking-only analysis is supported.

The HTTP adapter stages uploads and submits jobs. CLI `analyze` and `batch` call the service directly, without a queue or web server. The companion Mini Program uploads recordings and polls the same HTTP contract.

## Job lifecycle and reliability

1. Validate and stage UTF-8 uploads with a per-file byte limit.
2. Allocate a run manifest and enqueue into a bounded pending queue. Saturation returns `429`; rejected run directories are removed.
3. The supervisor launches up to the configured number of spawn-process workers. CPU work runs outside the request process; expired workers are terminated.
4. Publish terminal state through atomic manifest replacement. Failed persistence remains in a bounded retry backlog; health fails closed when supervision or capacity is unavailable.
5. On restart, mark interrupted queued/running jobs failed rather than pretending work resumed.

Per-run striped locks serialize manifest transitions. Artifact downloads acquire reader leases; cleanup respects readers and active jobs. Completed output expires after the retention window. Tests cover saturation, worker timeout/crash, restart, persistence failure, competing transitions, downloads, and cleanup.

## Artifacts and data integrity

Results expose `summary`, `metrics`, `risk_cards`, and an artifact allowlist. JSON converts non-finite values to `null`. The private backing signal is Parquet; full CSV is materialized atomically on first request. PNG plots use per-series chronological min/max envelopes. HTML reports escape embedded text. Filenames outside the manifest allowlist are never served.

## Run the API locally

Activate the environment created in the README (`source .venv/bin/activate` in Bash, `.\.venv\Scripts\Activate.ps1` in PowerShell), then:

```bash
python -m uvicorn stepwise.api:create_app --factory --host 127.0.0.1 --port 8080
```

Use one Uvicorn application process: job scheduling is in-memory and artifacts/manifests use a local filesystem, not a distributed queue. The service has no authentication layer; bind local demos to loopback and use appropriate access controls before exposing uploads to a network. Allow RAM and disk for input staging, worker processes, and retained artifacts.

| Environment variable | Default |
|---|---:|
| `STEPWISE_DATA_DIR` | `stepwise-data` |
| `STEPWISE_MAX_UPLOAD_BYTES` | `67108864` (64 MiB per file) |
| `STEPWISE_ANALYSIS_TIMEOUT_SECONDS` | `120` |
| `STEPWISE_MAX_WORKERS` | `2` |
| `STEPWISE_MAX_QUEUE` | `8` |
| `STEPWISE_RESULT_TTL_HOURS` | `24` |

### Companion Mini Program

Import `stepwise_miniprogram/` into WeChat Developer Tools. The committed `touristappid` supports a demonstration configuration; select your own AppID for device use. Set `apiBase` in `config.js` to the analysis service URL. Alternatively, `cloudEnv` and `cloudService` select the client's Cloud Run transport; these are configuration options, not a hosted service included with this repository. Keep deployment identifiers local. WeChat device requests need a reachable HTTPS endpoint and platform domain configuration; a phone's loopback address is not the development computer.

Upload a required walking TXT recording. A standing TXT calibration is **optional** and enables neutral-orientation delta metrics; without it, calibration-dependent interpretations are unavailable. The UI and API use the same requirement.

## Tests and containers

```bash
python -m pip install -e '.[dev]'
ruff check src tests bench
mypy src/stepwise
python -m pytest --cov=stepwise --cov-report=term-missing
node --test tests/js/*.test.js
```

Python coverage has an 80% gate. CI runs Python 3.11/3.12, low/high numeric dependency bounds, and the Mini Program tests. The optional large oracle is available separately:

```bash
python -m pytest -m large_equivalence tests/test_equivalence_oracle.py -q
```

The Docker gate uses a **real container** to exercise health, upload, status polling, result retrieval, and artifact download:

```bash
docker build --tag stepwise-api:test .
docker run --detach --name stepwise-demo --publish 127.0.0.1:8080:8080 stepwise-api:test
docker cp tests/fixtures/minimal_walk.txt stepwise-demo:/tmp/minimal_walk.txt
docker exec stepwise-demo python -m stepwise.ci_smoke --base-url http://127.0.0.1:8080 --walking /tmp/minimal_walk.txt --timeout-seconds 60
docker logs stepwise-demo
docker rm --force stepwise-demo
```

## Generate the showcase example

```bash
python -m bench.demo_assets
```

This runs the shared service against `synthetic_1682.txt`, reads the resulting Parquet signal, and writes the README image plus its numeric summary under `docs/assets/`. The contact intervals come from the same detection functions as normal analysis.
