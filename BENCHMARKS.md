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
| 20260915-195234 | cd64bdf | 1682 | 0.48 | median of 3 after 1 warmup, spread 4.5% |
| 20260915-195234 | cd64bdf | 6000 | 0.78 | median of 3 after 1 warmup, spread 6.6% |
| 20260915-195234 | cd64bdf | 30000 | 2.29 | median of 3 after 1 warmup, spread 3.5% |
| 20260915-195234 | cd64bdf | 180000 | 10.93 | median of 3 after 1 warmup, spread 3.5% |
| 20260915-195234 | cd64bdf | 360000 | 21.62 | median of 3 after 1 warmup, spread 0.9% |

> The section 5.2 `stance_features` result is valid (2.8814 s to 0.4306 s at 360,000
> samples). Its TOTAL is not directly comparable with the Phase 0 warm-up baseline: the
> sessions were about 20 hours apart, and the untouched parse, features_basic, segment,
> and artifacts stages measured 0.7%, 5.1%, 9.1%, and 6.4% slower respectively at 360,000
> samples, indicating machine-state drift. Starting with section 5.3, before and after are
> measured back to back in the same session.

## Back-to-back pipeline measurements (section 5.3 onward)

| date | commit | samples | total s | note |
|---|---|---:|---:|---|
| 20260915-211042 | 33faa78 | 1682 | 0.52 | median of 3 after 1 warmup, spread 4.4% |
| 20260915-211042 | 33faa78 | 6000 | 0.87 | median of 3 after 1 warmup, spread 9.6% |
| 20260915-211042 | 33faa78 | 30000 | 2.69 | median of 3 after 1 warmup, spread 19.7% |
| 20260915-211042 | 33faa78 | 180000 | 11.83 | median of 3 after 1 warmup, spread 2.3% |
| 20260915-211042 | 33faa78 | 360000 | 23.44 | median of 3 after 1 warmup, spread 4.4% |
| 20260915-211359 | 5af25b5 | 1682 | 0.48 | median of 3 after 1 warmup, spread 3.1% |
| 20260915-211359 | 5af25b5 | 6000 | 0.78 | median of 3 after 1 warmup, spread 6.1% |
| 20260915-211359 | 5af25b5 | 30000 | 2.30 | median of 3 after 1 warmup, spread 1.6% |
| 20260915-211359 | 5af25b5 | 180000 | 21.98 | median of 3 after 1 warmup, spread 63.0% |
| 20260915-211359 | 5af25b5 | 360000 | 22.55 | median of 3 after 1 warmup, spread 2.7% |
| 20260915-224134 | e3a4606 | 1682 | 0.46 | median of 3 after 1 warmup, spread 2.2% |
| 20260915-224134 | e3a4606 | 6000 | 0.77 | median of 3 after 1 warmup, spread 8.5% |
| 20260915-224134 | e3a4606 | 30000 | 2.27 | median of 3 after 1 warmup, spread 2.5% |
| 20260915-224134 | e3a4606 | 180000 | 11.03 | median of 3 after 1 warmup, spread 1.8% |
| 20260915-224134 | e3a4606 | 360000 | 21.30 | median of 3 after 1 warmup, spread 2.4% |
| 20260915-224422 | 87a7f83 | 1682 | 0.42 | median of 3 after 1 warmup, spread 1.9% |
| 20260915-224422 | 87a7f83 | 6000 | 0.62 | median of 3 after 1 warmup, spread 10.0% |
| 20260915-224422 | 87a7f83 | 30000 | 1.58 | median of 3 after 1 warmup, spread 3.0% |
| 20260915-224422 | 87a7f83 | 180000 | 7.29 | median of 3 after 1 warmup, spread 2.7% |
| 20260915-224422 | 87a7f83 | 360000 | 13.38 | median of 3 after 1 warmup, spread 1.9% |
| 20260916-014527 | bd476cb | 1682 | 0.42 | median of 3 after 1 warmup, spread 4.2% |
| 20260916-014527 | bd476cb | 6000 | 0.60 | median of 3 after 1 warmup, spread 11.7% |
| 20260916-014527 | bd476cb | 30000 | 1.56 | median of 3 after 1 warmup, spread 4.9% |
| 20260916-014527 | bd476cb | 180000 | 7.11 | median of 3 after 1 warmup, spread 0.8% |
| 20260916-014527 | bd476cb | 360000 | 13.08 | median of 3 after 1 warmup, spread 2.6% |
| 20260916-014704 | b3cd202 | 1682 | 0.41 | median of 3 after 1 warmup, spread 1.8% |
| 20260916-014704 | b3cd202 | 6000 | 0.77 | median of 3 after 1 warmup, spread 6.7% |
| 20260916-014704 | b3cd202 | 30000 | 1.29 | median of 3 after 1 warmup, spread 3.3% |
| 20260916-014704 | b3cd202 | 180000 | 3.91 | median of 3 after 1 warmup, spread 1.3% |
| 20260916-014704 | b3cd202 | 360000 | 6.98 | median of 3 after 1 warmup, spread 2.6% |
<!-- bench_stepwise.py inserts new per-size pipeline rows immediately above this line. -->
