# ADR 005: Serialize manifest transitions to prevent lost updates

## Decision

Serialize each run's complete manifest lifecycle with one of 32 striped reentrant locks. Select
the stripe deterministically from the canonical UUID. Hold the same stripe across manifest reads,
atomic writes, complete read-modify-write transitions, restart recovery decisions, rejected-run
deletion, and TTL eligibility checks through recursive removal.

The artifact lease guard is always acquired before a manifest stripe when both are needed. A
reentrant lock is required because transitions and recovery call the public, independently locked
read and write methods while already holding the run stripe.

## Context

Locking only the individual read and write calls leaves a platform-independent lost-update race:
two transitions can read the same prior state and the later replacement silently discards the
other transition. Windows also rejects `manifest.json.tmp -> manifest.json` replacement while a
status request has the destination open, making the same missing coordination visible as a
`PermissionError`.

Atomic replacement still prevents partial JSON from becoming visible; the stripe supplies the
missing transaction boundary and keeps readers from holding the destination open during replace.

## Scope boundary

The locks belong to one `JobRepository` instance and coordinate threads in its single service
process. They do not coordinate separate repository instances, Uvicorn processes, or replicas
sharing a data directory. Supporting that deployment requires an interprocess file lock or an
external state coordinator and is outside this change.

## Alternatives rejected

- **Retry replacement without locking.** Continuous readers can repeatedly recreate the conflict,
  and retries do not make read-modify-write transitions atomic.
- **One repository-wide lock.** It unnecessarily serializes status reads and transitions for
  unrelated runs and could distort concurrency measurements.
- **Rename an expired run and delete it after releasing the stripe.** The measured artifact shape
  does not justify adding a trash namespace, orphan recovery, and new crash semantics. Recursive
  removal remains inside the stripe; a rename-and-reap design can be reconsidered if future
  artifact growth makes that critical section material.
- **Cross-process file locking.** The documented service runs one Uvicorn process. Expanding its
  deployment model belongs in a separate design.

## Consequences

Transitions for one run have a deterministic order instead of silently overwriting one another.
Different UUID stripes remain concurrent. A status request that shares a stripe with cleanup can
wait for that run's removal, after which it observes the existing not-found behavior. Hash
collisions serialize a bounded subset of unrelated runs without changing their state or API
contract.
