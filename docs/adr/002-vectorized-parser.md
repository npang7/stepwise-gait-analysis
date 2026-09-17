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

On the 360,000-sample measurement, the median whole-text regex search took 0.0650 seconds and the
median empty-field/digit-mask/filter operation took 0.2378 seconds. Their combined median was 0.3027
seconds, or 9.66% of the paired 3.133-second parse median. The committed measurement JSON contains
all three raw runs and the corresponding results for every benchmark size.

## Superseded by PR #11

The section 5.3 session was originally summarized as leaving parse performance essentially unchanged
at `1.002x`. PR #11 supersedes that conclusion with a controlled, single-session A-B-A measurement:
the before control mean was 2.5756 seconds and the after median was 2.7544 seconds, a 0.1788-second
(`6.94%`) regression. The earlier session's 180,000-sample TOTAL spread reached 63% and its timing
pair was already classified as unusable; PR #11's A1/A2 360,000-sample symmetric difference was
0.6564%.

Within the section 5.3 session, the measured structural-correctness overhead was 0.3027 seconds:
0.0650 seconds for the separator-regex scan and 0.2378 seconds for mask/filter work. That overhead
acts in the opposite direction from the C-engine parsing benefit. These figures and PR #11's net
parse regression come from different sessions and must not be subtracted, so this ADR does not assign
a numeric value to the C-engine benefit. The resulting tradeoff is a peak-RSS reduction from 1.52 GB
to 899 MB at the cost of parse running 6.94% slower.
