# ADR 003: Parquet-backed lazy processed CSV

## Decision

Persist the processed frame eagerly as `processed_gait_data.parquet` without changing its
dtypes. Keep that file private to the run directory and keep the existing public
`processed_gait_data.csv` manifest entry. Generate the CSV on its first artifact request,
write it to a unique temporary sibling, atomically replace the final path, and cache it for
later requests. The existing whole-run TTL cleanup removes both formats.

`processed_gait_data.csv` permanently reports `size_bytes: 0` in `GET /result`. Zero is a
sentinel meaning that this lazy artifact's size is not maintained in the terminal manifest;
it does not mean an empty file. The response's `Content-Length` is authoritative. Keeping
the sentinel avoids a new write path to a terminal manifest and leaves `updated_at` and TTL
semantics unchanged.

Requests hold a run-scoped lease from materialization through streaming. Cleanup skips an
expired run with an active lease and may remove it on the next pass. If cleanup removes the
run first, the later request returns the existing not-found response. Per-artifact locks,
double-checked cache existence, unique temporary files, and atomic replacement prevent a
request from observing a partial CSV.

## Alternatives rejected

- Regenerating on every request repeatedly pays the largest serialization cost and creates
  unnecessary disk traffic.
- Updating `size_bytes` after materialization mutates an otherwise terminal manifest,
  introduces another atomic-write and TTL race, and changes `updated_at` expectations.
- Eager CSV behind a configuration flag keeps the default analysis cost and adds a seventh
  environment setting without need; the on-demand path is contained in the artifact route.
- Exposing Parquet changes the public artifact allowlist. It remains an implementation
  detail so existing API and Mini Program clients continue to see the same artifact names.

## Dependency

`pyarrow>=14,<26` is required because pandas has no built-in Parquet engine. A CSV backing
retains the cost being removed, pickle is Python-specific and unsafe for interchange, and
adding a different Parquet engine would not avoid a runtime dependency. The lower and upper
bounds are exercised by the dependency-bounds CI job.

Across the fixture matrix and the 360,000-sample oracle, the Parquet round-trip preserved
column order and dtypes and remained within the existing numerical tolerance. Re-emitting
CSV from the Parquet frame was byte-identical to the previous eager CSV, so no oracle or
reference relaxation is needed.
