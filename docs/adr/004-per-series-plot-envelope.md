# ADR 004: Use per-series min/max envelopes for plots

## Decision

Decimate each plotted signal independently. Split recordings into at most 4,000
contiguous time buckets and retain each signal's minimum and maximum sample from every
bucket in chronological order. Keep the first and last samples and a marker for each
non-finite gap. The complete processed frame remains the source for persisted artifacts;
only the arrays passed to Matplotlib are reduced.

At 360,000 samples and 100 Hz, a target of 4,000 buckets groups about 90 samples, or
0.9 seconds, into each bucket. A rendered plot at that resolution communicates trends and
extrema rather than exact cross-sensor values at a single instant. Exact aligned samples
remain available in `processed_gait_data.csv`.

## Alternatives rejected

- Fixed-stride sampling emits one shared timestamp per interval, but it can step over a
  short pressure peak or trough and misrepresent the signal.
- A shared set of min/max row indexes preserves cross-series timestamp alignment. Keeping
  the extrema for every series, however, can emit two indexes per series per bucket and
  substantially enlarge the plotting input. Limiting that shared set instead means some
  series lose their extrema.
- Aggregating each bucket to a mean retains a common timestamp but suppresses precisely
  the short pressure peaks that the stance plot must continue to show.

## Consequences

Different curves in one image may be rendered at different timestamps, so the image must
not be used to infer an exact P1-to-P2 relationship at an individual instant. Each curve
does preserve its own bucket extrema, endpoints, time order, and missing-data breaks. The
public artifacts and analysis calculations remain aligned and full resolution.
