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

## Encrypted local ledger

The `LOCAL_REPRODUCIBLE` adapter stores one encrypted global JSONL chain outside the primary database and object-store backup inputs.
Clear record envelopes contain only the random ledger identifier, sequence, fixed record type, key version, 96-bit nonce, previous record hash, ciphertext, and record hash.
Deletion identities, opaque suppression tokens, generation values, proof digests, event receipts, timestamps, and access details remain inside AES-256-GCM ciphertext.
The associated data authenticates every clear envelope field, and a global record hash chain detects gaps, reordering, middle deletion, and ordinary truncation.

HKDF-SHA256 derives separate envelope-encryption, event-receipt, genesis, head, and active-marker keys from each exact 32-byte ledger master key.
The implementation rejects nonce reuse under a key and fails closed when a historical key version is unavailable.
Signed genesis and head files bind the ledger identity, last acknowledged record, current ledger key version, and current restore key version.
An authenticated stream tail written before a crash but not yet reflected in the signed head is recovered under the exclusive ledger lock.
Every other head mismatch fails closed.

Ledger and restore suppression secrets are loaded from separate version-named key directories.
`RATEREPLAY_DELETION_LEDGER_CURRENT_KEY_VERSION` selects the key used for new encrypted ledger records, and `RATEREPLAY_RESTORE_CURRENT_KEY_VERSION` selects the key used for new suppression tokens.
Every process loads `RATEREPLAY_DELETION_LEDGER_KEYS_DIR`, while the API, preparation reconciler, and restore qualifier also load `RATEREPLAY_RESTORE_KEYS_DIR`.
The single-file variables remain a development compatibility path and cannot be configured together with their matching directory.
Restore qualification verifies that every restore-key version referenced anywhere in the ledger is available before inspecting or mutating restored data.
It derives each candidate suppression token using the exact version recorded by that event.
The preparation reconciler likewise continues an older `PREPARED` event with its original restore-key version after the configured write version changes.
Missing historical keys fail closed with `RESTORE_KEY_VERSION_UNAVAILABLE` and leave the target unexposed.
V1 retains all historical ledger and restore read keys indefinitely because retained backups and unresolved preparations can reference them.
Key retirement is therefore report-only until backup retention and ledger state prove a version unnecessary.

### Rotation procedure

The operator first stages distinct exact 32-byte old and new key files in both versioned directories while every historical file remains present.
The operator records the SHA-256 digest of `deletion-ledger-head-v2.json` and invokes the following command from a trusted host with local access to the ledger volume.

```console
ratereplay-worker rotate-deletion-keys \
  --root /var/lib/ratereplay/deletion-ledger \
  --keys-dir /run/secrets/deletion-ledger-keys \
  --restore-keys-dir /run/secrets/restore-keys \
  --expected-ledger-key-version ledger-v1 \
  --new-ledger-key-version ledger-v2 \
  --expected-restore-key-version restore-v1 \
  --new-restore-key-version restore-v2 \
  --expected-head-sha256 HEAD_SHA256 \
  --artifact-file /var/lib/ratereplay/private-evidence/deletion-key-rotation.json
```

The command takes the ledger lock, validates the complete old history, requires all four staged key versions, rejects reused key material, and compares the signed head with the operator's expected digest.
It then appends one encrypted `KEY_ROTATION` control record under the new ledger key and atomically updates the signed head to both new write versions without rewriting earlier records.
An authenticated rotation tail left by a crash before the head update is completed by retrying the same command with the original expected head digest.
The command writes and rereads a content-addressed redacted artifact before reporting success.
After success, every API and worker process is restarted with the new current-version settings.
A process that still selects either old write version fails closed before another ledger operation.
The old ledger and restore keys remain read-only members of their keyrings under the V1 indefinite-retention policy.

Every successful read or mutation first validates the entire chain and durably appends an encrypted access-audit record with a fixed actor, fixed operation, random operation ID, timestamp, and prior chain position.
Access-audit records never contain deletion IDs, scope tokens, user IDs, paths, secrets, or free text.
If the audit cannot be persisted, the requested read or mutation does not proceed.

The runtime never silently opens the earlier plaintext v1 ledger.
An existing v1 ledger produces `LEDGER_FORMAT_MIGRATION_REQUIRED` until an explicit offline migration is completed.
The filesystem adapter does not claim protection against an attacker who can atomically replay the complete stream and every signed control file together.
Hosted operation therefore remains withheld until a separately credentialed conditional-append or externally witnessed adapter is implemented and qualified.
