# Backup and quarantine restore

Status: Implemented for `LOCAL_REPRODUCIBLE` qualification.

RateReplay stores PostgreSQL custom-format dumps and a content-addressed object manifest in a separately configured encrypted object store.
The fixed backup retention period is 30 days.
Deletion does not rewrite retained backups, so deleted data can remain encrypted until the backup expires.
The separately protected deletion ledger prevents that retained data from becoming live again after a restore.

Hosted operation is not validated and no hosted-operation claim is made.

## Backup creation and verification

Configure the primary object store, the separately credentialed backup object store, PostgreSQL dump commands, and an exact 32-byte backup encryption key through the runtime secret paths described in `.env.example`.
Create a backup from a trusted worker host.

```console
ratereplay-worker create-backup
```

The command reports a backup identifier, database digest, manifest digest, object count, and fixed expiry.
Record that output in private operational evidence.
Verify the committed manifest and every encrypted database and object entry independently before relying on the backup.

```console
ratereplay-worker verify-backup BACKUP_ID
```

Apply the fixed retention policy from the separately scheduled retention worker.

```console
ratereplay-worker expire-backups-once
```

The retention command verifies that every object under an expired backup prefix is absent before reporting success.
Incomplete or unverifiable backup namespaces fail closed.

## Quarantine requirements

Never restore into a running primary database or a reachable object namespace.
Create a fresh PostgreSQL database and a fresh empty object-store namespace on an internal Compose network with no API, web, or proxy service attached.
Point `RATEREPLAY_DATABASE_URL` and the primary object-store variables at those quarantine destinations.
Point the backup variables at the existing separately credentialed backup location.
Mount the latest separately retained deletion ledger and every historical ledger and restore key referenced by its events as read-only secret input.
Keep the ledger volume itself writable because verification and reconciliation append encrypted access-audit and control records.

The PostgreSQL restore command must address only the quarantine database.
The command uses `pg_restore --clean --if-exists --no-owner --no-privileges --exit-on-error` after independently listing the custom archive.
The local reference topology does not configure PostgreSQL WAL archiving, so there are no post-dump transaction logs available to replay.
A deployment that adds WAL archiving must roll every available verified segment forward before running deletion reconciliation and must add a qualification test for that procedure.

## Complete restore command

Run one command after the empty quarantine services are healthy.

```console
ratereplay-worker restore-backup-to-quarantine BACKUP_ID \
  --materialization-directory /var/lib/ratereplay/quarantine/BACKUP_ID \
  --qualification-artifact-file /var/lib/ratereplay/private-evidence/restore-qualification.json \
  --exposure-artifact-file /var/lib/ratereplay/private-evidence/restore-exposure.json
```

The materialization directory must not exist before the command starts.
The command performs these checks in order:

1. It verifies the encrypted backup and committed manifest.
2. It materializes content-addressed database and object bytes into a new mode-0700 directory and rehashes every file.
3. It lists and restores the verified PostgreSQL custom archive into quarantine.
4. It restores exact object bytes only into an empty quarantine namespace and verifies the final key and digest set.
5. It verifies the restored Alembic revision.
6. It validates the latest encrypted deletion ledger and requires every referenced historical key.
7. It suppresses every `REQUESTED` or `COMPLETED` scope, reruns raw-object retention expiry, and holds every unresolved `PREPARED` event.
8. It writes and rereads the content-addressed qualification artifact.
9. It binds the exact backup manifest, database dump, redacted object set, ledger head, database revision, and qualification digest into a second content-addressed exposure artifact.

The command exits `0` only when the instance-bound artifact says `exposure_allowed=true`.
It exits `3` for a valid quarantine hold and `1` for an invalid backup, database, object set, ledger, key set, revision, or artifact.
No failure path starts or exposes an application service.

Authoritative transaction-outcome evidence for a prepared event can be supplied with `--outcome-evidence-file` only when it was produced by the separately trusted database-timeline procedure.
Missing, stale, ambiguous, or unverifiable evidence leaves the restore quarantined.

## Exposure gate

Verify the artifact again from the deployment controller before attaching any API, web, or proxy service to the restored network.

```console
ratereplay-worker verify-restore-exposure \
  --artifact-file /var/lib/ratereplay/private-evidence/restore-exposure.json
```

The artifact contains a random restore-instance identity and digests, not object keys, user identifiers, filenames, database URLs, or secret paths.
A standalone `restore-qualification-v1` artifact is diagnostic evidence and is not sufficient to expose a restore because it is not bound to one restored database and object namespace.
Changing the database, object namespace, ledger, or backup after binding invalidates the operational decision and requires a fresh quarantine restore.

## Failure recovery

Keep a failed materialization directory and its private logs until the failure is understood.
Do not reuse a partially restored database or nonempty object namespace.
Create new quarantine services and a new materialization directory for the retry.
An interrupted command may have appended valid access-audit records to the protected ledger, which is expected and does not authorize exposure.

A missing ledger, missing historical key, invalid record chain, unresolved preparation, nonempty object destination, changed backup, restore subprocess failure, or missing Alembic revision is a hard failure.
Elapsed time never resolves a prepared event.
Operators must not edit artifacts, ledger files, expected outputs, or restored lifecycle rows to clear a failure.

## Restore drill evidence

The Milestone 7 qualification drill uses PostgreSQL 16 and two independent MinIO namespaces under Docker Compose.
It starts with a backup that predates one deletion and one retention expiry, restores into a separate quarantine project, reapplies the protected ledger, and verifies absence through the public restore and exposure commands.
It also injects primary loss after `PREPARED`, after database fencing, and after `REQUESTED` and verifies the corresponding hold or suppression behavior.
The drill preserves missing-ledger and tampered-ledger failures and verifies 30-day backup expiry.

Only the evidence produced by that repository qualification command supports the `LOCAL_REPRODUCIBLE` claim.
Public ACME TLS, hosted storage durability, hosted encryption, hosted rollback, and hosted restore remain unvalidated until an explicitly authorized staging or production exercise passes.
