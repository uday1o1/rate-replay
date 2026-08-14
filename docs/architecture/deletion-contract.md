# Prepared deletion and recovery contract

Status: Implemented and accepted in Milestone 5.

## Client intent

An authenticated client starts an account deletion intent with an idempotency key and a locally generated high-entropy receipt secret.
Authenticated import and profile deletion requests use the same idempotency and receipt-secret protocol through their resource-specific `DELETE` routes.
The API returns the deletion ID, the 15-minute expiry, and a server verifier while storing only a domain-separated SHA-256 digest and Argon2id verifier of the receipt secret.
Repeating the same owner, target, key, and receipt is idempotent.
A conflicting unexpired or prepared intent fails closed.
The receipt can consume the intent exactly once and can read deletion status after normal session revocation.
An unprepared intent expires, while a prepared intent remains recoverable.

The client must persist the receipt before asking to consume the intent.
If the consume response is lost, it queries status with the receipt and does not create a replacement account or intent.

## Control-plane and database ordering

The deletion coordinator writes immutable `PREPARED` to a separately backed-up control-plane ledger before opening the database fencing transaction.
The transaction changes the selected account, import, or profile from `ACTIVE` to `DELETION_PENDING_LEDGER`, increments its lifecycle generation, consumes the intent, fences ordinary work, and records the deletion control operation.
Only after that transaction commits may the coordinator append `REQUESTED`.
The generation-authorized deletion job drains all older-generation writers and uploads, sweeps target data while preserving its own lifecycle and control records, verifies absence, appends `COMPLETED`, and atomically marks the target `DELETED`.
An import deletion includes its profiles and their dependent results.
An account deletion includes every child resource and safely subsumes any import or profile deletion already in progress while preserving the child receipt and minimum audit tombstone.

`REQUESTED` and `COMPLETED` are suppressive during restore and can never coexist with an `ACTIVE` target.
`PREPARED` alone is not suppressive because the database transaction may not have committed.
An unresolved preparation keeps restore quarantined until the coordinator proves the fence committed and appends `REQUESTED`, or proves noncommit against an authoritative database timeline and appends `ABORTED`.

Crashes after preparation, after the fence, during sweep, after verification, and after terminal append resume from immutable phase checkpoints.
A sweep cannot delete the target lifecycle row, control operation, job attempt, checkpoint, receipt verifier, ledger event, or suppression state before verified terminal completion.
Restore reconciliation treats an account or import suppressive event as authoritative over subordinate child controls restored from the same older backup, removes those controls before sweeping the parent scope, and never exposes a restored target first.
The executable model and state-machine tests are in the domain package.
