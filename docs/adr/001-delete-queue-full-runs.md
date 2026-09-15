# ADR 001: Delete run directories rejected by the in-memory queue

## Decision

If validation succeeds but the bounded in-memory queue rejects a submission, delete the newly
created run directory and its promoted inputs before returning `429 queue_full`. Staging files are
also removed by the API request's unconditional cleanup.

Keep `STEPWISE_MAX_QUEUE` at its existing default. That setting bounds waiting analysis work; it
does not bound concurrent HTTP uploads that have not yet reached the queue.

## Alternatives rejected

- **Retain a failed manifest and inputs until TTL cleanup.** The client never receives a run ID for
  a `429`, so it cannot inspect or retrieve that record. Retention consumes disk without providing
  an observable API resource.
- **Check whether the queue is full before creating the run.** A separate check races with the
  later enqueue. The authoritative operation remains `queue.Queue.put_nowait`, followed by cleanup
  if that atomic queue operation rejects the job.
- **Reduce the queue default.** This would reduce accepted work but would not constrain staging by
  concurrent request handlers. It would trade away useful burst capacity without solving the
  relevant exposure.

## Consequences

Rejected submissions leave neither a run directory nor staged input. Accepted jobs retain the
existing queue and worker semantics. Submission filesystem operations are serialized with a lock,
while `queue.Queue` continues to provide its own thread-safe enqueue operation.
