# Local deployment and rollback

Status: Implemented and exercised at evidence level `LOCAL_REPRODUCIBLE`.

The release topology runs PostgreSQL, two separately credentialed SeaweedFS S3 services, one migration job, the API, the worker, the static web application, and Caddy.
Caddy is the only service with host-published ports.
The local qualification binds those ports to loopback and trusts a generated local certificate authority only inside the qualification client.
Hosted deployment, managed storage encryption, public ACME operation, hosted network isolation, and hosted orchestrator rollback remain explicitly unvalidated.

## Prerequisites

Use Docker Engine with the repository-selected `docker-compose` client and enough storage for both candidate and rollback images.
Keep generated secret sources under one ignored mode-0700 host directory.
The local Compose adapter requires each secret source file to be mode 0644 because the file mount remains root-owned while application containers run as UID 10001.
Only the named service receives each secret mount, and the containing directory prevents traversal by other host users.
Hosted operation must replace this local adapter with service-identity secret injection before any hosted claim is made.

Run the static configuration gates before starting services.

```console
make security dependency-audit operations-config-check release-config-check
```

Every candidate and rollback image must have an immutable source revision and recorded image identity.
The repository qualification builds every image from an immutable base digest and scans operating-system packages and detected language binaries with the pinned Trivy image.
Any critical finding fails the gate, and the qualification provides no ignore-file or ignore-status path.

## Deployment procedure

Create and verify an encrypted backup before changing the running application or database.
Follow [Backup and quarantine restore](backup-restore.md) for the backup and restore exposure contract.
Record the current application image identity as the rollback image.
Build and scan the candidate image, then render the exact Compose configuration before applying it.

Run the migration service as the only schema writer.
Do not start the API or worker if the migration exits nonzero or the observed Alembic head differs from the expected head.
Start the API and worker only after PostgreSQL, object storage, and the migration service satisfy their dependency conditions.
Attach Caddy only after the API and static web health checks pass.

Verify these conditions through the Caddy HTTPS endpoint:

1. `/readyz` returns `200` with the exact ready payload.
2. `/v1/meta` returns the expected public schema version.
3. Registration, session lookup, and a fresh login complete over HTTPS.
4. The static application returns the configured security headers.
5. Compose reports only `proxy` with a nonzero published host port.

## Fault behavior

Stopping the primary object store or PostgreSQL must make `/readyz` return the versioned `DEPENDENCY_UNAVAILABLE` problem with an empty witness and `Cache-Control: no-store`.
The static web application must remain available during either backend outage.
Readiness may return to success only after the dependency health check recovers.

The worker uses an `unless-stopped` restart policy behind Compose's init process.
The qualification sends SIGKILL to the worker workload child rather than invoking an operator stop through the Docker API.
The gate requires the restart counter to increase, the worker health check to recover, and the API to remain ready.

The API and worker expose metrics only on their internal networks.
The qualification requires the declared HTTP, readiness, queue-depth, lease-age, retry, and worker-run metrics.
It also requires structured JSON events and API and worker traces while probing all generated credentials and account secrets for telemetry leakage.

## Application rollback

Application rollback is permitted only when the prior application image supports the currently applied database schema.
Do not improvise a database downgrade.
Use a separately tested down migration or quarantine restore when a schema change is not backward compatible.

Set `RATEREPLAY_APP_IMAGE` to the previously recorded image and recreate only the API and worker.

```console
docker-compose --file compose.release.yaml up \
  --detach \
  --no-deps \
  --force-recreate \
  --wait \
  --wait-timeout 120 \
  api worker
```

Verify the running container image identities instead of trusting tags.
Require readiness through Caddy, continuity of a session created by the candidate, and a fresh login against the rolled-back application.
If any check fails, remove public exposure and follow the quarantine restore procedure.

## Repository qualification

Run the complete local exercise from a clean commit that is already present on the current origin branch.

```console
make qualification-m7-deployment
```

The command builds the candidate and frozen rollback images, performs all security scans, checks the migration head, starts an isolated release Compose project, exercises the public HTTPS path, injects the three failures, verifies observability, and rolls the API and worker back.
It always removes the isolated project, named test volumes, and generated local secrets before exiting.
It writes `evidence/reliability/m7-local-deployment.json` only after every assertion passes.
The artifact is self-hashed and records source commits, image identities, tool versions, input digests, fault outcomes, rollback duration, and the claims that remain withheld.

This artifact supports only the committed `LOCAL_REPRODUCIBLE` result on its recorded environment.
A failed command, missing artifact, hash mismatch, unsupported environment, or infrastructure timeout is not passing evidence.
