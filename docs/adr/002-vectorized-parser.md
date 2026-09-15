# ADR 002: Use a vectorized parser with a narrow compatibility fallback

## Decision

Parse ordinary StepWise recordings with pandas' C CSV engine. Read all 15 fields as strings,
disable default NA recognition and CSV quoting, skip rows with too many fields, then select valid
rows with two vectorized predicates: no field may equal the empty string, and `Sample` must satisfy
`str.isdigit()`.

Before invoking pandas, search the complete text once for the additional line separators recognized
by Python's `splitlines()` but not by the CSV reader. If one is present, use the preserved line-filter
parser. This is the only compatibility fallback.

## Evidence behind the row filter

Exploration on pandas 2.3.3 showed that `dtype=str, keep_default_na=False` pads short rows with empty
strings rather than NA values. An `isna().any(axis=1)` filter therefore retained both truncated data
rows in `malformed_interleaved.txt`, producing 10 rows instead of the reference parser's 8.

Replacing that predicate with `(frame == "").any(axis=1)` identified the fixture's 5-field and
8-field data rows. It produced 8 rows with exactly one coerced NA in `P1`, matching the reference
exactly. It produced no false positives across the well-formed fixture matrix or when trailing spaces
and tabs were added to every line. The same exploration confirmed that `on_bad_lines="skip"` removes
rows with more than 15 fields.

## Consequences

Well-formed recordings avoid the Python loop and its per-field string lists. Inputs containing the
special separators pay for the single whole-text search and the original line parser. Malformed
ordinary inputs remain on the C-engine path and retain the established drop-versus-coerce behavior.

The permanent parser oracle compares every column, dtype and value exactly, including malformed and
large fixtures. Benchmark evidence records the whole-text search plus vectorized row filtering as a
separate structural-correctness cost so that the performance tradeoff is measured rather than
assumed.
