# Performance evidence

## Controlled pipeline comparison

Synthetic pressure/IMU recordings were measured in a sequential **A1/B/A2** session on September 16, 2026. A1 and A2 are control runs bracketing the optimized implementation. Each variant used one warmup and five measured repetitions per size (1,682, 6,000, 30,000, 180,000, and 360,000 samples).

Environment: Windows 10 build 26200; Intel64 Family 6 Model 186 Stepping 2; Python 3.11.7; NumPy 2.3.5; pandas 2.3.3. Recorded machine/version fields and per-run timings are retained in [portfolio-evidence.json](../bench-data/portfolio-evidence.json).

| 360,000-sample stage | Control | Optimized |
|---|---:|---:|
| Total | 21.4765 s | 6.6074 s |
| Artifact generation | 15.5545 s | 2.7678 s |
| Stance features | 2.6447 s | 0.3403 s |
| Parse | 2.5756 s | 2.7544 s |

Total uses the pooled median of ten A1/A2 timings versus B's five-run median. Stage controls use the mean of A1/A2 medians. A1/A2 total medians differ by **0.6564%** symmetrically. The total ratio is approximately **3.25×**.

Artifact work improved through lazy CSV materialization, Parquet backing, and extrema-preserving plot envelopes; vectorized stance-feature work reduced computation. The parser prioritizes exact malformed-row compatibility and lower memory: parsing alone was **6.94% slower** in this controlled comparison. Stage figures do not imply universal speedups on other hardware or inputs.

## Peak process memory

A separate September 15, 2026 measurement submitted a **64 MiB synthetic recording** with 713,650 samples, padded with whitespace to the upload limit. Peak API process working set (`Get-Process PeakWorkingSet64`) changed from **1,520,627,712** to **898,891,776 bytes**: 1.52 GB → 899 MB in decimal units, approximately 40.89% lower.

This is a process peak, not aggregate host memory or the sum of all workers. The memory session is separate from the pipeline timing session and should not be interpreted as a simultaneous speed/memory benchmark.

## Concurrent load behavior

Recorded September 17, 2026 with **2 workers**, **8 queued jobs**, 50 ms polling, 500 ms retry after `429`, 120 s timeout, and a 24 h retention window. Client and service shared a Windows host with 20 logical / 14 physical CPUs and 16,780,636,160 bytes of RAM; dependency versions are included in the evidence.

For 30,000-sample recordings, median throughput across repetitions was **45.0 jobs/min at concurrency 10** and **31.5 jobs/min at concurrency 20**. More clients did not increase useful throughput under a fixed worker limit. At C10, the first repetition's median queue time was **9.735 s** out of **13.016 s** end-to-end (**74.8%**). Successful results were checked against the correctness digest: **2,111 jobs**, all equal.

The A2 control shifted 1.8349% from A1 (21.8 → 22.2 jobs/min); the recorded drift flag remains in the evidence. Repetitions retain citable/invalid flags, quantile policies, raw latency arrays where available, rejection counts, terminal-persistence statistics, and throughput spread. Interpret a point in light of its flags and spread. Rejection counts depend on retry timing and are not rejection rates; no p99 claim is made.

## Recalculate or rerun

The compact JSON is a deterministic selection of recorded numeric evidence, not newly measured CI performance. It includes A1/B/A2 raw timing arrays, stage summaries, memory values, load repetitions, and environments. CI checks metric calculations and behavior; it does not reproduce historical timings.

```bash
python -m bench.readme_metrics all
python bench_stepwise.py --sizes 1682 30000 --repeat 3 --warmup 1
python -m bench.load_test --help
```

The first command recalculates the published values. The benchmark command generates local synthetic input and new JSON/Markdown reports; these outputs are ignored by Git. Use the load tool's preflight and memory gates before larger runs. Re-running the current implementation measures current behavior only, not the historical control variant.
