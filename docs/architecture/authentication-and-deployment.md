# Authentication and deployment ADR

Status: Accepted for V1 at evidence level `LOCAL_REPRODUCIBLE`.

## Local authentication

Usernames receive one ASCII-lowercase canonicalization pass and then must match `[a-z0-9_]{3,64}`.
Passwords must contain 12 through 128 UTF-8 characters and are never normalized or truncated.
Passwords use Argon2id with version 19, time cost 3, memory cost 65,536 KiB, parallelism 4, a 16-byte random salt, and a 32-byte hash.
The Milestone 0 benchmark result and named hardware are recorded in `evidence/performance/m0-feasibility.json`.
Authentication cookies are host-only, `Secure`, `HttpOnly`, and `SameSite=Strict`, and all state-changing requests require a same-origin CSRF token.
V1 stores no email address and has no credential recovery.
The product warns before private upload that a lost password makes the account unrecoverable.

## Reference hosted topology

The reference host is Ubuntu Server 24.04 LTS on x86-64 with Docker Engine 29.5 and Compose 5.4.
The reverse proxy is Caddy 2.10 and is the only process exposing TCP ports 80 and 443.
Caddy redirects HTTP to HTTPS, obtains and renews certificates through ACME, and applies the versioned security-header policy.
The web, FastAPI, and worker containers communicate only on an internal network.
PostgreSQL is `postgres:16.10-alpine3.22` at the content digest in `compose.yaml`.
The S3-compatible object store is `minio/minio:RELEASE.2025-09-07T16-13-09Z` at the content digest in `compose.yaml`.
Application images are addressed by registry digest in any hosted manifest.

The mandatory local path supports both arm64 and x86-64 Docker hosts.
Milestone 0 verified the selected PostgreSQL and object-store images on arm64.
Hosted operation remains an unclaimed specification until separately authorized and validated.

## Secrets and TLS

Runtime secrets enter through root-readable files mounted under `/run/secrets` and are never image layers, environment-file commits, command-line arguments, or logs.
Production TLS uses ACME through Caddy with the operator-controlled DNS and contact identity.
Local qualification uses a repository-generated development certificate authority that is trusted only by the test client.

## Migrations, backup, rollback, and teardown

The named service operator owns migrations and backups.
A deployment first records image digests, takes and verifies an encrypted PostgreSQL and object-store backup, runs one Alembic migration job, and then replaces application containers.
The migration job is single-writer and fails the deployment on any revision mismatch.
Application rollback is permitted only while the deployed schema declares backward compatibility with the previous application digest.
Schema rollback uses a separately tested down migration or restore, never an ad hoc production edit.

Backups use PostgreSQL custom-format dumps plus a content-addressed object manifest, are encrypted before transfer, and use separately credentialed object storage.
The service operator owns the encryption key and quarterly restore drill.
Backup retention is at most 30 days, while verified deletion ledger records are retained separately for restore suppression.

The reference monthly infrastructure ceiling is USD 40 excluding domain registration.
Exceeding the ceiling requires a new ADR before any hosted claim.
Teardown exports the final verified backup if retention policy requires it, revokes object-store and ACME credentials, removes only the named Compose project and its named volumes, deletes DNS records, and terminates the named host.
No teardown procedure uses a broad recursive filesystem target.
