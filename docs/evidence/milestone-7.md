# Milestone 7 evidence

State: `IMPLEMENTED_PENDING_GATE`.

Every Milestone 7 software deliverable and locally executable automated gate is implemented and passing.
Sequential acceptance remains pending because Milestone 6 is `HUMAN_VALIDATION_DEFERRED` until five genuine uncoached sessions are recorded and at least four pass.
No synthetic session is counted as human evidence.

## Reliability and security surface

The API enforces fixed authentication, upload, mutation, and read budgets with sanitized `429` problems and bounded in-memory identity storage.
Trusted proxy handling accepts forwarding data only from configured proxy networks, and the release Caddy service overwrites client forwarding headers.
The web client polls durable jobs within the read budget and honors bounded `Retry-After` values.

The release topology pins every base image by digest, runs application services as non-root with dropped capabilities and read-only filesystems where supported, mounts service-specific secret files, publishes only Caddy, and separates edge and backend networks.
The pinned Trivy scan checks operating-system and detected language components for all candidate, rollback, PostgreSQL, object-store, proxy, and web images without an ignore path.
The locked Python and pnpm audits report no known vulnerabilities.
Bandit and the credential-pattern scan report no findings.

The API and worker emit fixed-label metrics, structured JSON events, and traces.
The qualification observes HTTP requests, readiness, queue depth, lease age, retries, worker runs, API spans, and worker spans while probing generated credentials and account secrets for telemetry leakage.
The committed dashboards and alerts validate against the versioned SLI contract.

## Restore and rollback evidence

`evidence/reliability/m7-local-restore-rollback.json` is a self-hashed `LOCAL_REPRODUCIBLE` artifact from source commit `36fd8742a090cbbc270f010e5c4d0965d1b3794c`.
It starts from an encrypted PostgreSQL and object backup created before deletion and retention expiry, restores into a distinct quarantine topology, reapplies the separately protected ledger, suppresses deleted scopes, expires raw data, and binds the exposure decision to the restored instance.
It passes missing-ledger, tampered-ledger, unresolved-preparation, and three primary-loss injection cases.
It proves the 30-day backup boundary by retaining the backup one microsecond before expiry and removing every backup object at the deadline.
It also exercises a safe migration rollback and re-upgrade while preserving the stable row.

`evidence/reliability/m7-local-deployment.json` is a self-hashed `LOCAL_REPRODUCIBLE` artifact from source commit `7539819b64bf40778a2453e1d48a3971a20ed893` with rollback source `0bf962848c206c96920eae71aa1a5c666fb0f23a`.
It records zero critical container findings, zero ignored critical findings, and successful dependency, static, and credential checks.
Only the proxy has a published host port, and the test client completes registration, session lookup, and static application loading over locally trusted HTTPS.
Object-store and PostgreSQL outages return safe dependency problems while the static application remains available.
A SIGKILL of the worker workload increments the restart counter, restores worker health, and leaves the API ready.
The rollback recreates only the API and worker with the stable image, preserves the candidate session, permits a fresh login, and completed in 6392.609 milliseconds on the recorded arm64 environment.

The following claims remain explicitly withheld in the artifacts and public documentation:

- `HOSTED_VALIDATED`
- `MANAGED_VOLUME_ENCRYPTION`
- `PRODUCTION_ACME_TLS`
- `PRODUCTION_NETWORK_ISOLATION`
- `PRODUCTION_ORCHESTRATOR_ROLLBACK`
- real WAL or point-in-time recovery

## Verification commands and observed results

Run:

```console
make check
make dependency-audit
make qualification-m7-restore
make qualification-m7-deployment
```

The final local suite passed 571 Python tests with 13 environment-gated skips and 85.09 percent coverage.
All 18 web unit tests and 11 Playwright browser journeys passed.
Formatting, Python and TypeScript linting and strict static analysis, Bandit, credential scanning, the production web build, both Compose configurations, operations validation, release validation, demo reproduction, frozen study-protocol validation, and repository evidence validation passed.
The locked Python and pnpm dependency audits reported no known vulnerabilities.

These results establish only the recorded `LOCAL_REPRODUCIBLE` scope.
They do not satisfy the deferred genuine human study and do not authorize a hosted claim or publication.
