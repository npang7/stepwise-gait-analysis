# StepWise benchmarks

| date | commit | samples | total s | note |
|---|---|---:|---:|---|
| 20260914-222229 | c8d15c5 | 1682 | 0.49 | median of 3, spread 28.1% |
| 20260914-222229 | c8d15c5 | 6000 | 0.82 | median of 3, spread 3.0% |
| 20260914-222229 | c8d15c5 | 30000 | 2.45 | median of 3, spread 6.8% |
| 20260914-222229 | c8d15c5 | 180000 | 12.58 | median of 3, spread 17.1% |
| 20260914-222229 | c8d15c5 | 360000 | 24.62 | median of 3, spread 2.0% |

## Phase 1 upload measurements

| date | commit | samples | file bytes | API peak RSS before | API peak RSS after | worker to terminal |
|---|---|---:|---:|---:|---:|---:|
| 20260915-005951 | 6067e55 | 713650 | 67108864 | 1520078848 | 1520410624 | 45.6876 s |

The full protocol, dependency metadata, source commits, and submission timings are recorded in
`bench-data/baseline-phase1-upload-20260915-005951.json`.
