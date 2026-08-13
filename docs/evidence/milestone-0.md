# Milestone 0 evidence

State: `ACCEPTED`.

Evidence level: `LOCAL_REPRODUCIBLE` foundation checks only.
No hosted deployment claim is made.

## Gate evidence

- The July 2026 E-1 vector and hand-derived complete-bill expected values are locked in `tariffs/sources.lock.json` and `tariffs/golden/e1-july-2026-complete-bill.json`.
- All five tariff families have source hashes and candidate admission records, and no candidate is marked `ADMITTED`.
- The only July holiday-dependent candidate, E-TOU-D, uses the source-locked July 3 observed holiday.
- The schema-valid independent ESPI fixture produced 362 hourly integer-watt-hour readings.
- The parser rejected external entities, wrong commodities, unsupported semantics, dangling links, invalid up links, duplicate links, and synthetic nonintegral energy with stable errors.
- The provider CSV review validated 5,664 provider rows, the exact BOM and header, `SAMPLE` redaction, kWh units, provider time labels, and integral watt-hour conversion.
- The canonical content property suite covered source reordering, random persistence identities, every profile field, and every reading field, while the diagnostic golden SHA-256 remained `60f888697a7f2df0457f9555006277e56849fea88eda2a8dcf4554ba3d4011c3`.
- The four-stage CP-SAT result matched the independent exhaustive oracle and was repeatable under locked parameters.
- The client intent and external deletion state-machine suite covered creation idempotency, expiry, consumption, lost response, post-session status, legal ordering, proved abort, sweep crash, terminal crash, and restore quarantine.
- The performance charter is machine-readable and freezes all required thresholds, hardware, topology, workload, cache, repetition, recovery, and duplicate-result fields.
- The measured M0 feasibility result passed on the named Apple M5 machine.
- The authentication, deployment, public-demo, calculation, identity, and deletion decisions are frozen under `docs/architecture`.
- The pinned PostgreSQL and MinIO images started on Linux arm64, passed real readiness queries, and were torn down with their disposable qualification volumes.
- The exact hashed Python lock and pnpm lock passed current vulnerability audits with no known vulnerabilities.

## Verification

The repository foundation clean-checkout check passed before commit `b496c19918e96c61369374f1f9c82e1f1f04d683` and that exact commit was confirmed on `origin/main`.
The complete Milestone 0 staged tree passed `make check`, dependency audits, the public CLI checks, and `scripts/clean-checkout-check.sh` before the evidence commit was created.
The evidence commit is the commit containing this document and is verified against `origin/main` immediately after push.

Measured M0 spike results are in `evidence/performance/m0-feasibility.json`.
The result is a feasibility measurement, not a user-facing scale claim.
