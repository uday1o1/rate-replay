# Milestone 1 evidence

State: `ACCEPTED`.

Evidence level: `LOCAL_REPRODUCIBLE` for canonical ingestion and the private import workflow.
No hosted deployment, tariff replay, bill-accuracy, or savings claim is made.

## Gate evidence

- The production XML pull parser validates every supported ESPI resource against the locked schema, resolves the relationship graph independently of Atom entry order, and rejects DTDs, entities, excessive depth, oversized input, malformed graphs, unsupported semantics, wrong commodities, duplicates, overlaps, and nonintegral energy.
- The locked PG&E CSV adapter accepts the sanitized 5,664-row provider fixture, admits hourly and 15-minute energy, resolves 23-hour and 25-hour Pacific days, and rejects unknown fingerprints, units, ambiguous clocks, duplicate intervals, overlaps, nonmonotonic input, and nonintegral energy.
- Adapter-independent normalization persists exact integer watt-hours, UTC nanoseconds, direction, source semantics, quality flags, local-time metadata, and stable safe findings without filenames, utility identifiers, or source identifiers.
- The canonical profile-content suite proves stability under random persistence identities, source references, and independent Atom entry order, and sensitivity to calculation-relevant readings, findings, policies, billing periods, timezones, and warning acknowledgements.
- Confirmation rejects incomplete, gapped, overlapping, mixed-resolution, unattested, or incompletely acknowledged billing periods.
- Draft readings, findings, and confirmed profile versions are immutable at the application persistence boundary.
- Local username and password registration, canonicalization, Argon2id hashing, login rotation, idle and absolute expiry, same-origin enforcement, CSRF rejection, logout revocation, hardened cookies, bounded authentication work, and upload throttling pass through the public API.
- The generated authorization matrix denies unauthenticated upload, missing CSRF, cross-owner import reads, cross-owner confirmation, cross-owner profile reads, and cross-owner profile listing.
- Import submission returns `202 Accepted` only after an owner-scoped idempotency record, raw-object record, import record, durable job, and captured lifecycle generations commit.
- Duplicate submissions return the original operation for an identical canonical payload and reject a reused key for a different payload.
- PostgreSQL leasing uses `FOR UPDATE SKIP LOCKED`, 20-second leases, attempt rows, heartbeat extension, bounded deterministic backoff, cancellation, lifecycle-generation fences, and conditional terminal publication.
- The recovery suite injects termination before parsing, during parser streaming, before draft publication, and immediately after atomic publication across ten cases.
- The measured conservative recovery upper bound was 21,031.514375 milliseconds against the frozen 30,000-millisecond threshold, with zero duplicate draft rows and zero duplicate terminal results on the named Apple M5 developer machine.
- Confirmed raw objects enter immediate two-phase deletion, failed deletion remains retryable as `DELETE_PENDING`, and abandoned raw objects expire at the 24-hour maximum.
- The real migrated PostgreSQL qualification passed both authentication and durable import execution with no Alembic model drift.
- The public browser workflow passed registration, locked-fixture upload, queued review, execution through `ratereplay-worker run-once`, quality refresh, explicit PG&E attestation, confirmation, raw deletion, and the visible `CONFIRMED` state.
- Desktop and responsive rendered inspections found no overlap, clipping, inaccessible confirmation state, or misleading hidden acknowledgement.
- Captured application logs contained neither the private filename marker nor the locked fixture's interval-value marker.

## Reproduction

Run `make check` for formatting, lint, type checking, unit and integration tests that do not require external services, security scans, the production web build, Compose validation, and evidence validation.

Run `make benchmark-m1-recovery` to regenerate the durable recovery measurement in `evidence/performance/m1-import-recovery.json`.

Start the pinned PostgreSQL service and run `RATEREPLAY_TEST_DATABASE_URL=postgresql+psycopg://ratereplay:<local-password>@127.0.0.1:55432/ratereplay make integration-m1` for migrations, drift detection, and real PostgreSQL authentication and import qualification.

Run `scripts/clean-checkout-check.sh` to reproduce the repository gate from the staged Git tree.

The evidence commit is the commit containing this document and is verified against `origin/main` immediately after push.
