# StepWise benchmarks

## Phase 1 upload measurements

| date | commit | samples | file bytes | API peak RSS before | API peak RSS after | worker to terminal |
|---|---|---:|---:|---:|---:|---:|
| 20260915-005951 | 6067e55 | 713650 | 67108864 | 1520078848 | 1520410624 | 45.6876 s |

The full protocol, dependency metadata, source commits, and submission timings are recorded in
`bench-data/baseline-phase1-upload-20260915-005951.json`.

## Pipeline measurements

| date | commit | samples | total s | note |
|---|---|---:|---:|---|
| 20260914-222229 | c8d15c5 | 1682 | 0.49 | median of 3, spread 28.1% |
| 20260914-222229 | c8d15c5 | 6000 | 0.82 | median of 3, spread 3.0% |
| 20260914-222229 | c8d15c5 | 30000 | 2.45 | median of 3, spread 6.8% |
| 20260914-222229 | c8d15c5 | 180000 | 12.58 | median of 3, spread 17.1% |
| 20260914-222229 | c8d15c5 | 360000 | 24.62 | median of 3, spread 2.0% |
| 20260914-235536 | e3aa928 | 1682 | 0.44 | median of 3 after 1 warmup, spread 4.2% |
| 20260914-235536 | e3aa928 | 6000 | 0.75 | median of 3 after 1 warmup, spread 4.6% |
| 20260914-235536 | e3aa928 | 30000 | 2.23 | median of 3 after 1 warmup, spread 2.8% |
| 20260914-235536 | e3aa928 | 180000 | 11.69 | median of 3 after 1 warmup, spread 1.0% |
| 20260914-235536 | e3aa928 | 360000 | 22.92 | median of 3 after 1 warmup, spread 1.4% |
<!-- bench_stepwise.py inserts new per-size pipeline rows immediately above this line. -->
