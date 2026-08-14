# Milestone 2 evidence

State: `ACCEPTED`.

Evidence level: `LOCAL_REPRODUCIBLE` for the locked July 2026 PG&E E-1 historical replay slice.
No hosted deployment, current-bill equivalence, alternative-plan comparison, optimization, forecast, recommendation, or savings claim is made.

## Gate evidence

- The strict typed tariff definition compiles to immutable canonical integer IR and emits normalized-AST, IR, eligibility, component-vector, source-coverage, and golden-coverage reports.
- Two independent compilations produce compiler content hash `ae003e7717fbb8fa964aac75ba21efa737f4db54bdba2abcb90b1a22d81a0016` and identical complete bundles.
- The two locked component versions each cover the complete half-open service window `[2026-07-01, 2026-08-01)` exactly once.
- Deliberate component gaps, component overlaps, invalid tiers, unknown units, and source-hash mismatches fail with their intended stable error codes.
- Every admitted rule is linked to Advice 7846-E or Advice 7921-E and at least one independently frozen golden case.
- The complete-bill and ten boundary cases exercise baseline allowance, tier allocation, income-tier applicability, fixed daily charge, bill-cycle credit applicability, and half-up cent rounding.
- The 310 kWh frozen request emits line results of 6,561, 4,416, 2,460, and -3,618 cents for a supported total of 9,819 cents.
- The supported total equals the sum of the four emitted line items.
- With an entered bill total of 11,000 cents and a user-entered unsupported line of 200 cents, the signed unexplained residual remains visible as 981 cents and the result is labeled `REVIEW_REQUIRED`.
- The manifest records distinct reconciliation-input, reconciliation-policy, account-facts, replay-input, tariff-compiler, profile-content, and calculation hashes.
- Ten deliberate rate and boundary mutations each fail compilation, eligibility, or an intended frozen golden.
- Registration, profile ownership, confirmed-profile resolution, CSRF rejection, owner-scoped idempotency, persistent replay publication, replay retrieval, and cross-owner rejection pass through the public API.
- Fresh PostgreSQL 16 migrations through revision `0003`, Alembic model-drift detection, and the authentication, import, and replay integration suites pass on the real database backend.
- The actual browser workflow loads an owned confirmed profile, displays locked account facts and source provenance, submits an E-1 replay, and renders supported lines, unsupported input, the signed residual, and calculation hashes.
- The responsive 390 px browser qualification replayed the owned 750 kWh simulated profile to 27,728 supported cents without horizontal page expansion, while the audit table remains intentionally scrollable.
- Browser text and assertions confirm the E-1 result contains no plan recommendation or savings claim.

## Reproduction

Run `make qualification-m2` to regenerate `evidence/correctness/m2-e1-qualification.json` through the public tariff CLI and execute the focused compiler, evaluator, mutation, CLI, and replay-API suites.

Run `uv run ratereplay-tariff compile-e1` to inspect the deterministic compiler bundle.

Run `uv run ratereplay-tariff replay-e1 tariffs/examples/e1-replay-input.json` to reproduce the complete-bill result, visible unsupported line, residual, and exact manifest hashes.

Run `make check` for formatting, lint, static analysis, the complete default test suites, security scans, the production web build, Compose validation, and evidence validation.

Start the pinned PostgreSQL service and run `RATEREPLAY_TEST_DATABASE_URL=postgresql+psycopg://ratereplay:<local-password>@127.0.0.1:55432/ratereplay make integration-m2` for migrations, drift detection, and real PostgreSQL authentication, import, and replay qualification.

Run `make clean-checkout-check` to reproduce the complete repository gate from the staged Git tree.

The evidence commit is the commit containing this document and is verified against `origin/main` immediately after push.
