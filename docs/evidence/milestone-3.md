# Milestone 3 evidence

State: `ACCEPTED`.

Evidence level: `LOCAL_REPRODUCIBLE` for unchanged-usage comparison across five admitted PG&E tariffs for the locked July 2026 bundled Tier 3 EV account.
No hosted deployment, complete-current-bill equivalence, forecast, flexible-load optimization, or result outside this account and service window is claimed.

## Gate evidence

- E-1, E-TOU-C, E-TOU-D, E-ELEC, and EV2-A are admitted for the same half-open service window `[2026-07-01, 2026-08-01)`.
- Each admitted tariff reproduces its locked compiler content hash and independently frozen complete-bill, eligibility, time-boundary, component-version, and rounding evidence.
- The independent validator derives the four candidate golden suites without importing the production tariff schema, compiler, or evaluator.
- The frozen 2,976-interval simulated profile contains exactly 750,000 integer watt-hours.
- All five candidates evaluate as `ELIGIBLE` for the locked account and dated eligibility facts.
- The required comparable-component universe contains base services charge, baseline adjustment, bundled energy, California Climate Credit, and minimum-bill adjustment.
- The frozen comparison is rankable and has comparison hash `001ae25075ed7107b5a2951754fc9c6374a0756006c03e6682ffe64ea89ebb9d`.
- Supported subtotals are 27,728 cents for E-1, 30,253 cents for E-TOU-C, 26,021 cents for E-TOU-D, 30,278 cents for E-ELEC, and 26,890 cents for EV2-A.
- E-TOU-D has the lowest supported subtotal, which is 1,707 cents below the current E-1 supported subtotal under this locked comparable-cost contract.
- Removing the annual-usage fact yields `UNKNOWN`, suppresses the rank and winner, and emits no savings value.
- Removing E-TOU-C's active baseline-adjustment declaration yields `UNCLASSIFIED_ACTIVE_COMPONENT`, blocks ranking, and emits no savings value.
- Tightening the EV2-A annual-baseline eligibility ratio yields `INELIGIBLE`, blocks ranking, and emits no savings value.
- Current-replay user-entered unsupported charges and the signed residual remain present only in the current replay.
- Alternative-plan result schemas contain no current bill total, user-entered unsupported-line, or reconciliation field.
- Authenticated comparison creation requires a successful owner-scoped replay, CSRF protection, and an explicit unique candidate list.
- Comparison publication is immutable, owner scoped, semantically deduplicated, and operation-idempotent.
- Cross-account reads and attempts to use another account's replay return a non-disclosing `404` response.
- Fresh PostgreSQL 16 migrations through revision `0004`, Alembic model-drift detection, and all four database integration suites pass.
- Browser workflow tests submit the same account facts used by the current replay, exact dated facts, and all five explicit candidates.
- The rankable browser view shows the winner, supported-charge difference, eligibility, component coverage, and filed-source provenance.
- The blocked browser view shows machine-readable exclusions without a winning plan or savings value.

## Reproduction

Run `make qualification-m3` to regenerate `evidence/correctness/m3-comparison-qualification.json` through the public tariff CLI and execute the focused tariff, comparison, API, and browser suites.

Run `make check` for formatting, lint, strict static analysis, complete default tests, security scans, the production web build, Compose validation, and evidence validation.

Start the pinned PostgreSQL service and run `RATEREPLAY_TEST_DATABASE_URL=postgresql+psycopg://ratereplay:<local-password>@127.0.0.1:55432/ratereplay make integration-m3` for migrations, model-drift detection, and real PostgreSQL authentication, import, replay, and comparison qualification.

Run `make clean-checkout-check` to reproduce the complete repository gate and Milestone 3 qualification from the staged Git tree.

The evidence commit is the commit containing this document and is verified against `origin/main` immediately after push.
