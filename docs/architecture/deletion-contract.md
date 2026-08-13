# Prepared deletion and recovery contract

Status: Frozen before authentication and persistence.

## Client intent

An authenticated client creates one deletion intent with an idempotency key and a locally generated high-entropy receipt secret.
The API returns the deletion ID, the 15-minute expiry, and a server verifier while storing only a domain-separated SHA-256 digest and Argon2id verifier of the receipt secret.
Repeating the same owner, key, and receipt is idempotent.
A conflicting unexpired or prepared intent fails closed.
The receipt can consume the intent exactly once and can read deletion status after normal session revocation.
An unprepared intent expires, while a prepared intent remains recoverable.

The client must persist the receipt before asking to consume the intent.
If the consume response is lost, it queries status with the receipt and does not create a replacement account or intent.

## Control-plane and database ordering

The deletion coordinator writes immutable `PREPARED` to a separately backed-up control-plane ledger before opening the database fencing transaction.
The transaction changes `ACTIVE` to `DELETION_PENDING_LEDGER`, increments lifecycle generation, consumes the intent, fences ordinary work, and records the deletion control operation.
Only after that transaction commits may the coordinator append `REQUESTED`.
The generation-authorized deletion job drains all older-generation writers and uploads, sweeps target data while preserving its own lifecycle and control records, verifies absence, appends `COMPLETED`, and atomically marks the target `DELETED`.

`REQUESTED` and `COMPLETED` are suppressive during restore and can never coexist with an `ACTIVE` target.
`PREPARED` alone is not suppressive because the database transaction may not have committed.
An unresolved preparation keeps restore quarantined until the coordinator proves the fence committed and appends `REQUESTED`, or proves noncommit against an authoritative database timeline and appends `ABORTED`.

Crashes after preparation, after the fence, during sweep, after verification, and after terminal append resume from immutable phase checkpoints.
A sweep cannot delete the target lifecycle row, control operation, job attempt, checkpoint, receipt verifier, ledger event, or suppression state before verified terminal completion.
The executable model and state-machine tests are in the domain package.
