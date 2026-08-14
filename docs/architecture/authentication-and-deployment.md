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

## Request budgets and proxy identity

The single-process V1 API enforces sliding one-minute budgets before executing a route.
Authentication permits five attempts per effective connection and principal, upload permits ten submissions per owner, authenticated and unauthenticated mutations permit sixty requests, and reads permit 240 requests.
Every rejection uses the versioned problem schema, returns `429`, supplies an integer `Retry-After` value, and increments only a fixed-scope metric.
The limiter retains only HMAC digests and timestamps, discards expired buckets, caps active identifier storage at 4,096 entries, and sends rotating excess identities into one shared overflow budget.
The static public demo does not call this API and is unaffected.

The browser polls durable jobs no more than once per second and performs one bounded retry when a valid `Retry-After` value of 1 through 60 seconds is present.
This keeps ordinary long-running work below the read budget without automatically replaying a mutation.

Forwarding headers are ignored unless the immediate peer belongs to `RATEREPLAY_TRUSTED_PROXY_CIDRS`.
When a peer is trusted, the API selects the rightmost forwarded address outside every trusted proxy network.
The reference Caddy service must overwrite client forwarding headers, and its exact internal network must be configured rather than using a universal trust value.
Deployments with multiple API processes require a separately qualified shared limiter and are outside V1.

## Reference hosted topology

The reference host is Ubuntu Server 24.04 LTS on x86-64 with Docker Engine 29.5 and Compose 5.4.
The reverse proxy is Caddy 2.11.4 and is the only process exposing TCP ports 80 and 443.
Caddy 2.10.2 was the original Milestone 0 selection, but the 2026-08-14 Trivy database reports fixed critical OpenSSL, Go standard-library, gRPC, and Smallstep findings in that image.
The smallest security-preserving correction moves to the current official release in the same major line and retains the original Caddy topology and configuration objective.
Caddy redirects HTTP to HTTPS, obtains and renews certificates through ACME, and applies the versioned security-header policy.
The web, FastAPI, and worker containers communicate only on an internal network.
The release topology uses PostgreSQL 16.15 from a repository-owned image derived from the official immutable Alpine image.
The derived image removes the scanner-identified vulnerable `gosu` helper and runs the official entrypoint directly as the built-in `postgres` user.
PostgreSQL 16.10 remains the accepted earlier data-only fixture, while the current maintained patch release preserves the frozen PostgreSQL 16 major-version contract for deployment.
The release topology uses SeaweedFS 4.40 from the immutable official multi-architecture image as its S3-compatible object store.
The frozen MinIO community image remains only in the earlier data qualification fixture because its last community release has scanner-reported critical Go components and no patched community image.
SeaweedFS preserves the S3 adapter boundary and passes the pinned Trivy critical scan across its Alpine packages and Go binary without ignore rules.
Application images are addressed by registry digest in any hosted manifest.

The mandatory local path supports both arm64 and x86-64 Docker hosts.
Milestone 0 verified the selected PostgreSQL and object-store images on arm64.
Hosted operation remains an unclaimed specification until separately authorized and validated.

## Secrets and TLS

Runtime secrets enter through files mounted under `/run/secrets` and are never image layers, environment-file commits, command-line arguments, or logs.
The `LOCAL_REPRODUCIBLE` Compose path stores generated sources in an ignored mode-0700 host directory and uses mode-0644 files because file-backed Compose mounts preserve root ownership while the services deliberately run as non-root.
Only named services receive each mount, and host users cannot traverse the containing directory.
The application copies `pgpass` into its private container tmpfs at mode 0600 before libpq starts.
Hosted operation requires a managed secret injection mechanism with service-specific identities and remains unclaimed until separately validated.
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
