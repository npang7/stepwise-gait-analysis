# Design decisions

Each record explains a concrete tradeoff in the current implementation:

1. [Delete queue-full runs](001-delete-queue-full-runs.md) — bounded scheduling without leaked upload directories.
2. [Vectorized parser with compatibility fallback](002-vectorized-parser.md) — memory efficiency and exact input semantics.
3. [Lazy processed CSV](003-lazy-processed-csv.md) — fast report creation while preserving downloadable full signals.
4. [Per-series plot envelope](004-per-series-plot-envelope.md) — bounded rendering that preserves extrema and gaps.
5. [Striped manifest transitions](005-run-striped-manifest-transitions.md) — serialize same-run updates without a global lock.
