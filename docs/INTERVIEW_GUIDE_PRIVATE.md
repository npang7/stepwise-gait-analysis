# StepWise Interview Guide (Private)

Use this as a study guide, not a script. Describe only behavior you can explain and reproduce.

## 60-second project explanation

StepWise began as two roughly 1,500-line course scripts for analyzing plantar-pressure and IMU text recordings. I refactored the prototype into a typed `src/stepwise` package with one `AnalysisService` entry point shared by the CLI, batch runner, FastAPI service, and worker processes. The API accepts multipart uploads, returns a UUID immediately, and runs each analysis in a separate process so the supervisor can enforce a hard timeout. Results and artifact allowlists are stored in atomic JSON manifests. The signal pipeline smooths pressure channels, detects stance with adaptive hysteresis, extracts regional loading and timing features, and suppresses conclusions when data quality is low or evidence conflicts.

## Architecture choices to defend

- **Why one application service?** It prevents the API, CLI, batch runner, and mini program from drifting into separate algorithm implementations.
- **Why processes instead of background threads?** NumPy/pandas/Matplotlib work can be CPU-heavy or hang. A process can be terminated on timeout; a Python thread cannot be safely killed.
- **Why a filesystem repository?** It keeps the prototype reproducible without adding a database. Atomic `manifest.json` replacement avoids partially written state.
- **Why one Uvicorn process?** The in-process queue and worker supervisor own job state. Multiple API processes would require a shared broker or database, which is intentionally outside scope.
- **Why an artifact allowlist?** Download requests are resolved only from names written to the successful job manifest, preventing arbitrary path access.
- **Why return 202?** Analysis is asynchronous. Clients poll a status resource rather than holding an HTTP request open.
- **Why quality/conflict gates?** A screening prototype should explicitly refuse unsupported conclusions instead of converting every recording into a posture label.

## Request lifecycle

1. FastAPI validates upload size, UTF-8 text, timestamps, and unique sensor mapping.
2. `JobRepository` creates a UUID directory and atomically writes a queued manifest.
3. `JobManager` starts a spawn-based worker when capacity is available.
4. The worker calls `AnalysisService.analyze` and writes the standard artifact set.
5. The supervisor records success, failure, crash, or hard timeout in the manifest.
6. The client polls status, fetches strict JSON, and downloads only allowlisted artifacts.

## Limits to state honestly

- This is an engineering screening prototype, not a medical device or diagnosis system.
- There is no authentication, database, durable object storage, or public production deployment.
- The queue is process-local; horizontal scaling would require a shared job broker and state store.
- Historical nine-condition trials are private project evidence; CI uses only an anonymous synthetic fixture.
- Docker may be listed only after a local or CI build succeeds. GitHub Actions may be listed only after the remote workflow runs successfully.

## Likely follow-ups

- **How would you productionize it?** Add authentication, object storage, a relational job table, a shared queue such as Redis-backed workers, observability, rate limits, and retention policies. Keep `AnalysisService` transport-independent.
- **What happens after restart?** Successful manifests remain readable; queued/running manifests become `failed/service_restarted`; expired terminal runs are removed according to TTL.
- **How do you avoid JSON `NaN`?** Domain serialization recursively converts non-finite numeric values to JSON `null` and writes with `allow_nan=False`.
- **How is timeout tested?** A test worker deliberately sleeps beyond a 0.15-second limit; the supervisor terminates it and records `analysis_timeout`.
- **What would you measure?** Queue depth, analysis duration, failure codes, timeout count, artifact size, and per-stage timing—without logging raw sensor data.
