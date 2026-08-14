# Milestone 5 evidence

State: `ACCEPTED` after the commands in this document pass and origin contains the verified milestone commit.

## Durable production path

Replay, comparison, scenario, report, retention, account deletion, import deletion, and profile deletion use durable PostgreSQL jobs with explicit scope modes, leases, attempt checkpoints, retry exhaustion, cancellation, and conditional terminal publication.
Operation idempotency is separate from versioned semantic-calculation identity, and accepted result uniqueness is owner scoped.
Attempt-scoped artifacts remain unpublished until a generation-fenced finalizer wins the result claim, while stale and crashed attempts leave sweepable artifacts.
The worker-kill, stale-finalizer, calculation-contract, semantic-replay-input, and cross-account result tests are in the API, worker, persistence, and PostgreSQL integration suites.

## Authorization and deletion

The generated authorization matrix covers replay, comparison, scenario, job, result, report, export, import, and profile resources and rejects cross-account direct and indirect identifiers.
Account, import, and profile deletion use stable receipt-secret-bound identities, external `PREPARED` evidence before the database fence, exact lifecycle generations, resumable sweep checkpoints, strong object verification, and terminal ledger evidence.
An account deletion subsumes child deletions without invalidating their session-independent receipts, and restore suppression removes subordinate controls restored with an older parent scope.
Tests cover response loss, failed preparation, failed `REQUESTED`, every worker phase, older writer and upload fences, restore quarantine, stable event identity, child-resource sweeps, and terminal receipt access after session revocation.

## Storage, retention, telemetry, and audit

The real object-store gate writes encrypted objects to the primary MinIO service and verifies strong read, listing, and deletion behavior.
The real backup gate creates a PostgreSQL custom-format dump, validates it through `pg_restore --list`, stores only encrypted payloads in a separate MinIO service, verifies the backup manifest, and expires the complete backup at its fixed deadline.
Confirmed and abandoned raw uploads have a fixed 24-hour maximum lifetime, while backup manifests declare a fixed maximum age of 30 days.
Audit events are immutable, schema-bound, owner-scoped content hashes without a free-form payload surface.
Telemetry accepts only fixed allowlisted labels and tests reject or redact interval values, bill values, credentials, identifiers, object keys, and arbitrary error text.

## Verification commands and observed results

Run:

```sh
make check
make integration-m5 RATEREPLAY_TEST_DATABASE_URL='postgresql+psycopg://...'
make integration-object-store \
  RATEREPLAY_TEST_MINIO_ENDPOINT=127.0.0.1:59000 \
  RATEREPLAY_TEST_MINIO_ACCESS_KEY_FILE=.local-secrets/minio_user \
  RATEREPLAY_TEST_MINIO_SECRET_KEY_FILE=.local-secrets/minio_password
make integration-backup \
  RATEREPLAY_TEST_MINIO_ENDPOINT=127.0.0.1:59000 \
  RATEREPLAY_TEST_MINIO_ACCESS_KEY_FILE=.local-secrets/minio_user \
  RATEREPLAY_TEST_MINIO_SECRET_KEY_FILE=.local-secrets/minio_password \
  RATEREPLAY_TEST_BACKUP_MINIO_ENDPOINT=127.0.0.1:59001 \
  RATEREPLAY_TEST_BACKUP_MINIO_ACCESS_KEY_FILE=.local-secrets/backup_minio_user \
  RATEREPLAY_TEST_BACKUP_MINIO_SECRET_KEY_FILE=.local-secrets/backup_minio_password \
  RATEREPLAY_TEST_BACKUP_PGDUMP_COMMAND_JSON='["docker","exec","-i","rate-replay-postgres-1","pg_dump","-U","ratereplay","-d","ratereplay"]' \
  RATEREPLAY_TEST_BACKUP_PGDUMP_VERSION_COMMAND_JSON='["docker","exec","-i","rate-replay-postgres-1","pg_dump"]' \
  RATEREPLAY_TEST_BACKUP_PGRESTORE_COMMAND_JSON='["docker","exec","-i","rate-replay-postgres-1","pg_restore"]'
```

The complete local suite passed 484 Python tests with 13 environment-gated skips and 85.18% coverage.
All 9 web tests passed, and formatting, linting, Python and TypeScript static analysis, Bandit, secret scanning, the production web build, Compose validation, and evidence validation passed.
The real PostgreSQL suite applied the current migrations, reported no new Alembic upgrade operations, and passed all 11 integration tests.
The primary MinIO object-store integration and the separate encrypted PostgreSQL backup integration each passed.

These results establish `LOCAL_REPRODUCIBLE` only.
No hosted encryption, retention, restore, or operational claim is made.
