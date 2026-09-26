# HTTP API

## Upload → poll → result → download

Start the service as described in the [engineering guide](ENGINEERING.md). This Bash example uses Python to read JSON, so no additional JSON utility is required:

```bash
BASE=http://127.0.0.1:8080
curl --fail -sS -F walking=@tests/fixtures/synthetic_1682.txt "$BASE/api/v1/analyses" > /tmp/stepwise-job.json
RUN_ID=$(python -c "import json; print(json.load(open('/tmp/stepwise-job.json'))['run_id'])")
while true; do
  STATE=$(curl --fail -sS "$BASE/api/v1/analyses/$RUN_ID" | python -c "import json,sys; print(json.load(sys.stdin)['status'])")
  [ "$STATE" = succeeded ] && break
  [ "$STATE" = failed ] && { echo 'Analysis failed'; exit 1; }
  sleep 1
done
curl --fail -sS "$BASE/api/v1/analyses/$RUN_ID/result" > /tmp/stepwise-result.json
curl --fail -sS "$BASE/api/v1/analyses/$RUN_ID/artifacts/user_report.html" -o /tmp/stepwise-report.html
```

The upload responds **202 Accepted** with a `run_id`, `status_url`, and `result_url`. Poll until `succeeded` or `failed`; successful results contain summary/metrics and the available artifact names. Use an artifact name from that allowlist to download it. Add `-F standing=@path/to/standing.txt` only when optional calibration is available. Interactive OpenAPI documentation is served at `/docs`.

## Endpoint reference

## `POST /api/v1/analyses`

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

## `GET /api/v1/analyses/{run_id}`

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

## `GET /api/v1/analyses/{run_id}/result`

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

## `GET /api/v1/analyses/{run_id}/artifacts/{name}`

Downloads only a basename present in the successful result's artifact allowlist. The full-signal `processed_gait_data.csv` is materialized atomically on first download and then cached until normal retention cleanup.

## `GET /healthz`

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

## Error contract

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
