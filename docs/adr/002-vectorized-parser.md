# ADR 002: Vectorized parser with a narrow compatibility fallback

## Context

Recordings have 15 fields per row. Compatibility includes malformed rows, text headers, coerced numeric values, and Python's extended line separators. A faster parser must preserve these semantics rather than silently accepting extra data.

## Decision

Use pandas' C CSV engine with string fields, default NA recognition disabled, quoting disabled, and excess-field rows skipped. Retain rows only when every field is nonempty and `Sample` satisfies `str.isdigit()`.

Search the text once for separators recognized by Python's `splitlines()` but not the CSV reader. If present, use the compatible line-filter fallback. Ordinary malformed input remains on the vectorized path.

## Validation

Short rows are padded with empty strings, not NA, so `isna()` alone would accept truncated input. The empty-field predicate drops the 5- and 8-field rows in `malformed_interleaved.txt`, matching the eight-row oracle and its single coerced NA. Oracle tests compare every column, dtype, and value, including special separators and large generated recordings. CI tests low/high numeric dependency bounds.

## Consequences

The vectorized path avoids per-field Python string lists and reduces peak memory. Structural validation still has a cost: the controlled 360,000-sample comparison measured parse at 2.5756 → 2.7544 seconds, **6.94% slower**, while a separate 64 MiB input measurement showed peak API process memory at **1.52 GB → 899 MB**. These separate sessions are documented in [performance evidence](../PERFORMANCE.md).

This decision favors memory efficiency and exact compatibility over a claim of parser-only speedup. End-to-end improvements also come from feature computation and artifact generation.
