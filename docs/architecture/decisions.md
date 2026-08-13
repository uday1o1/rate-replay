# Architecture decisions

Status: Accepted for V1.

## Money and exact arithmetic

Source energy enters the calculation domain only as integer watt-hours.
Tariff rates are integer microdollars per declared unit, and rational intermediate values remain exact until a named tariff rounding boundary.
Money presented to a user is signed integer cents.
The default named operator is half-up to the cent at each source-defined line-item boundary, and no floating-point value may enter a bill or objective.
All compiled coefficients, products, sums, and declared bounds must fit in signed 64-bit integers before an evaluator or solver receives them.

## Time

Canonical instants are signed 64-bit Unix nanoseconds in UTC, intervals are half-open, and durations are positive integer seconds.
The source timezone metadata and `America/Los_Angeles` tariff zone remain calculation inputs.
Classification uses the local instant derived from each UTC interval start.
An interval crossing a tariff boundary is rejected unless the source provides exact subinterval readings.

## Storage

PostgreSQL owns identities, lifecycle state, immutable calculation manifests, findings, job leases, and deletion fences.
Raw uploads and generated binary artifacts live behind an object-store interface with content hashes and lifecycle metadata in PostgreSQL.
The local adapter uses a dedicated filesystem directory, and the reference topology uses the pinned S3-compatible service in `compose.yaml`.
No calculation relies on object listing order, database row order, or generated persistence identities.

## Comparison

The compiler classifies every charge component before ranking.
Unknown or unsupported components are difference-making by default.
Only eligible tariffs with complete source vectors and complete support for all difference-making components can produce savings, a winner, or a recommendation.
Current-bill residuals and user-entered unsupported charges never migrate into alternative tariff results.

## Source composition

A mutable tariffbook PDF is discovery evidence only.
Each admitted tariff composes stable source identifiers into an ordered component vector whose effective ranges cover each admitted service instant exactly once.
Compilation rejects gaps, overlaps, source hash changes, missing holidays, and rule identifiers not present in the lock.

## Solver semantics

The optimizer lowers only operators from the canonical charge IR capability table.
It does not implement tariff meaning independently.
V1 solves four objectives lexicographically: supported cost, changed reference entries, completion index sum, and stable slot-order score.
Each optimum is fixed as an equality before solving the next stage.
The solver uses one worker, a fixed random seed, and a frozen deterministic-time limit for reproducibility.
The initial off-peak proxy chooses the earliest feasible off-peak slots, then partial-peak slots, then peak slots, with stable UTC slot order inside each class.
That proxy is a comparison baseline only and never carries an optimality claim.
Small instances use an independent bounded exhaustive enumerator that shares no constraint-construction code with the solver.

## Operation and semantic identity

An operation identity scopes retries for one owner, endpoint, idempotency key, and canonical request hash.
A semantic calculation identity hashes the calculation kind, canonical profile content hash, tariff component vector, account facts, calculation-contract versions, reconciliation policy, scenario inputs, and solver parameters.
Owner identity is excluded from semantic content but remains part of authorization and storage ownership.
Equal semantic hashes in different accounts may reuse immutable bytes only through separately owned references and must never share mutable state.
Any difference-making contract change produces a new semantic identity and leaves the older result immutable.

## Authentication and job scopes

Private V1 accounts use local usernames and passwords under the contract in `authentication-and-deployment.md`.
Ordinary jobs are owner-scoped and generation-authorized.
Deletion jobs are additionally authorized by the fenced target generation and may run while ordinary work is rejected.
The public demo creates no job because it is a static browser-only artifact set.

## Deletion and restore

The executable deletion state model in `packages/domain/ratereplay_domain/deletion_protocol.py` is the normative ordering specification until persistence implements the same protocol.
An external control-plane `PREPARED` record precedes the database fence, the fence precedes suppressive `REQUESTED`, and only proved noncommit permits `ABORTED`.
Restore remains quarantined while any preparation is unresolved and reapplies verified `REQUESTED` and `COMPLETED` records before service exposure.
Sweep checkpoints and control records remain outside the deletion target until terminal verification is atomic.
