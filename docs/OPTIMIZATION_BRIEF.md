# StepWise Backend Hardening — Implementation Brief

Audience: an implementer working in the `stepwise-gait-analysis` repository.
This brief supplements `AGENTS.md`. Where this brief is more specific, it wins; every
other rule in `AGENTS.md` still applies in full.

---

## 0. Explicit scope expansion

`AGENTS.md` forbids adding a database, a distributed queue, long-term storage, or a
public deployment "unless the user explicitly expands the scope." **The repository owner
hereby expands the scope** to permit, and only to permit:

- a PostgreSQL-backed run store replacing the filesystem manifest,
- a Postgres-backed job queue replacing the in-process queue,
- S3-compatible object storage for inputs and artifacts (Phase 4, optional),
- a single-instance public deployment (Phase 4, optional).

**Still forbidden, unchanged:** authentication, any new or modified clinical algorithm,
any change to screening thresholds or rule semantics, and any claim of clinical accuracy,
diagnostic capability, production users, production traffic, or production scale.

---

## 1. Ground rules

1. **Never invent a number.** Every performance figure must come from an actual run of
   `bench_stepwise.py`, committed as a row in `BENCHMARKS.md` plus a
   `bench-data/baseline-*.json` that records the machine, Python, NumPy, and pandas
   versions. If you did not measure it, do not write it — in code comments, in docs, in
   commit messages, or anywhere else. A number you estimated, extrapolated, or copied
   from this brief is an invented number.
2. **One behavioural change per commit**, each with its own tests. For Phase 2, each of
   the four optimisations is its own commit with a before/after benchmark row.
3. **Tests before implementation** for every behaviour change (already required by
   `AGENTS.md`).
4. **Preserve the public contract**: the five HTTP routes, the `{"error":{"code","message"}}`
   shape, the status-code semantics (413/422/429/409/404), the CLI exit codes (0/2/1), the
   six existing environment variables, artifact names, and strict-JSON output with no
   `NaN`/`Infinity`. New environment variables may be *added*; none may be removed or
   change meaning.
5. **Do not touch `archive/`.** It is historical course material, read-only.
6. Branch `codex/*`, independent worktree, integrate by PR with green CI. Never commit
   to `master` directly.
7. Every phase must leave `ruff check src tests`, `mypy src/stepwise`,
   `pytest --cov=stepwise` (≥80%), and `node --test tests/js/*.test.js` green.
8. **New dependencies require justification.** List each one and why a stdlib or existing
   dependency will not do. Expected additions: `sqlalchemy`, `alembic`, `psycopg[binary]`,
   `pyarrow`. Anything beyond that must be argued for.
9. For each non-obvious design decision, write a short record in `docs/adr/NNN-title.md`:
   the decision, the alternatives rejected, and why. Keep each under one page.

---

## 2. Already correct — do not rewrite

The following are deliberate and working. Extend them; do not redesign them.

- `AnalysisService.analyze` as the single business entry point, with API, CLI, batch, and
  workers all calling it. **No adapter may fork the algorithm.**
- The stable error model and status-code mapping in `api.py`.
- Artifact allowlisting in `JobRepository.artifact_path` (basename check plus
  manifest-declared allowlist). This is the path-traversal defence — keep it in place
  when storage moves.
- Killable hard timeouts, crash detection via the worker result queue, and the
  `terminate → join → kill` escalation in `JobManager._stop_process`.
- The frozen dataclass domain models and `strict_json_value` sanitisation.
- The non-root Dockerfile, its healthcheck, and the CI container smoke test.
- The Mini Program's separately tested API client state machine.

---

## 3. Phase 0 — Baseline and two defects

**Nothing else may begin until Phase 0 is committed.** Without a recorded baseline no
later speedup can be substantiated.

### 3.1 Benchmark harness
Add `bench_stepwise.py` (supplied by the owner) at the repository root. Run:

```
python bench_stepwise.py --compare
```

Commit the resulting `bench-data/baseline-*.json` and the `BENCHMARKS.md` rows. Add
`bench-data/walk_*.txt` and `bench-data/artifacts_*/` to `.gitignore` — generated
recordings must not enter Git.

### 3.2 Defect: eager default argument in `features.py`

```python
trapezoid = getattr(np, "trapezoid", np.trapz)   # np.trapz evaluated eagerly
```

Python evaluates the default argument before `getattr` runs, so this raises
`AttributeError` on NumPy ≥ 2.0, where `np.trapz` was removed. Replace with:

```python
trapezoid = np.trapezoid if hasattr(np, "trapezoid") else np.trapz
```

Hoist it out of the per-step loop while you are there. Add a regression test that passes
under both the lowest and highest supported NumPy.

### 3.3 Defect: dependency range admits a broken NumPy
`pyproject.toml` pins `numpy>=1.26,<2.4`, a range that includes versions where 3.2 fails.
Fix 3.2, then add a CI leg that installs the **lowest** and the **highest** allowed
versions of `numpy` and `pandas` and runs the test suite against each. The existing
matrix varies only the Python version, which is why this was invisible.

---

## 4. Phase 1 — Streaming upload and a raised input ceiling

**Why:** `STEPWISE_MAX_UPLOAD_BYTES` defaults to 2 MiB, capping a recording at roughly
seven minutes. Lifting it is the precondition for every later phase. But
`_read_upload` currently does `await upload.read(limit + 1)`, materialising the entire
payload as one `bytes` object, so the ceiling cannot be raised as it stands.

### 4.1 Streaming ingest
Replace the read-all path with a chunked stream:

1. Stream the upload into a staging file under `<data_dir>/.staging/<uuid>` in fixed
   64 KiB chunks.
2. Enforce the byte cap **during** streaming: stop and return `413` as soon as the cap is
   exceeded, without reading the remainder.
3. Validate from the staged path, not from an in-memory buffer.
4. Only on success create the run directory and atomically move the staged file into it.
5. On **every** failure path — oversize, invalid input, full queue, unexpected error —
   remove the staging file. No orphaned staging files, no orphaned run directories.
6. Add startup cleanup of `.staging/` alongside the existing `recover_incomplete()`.

`parse_stepwise_bytes` stays for the CLI and tests; add a path-based validation entry
point for the service. Do not duplicate parsing logic between the two.

### 4.2 Raised ceiling
Default `STEPWISE_MAX_UPLOAD_BYTES` to 64 MiB, still overridable. Confirm
`STEPWISE_ANALYSIS_TIMEOUT_SECONDS` (120 s) still covers the largest supported recording
after Phase 2; if not, raise the default and say so in the PR.

### 4.3 Required tests
Streamed write correctness; cap tripped mid-stream returns 413 and leaves no staging file;
invalid content after a complete stream returns 422 and cleans up; queue-full after a
complete stream returns 429 and cleans up; startup removes stale staging files.

---

## 5. Phase 2 — Pipeline performance

Target, measured on a 1-hour, 360,000-sample recording: **~54 s → ~5 s**. Four
independent commits. Report each one's measured before/after; do not report a combined
figure until all four have landed.

### 5.1 Permanent equivalence check (do this first)
Before changing any computation, add a verification mode that runs both the original and
the optimised path over a fixture set and asserts agreement within a declared tolerance
(`1e-9` absolute for float columns). Wire it into CI as a test, not a one-off script.
**This is the mechanism that makes every later claim defensible — it is not optional and
it must not be deleted once the optimisations land.**

### 5.2 Vectorise `extract_stance_features`
Currently a per-stance Python loop performing roughly 25 pandas slices and aggregations
per step. Replace with:
- integer stance/early/late label columns assigned once,
- three `groupby` aggregations for the segment, early-window, and late-window means,
- a cumulative-trapezoid pass plus endpoint differencing for `PressureImpulse_Ns`.

**Must preserve exactly**: every column name, column order, dtype, and the `Pattern`
classification strings. `StrideTime_s`/`SwingTime_s` must keep their `NaN`-on-first-step
semantics.

### 5.3 Vectorise `parse_stepwise_text`
Replace the per-line Python loop with a `pandas.read_csv` fast path
(`sep=r"\s+"`, C engine). The current parser deliberately tolerates malformed lines by
filtering on `len(parts) == 15 and parts[0].isdigit()`; `read_csv` does not. So: attempt
the fast path, and **fall back to the existing line filter** whenever the fast path raises
or yields an unexpected shape. All five existing error codes — `empty_input`,
`binary_input`, `invalid_encoding`, `no_data_rows`, `invalid_timestamp` — must keep firing
on exactly the same inputs. Add fixtures with truncated, extra-field, and junk-interleaved
lines proving the fallback path.

### 5.4 Stop eagerly writing the full-signal CSV
`write_analysis_artifacts` calls `processed.to_csv(...)` on every run, producing roughly
179 MB of text for a 1-hour session — the single largest cost in the pipeline.

Preferred: persist the processed frame as **Parquet** eagerly (sub-second), and generate
`processed_gait_data.csv` **on demand** when the artifact endpoint requests it, writing
atomically so concurrent requests cannot observe a partial file. The artifact name stays
in the manifest, so the API contract does not change.

Acceptable fallback if the on-demand path proves too invasive: keep eager CSV behind a
config flag defaulting to off, with Parquet as the default artifact. Document whichever
you choose in an ADR.

### 5.5 Decimate plot series
Plots render every sample at dpi 130. Decimate to roughly 4,000 points for rendering only;
the stored data is unchanged.

**Use min/max envelope decimation per bucket, not naive striding.** Striding
(`iloc[::step]`) can skip pressure peaks, which would visibly alter the stance plot and
silently misrepresent the signal.

---

## 6. Phase 3 — Postgres state and a SKIP LOCKED queue

**Why, concretely:** `JobRepository.recover_incomplete()` currently marks every `queued`
or `running` job as `failed/service_restarted` on startup. With analyses that take tens of
seconds, **every deploy destroys in-flight user work.** That is the problem this phase
solves. It is not "adding a queue for its own sake."

### 6.1 Schema
One `runs` table: `run_id` (uuid PK), `status`, `created_at`, `updated_at`,
`idempotency_key` (unique, nullable), `config` (jsonb), `result` (jsonb, null),
`error_code`, `error_message`, `attempts`, `lease_owner`, `lease_expires_at`,
`code_version`. Index on `(status, created_at)` for the claim query. Manage with Alembic.

`code_version` records which build produced a result — needed so results from different
analysis versions are distinguishable.

### 6.2 Claim, lease, reclaim
Workers claim with `SELECT … FOR UPDATE SKIP LOCKED` over rows that are `queued` **or**
`running` with an expired lease, setting `lease_owner`, `lease_expires_at`, and
incrementing `attempts`. A running worker renews its lease on a heartbeat. A crashed or
restarted worker's lease simply expires and another worker reclaims the row.

`recover_incomplete()`'s fail-everything behaviour is **removed**, not modified.

### 6.3 Idempotency — required, not optional
Lease expiry means a job can legitimately execute more than once. The guarantee to state is
"zero duplicate **committed results**", and that requires a mechanism:

- the result is a single row keyed by `run_id`, so re-execution overwrites rather than
  appends — duplication is structurally impossible, not merely unlikely;
- artifact paths are derived deterministically from `run_id` and written atomically;
- `POST /api/v1/analyses` accepts an optional `Idempotency-Key` header; a repeat key
  returns the existing run instead of creating a second one. This matters because the
  Mini Program retries uploads over mobile networks and today every retry creates a
  duplicate analysis.

Adding a request header does not change the five routes, so the contract holds.

### 6.4 Worker as a separate process
Add `python -m stepwise.worker`. Add `STEPWISE_QUEUE_BACKEND` with values `memory`
(default, the existing in-process manager) and `postgres`. **The default must keep every
existing test and the CI container smoke test passing without a database.** Add a separate
CI job with a Postgres service container that runs the suite with the `postgres` backend.

### 6.5 Chaos verification
A test that runs N workers against M submitted jobs, kills workers at random points
including mid-analysis, and asserts: every job reaches a terminal state, every succeeded
job has exactly one result row, artifacts are complete and consistent, and no job is lost.
Report the actual injected-failure count — do not write a round number you did not run.

---

## 7. Phase 4 — Object storage and deployment (optional)

Only once Phase 3 is merged and green. S3-compatible storage for inputs and artifacts
(MinIO locally), keeping the allowlist check on retrieval. Single-instance deployment with
HTTPS and a GitHub Actions deploy step. Do not begin this phase without the owner
confirming it.

---

## 8. Per-phase deliverables

- `README.md` updated **only where this phase made it untrue or incomplete** — in
  particular the "Deliberate scope boundaries" section, plus any newly added developer
  entry point (a new script, command, or generated file). If this phase made nothing in
  the README false, say so in the PR and change nothing;
- an ADR for each non-obvious design decision, **if this phase had any** — a phase with
  no real design choices needs none;

---

## 9. What you must not claim

Per `AGENTS.md`, restated because it is the easiest rule to breach while writing docs:

- A workflow file existing does not mean CI ran. A test existing does not mean it passed.
- No performance number without a committed benchmark row behind it.
- No "production", "at scale", "users", or clinical language anywhere.
- If a phase is partially done, say so plainly in the PR rather than rounding up.

---

## 10. Mandatory deep-dive documentation

Implement everything in this brief, including the queue internals, the chaos test, and the
benchmark runs. But for these three areas **the implementation alone is not an acceptable
deliverable** — each requires an explanatory artifact written to the standard below,
because the repository owner has to be able to reconstruct the reasoning from memory
without reading the code. A PR that lands the code without its document is incomplete.

Write these for a competent engineer who has never seen the repository. No hand-waving,
no "for robustness", no restating what the code literally says.

### 10.1 `docs/adr/00X-postgres-skip-locked-queue.md`

Must answer each of the following in prose, with specifics rather than generalities:

1. Why a Postgres-backed queue instead of Celery, RabbitMQ, or Redis? What is given up by
   that choice, and at what scale would it stop being the right call?
2. What does `FOR UPDATE SKIP LOCKED` actually do? What would happen with `FOR UPDATE`
   alone, and what would happen with no row locking at all?
3. Where exactly are the transaction boundaries in claim, heartbeat, and commit — and what
   breaks if the commit is moved inside or outside the claim transaction?
4. Walk the full sequence for the hard case: **a lease expires while the original worker
   is still alive and mid-analysis.** Who does what, in what order, and what does the user
   observe?
5. Why is this at-least-once rather than exactly-once? Name the specific mechanism that
   prevents a duplicate committed result, and explain why it is a structural guarantee
   rather than a race that is merely unlikely.
6. Why is `attempts` tracked, and what should happen after N failed attempts?
7. Why index `(status, created_at)`? Include the actual `EXPLAIN` output for the claim
   query against a table seeded with a realistic row count, and explain the plan.

Additionally, annotate the claim query in the source line by line.

### 10.2 `docs/CHAOS_TEST.md`

- Every failure-injection point, and which real-world failure each one represents
  (deploy, OOM kill, network partition to the database, worker segfault, …).
- For each assertion: what bug would trip it. Give a concrete example of a plausible
  implementation mistake that the assertion would catch.
- What the test does **not** cover, stated plainly.
- How to reproduce a single failing seed deterministically.

### 10.3 Benchmarks

Run them and commit the results with full machine and dependency metadata (the harness
captures this automatically). Then, in the PR description, state explicitly which numbers
came from which machine.

**The owner must independently re-run at least one size and confirm it reproduces.** If
the owner's figure and the committed figure differ by more than 20%, neither number may be
used anywhere until the discrepancy is explained — a benchmark that does not reproduce on
a second machine is not a result, it is an artifact of one environment.





## 11. Standing decisions — decide these yourself, do not ask

Do not ask the owner to arbitrate anything this section already settles. Act, and record
what you chose in the PR description.

Do not delete remote branches. The repository has "Automatically delete head
branches" enabled, so GitHub removes the head branch server-side on merge. Local
worktrees and local branches you created may be cleaned up normally.

**Git and PR mechanics**

- Branch `codex/<phase-slug>`, independent worktree, PR to `master`, never commit to
  `master` directly.
- Create PRs with the `gh` CLI; it is authenticated.
- Merge with `gh pr merge <N> --merge`. **Never `--squash` or `--rebase`** — both rewrite
  commit SHAs, and measurement artifacts record the SHA of the commit they were produced
  on. A rewritten SHA orphans that reference.
- Delete the branch after a successful merge.

**Commit structure**
- One behavioural change per commit, with its tests.
- A commit that records a measurement must come *after* the commit containing the code it
  measured, so the recorded SHA resolves to a tree that includes it.
- Tooling changes, defect fixes, measurement records and CI changes are always separate
  commits.

**Merging**
- You may merge a PR yourself once CI is green **and** it introduces no new measured
  number — no new or changed `BENCHMARKS.md` row, benchmark artifact, speedup, latency,
  throughput, or failure-injection count.
- A PR that does introduce a measured number stays open until the owner explicitly
  approves. Post the numbers and stop.

**Documentation**
- Section 8 as written: README only where this phase made it untrue; ADR only for a real
  design decision.
- Never hardcode a dynamic fact (test counts, SHAs, resolved dependency versions) into a
  durable document. Those belong in PR descriptions and CI logs.

**Phase boundaries**
- Never start the next phase on your own, whatever the outcome of the current one.

## 12. When you must still ask

Ask only if one of these holds. Otherwise choose, proceed, and explain the choice:

1. It would alter the public contract — the five routes, the error shape, a status code,
   a CLI exit code, or the meaning of an existing environment variable.
2. It would invalidate an already-recorded measurement, or make an earlier recorded
   number no longer reproducible.
3. It is irreversible — force-push, history rewrite, deleting committed data.
4. Two provisions of this brief genuinely conflict and the choice materially changes what
   is delivered.

"Which of these three reasonable approaches do you prefer?" is not a reason to ask. Pick
the one that best satisfies this brief, say so, and move on.





## 13. Measurement hygiene

Timing runs are only valid on a quiet machine. Before any before/after measurement:

- Close other agent sessions, builds, containers, and any GPU or ML workload.
- Confirm the repository is NOT inside a cloud-synced folder. Writing the
  179 MB processed CSV into a OneDrive-backed path repeatedly injects large,
  unpredictable variance into the artifacts stage and silently consumes quota.
  If it is synced, either exclude `bench-data/` from sync or move the repository
  outside the synced tree before measuring.
- Do not modify antivirus, firewall, or any other system security configuration.
  Excluding `bench-data/` from real-time scanning may lower variance, but it
  requires administrator rights, a failed cleanup would leave the exclusion in
  place silently, and its actual cost here has never been measured. It is the
  repository owner's decision to make manually, not an agent action, and it is
  never a precondition for a valid measurement: scanning affects the before and
  after runs alike, adding noise rather than bias.
- Record in the PR what else was running, or state that the machine was idle.

If any size's relative spread exceeds 20%, the session was not quiet enough. Report
it and re-run on a quiet machine rather than citing the result. A spread that large
means the measurement is of the machine, not of the code.
