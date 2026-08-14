# Tariff compilation and replay methodology

RateReplay admits tariff behavior only for a bounded service window, a fully specified account predicate, and a source-complete component vector.
The current admitted boundary is PG&E E-1 historical replay for the half-open July 2026 service window `[2026-07-01, 2026-08-01)` and the account facts locked in `tariffs/admission/target-account-v1.json`.
This boundary does not admit alternative-plan comparison, optimization, forecasting, or a savings claim.

## Source and admission chain

Each difference-making rule links to a stable filed source identifier, source URL, content hash, source sheet, and effective range.
Source records and ordered component vectors are locked in `tariffs/sources.lock.json`.
The compiler rejects a missing source, a hash mismatch, a rule that is not assigned to exactly one component, and a component or rule that does not cover the complete admitted window.
Mutable tariffbook PDFs may support discovery, but only the stable source records in the lock can support compilation and admission.

Independent complete-bill and boundary goldens are frozen before production evaluator work.
The E-1 admission lock hashes the declarative definition and both golden suites, then records the exact compiler content hash that must be reproduced.
A changed definition, golden, source vector, or compiler result invalidates the admission lock instead of silently creating a new tariff version.

## Declarative schema and canonical IR

The tariff definition is validated by strict immutable Pydantic models that reject unknown fields and invalid tagged operators.
Dates use half-open ranges so every service instant has one unambiguous owner.
Rates use integer microdollars per declared unit, quantities use integer watt-hours or integer service days, and tier bounds remain explicit rational values.
The compiler proves declared units, signed 64-bit bounds, effective-date coverage, tier ordering, eligibility-lock identity, source composition, and golden coverage before emitting IR.

The normalized AST hash identifies the canonical declarative input.
The compiler content hash identifies the normalized AST, canonical IR, and complete report bundle.
Compilation emits normalized-AST, IR, eligibility, component-vector, source-coverage, and golden-coverage reports in one deterministic JSON object.

## Reference evaluation

The reference evaluator uses exact rational arithmetic until a named tariff rounding boundary.
The admitted E-1 operators allocate energy at the baseline boundary, multiply watt-hours by integer microdollar rates, multiply service days by an integer daily charge, and apply a bill-cycle-gated integer credit.
Each supported line carries its rule identifier, tariff version, source and sheet identifiers, exact quantity, exact rate, pre-round rational amount, half-up cent rounding operator, service window, and explanation key.
The supported total must equal the sum of emitted rounded line items for every accepted request.

Eligibility is tri-state.
Facts that contradict the locked predicate produce `INELIGIBLE`, facts outside the admitted knowledge boundary produce `UNKNOWN`, and only `ELIGIBLE` requests can be replayed.
Unknown or ineligible accounts never inherit a supported rate by default.

## Reconciliation and semantic identity

Current-bill reconciliation is optional and remains separate from supported tariff evaluation.
User-entered unsupported lines remain labeled and visible, and the signed unexplained residual is calculated as the entered bill total minus supported calculated charges and user-entered unsupported charges.
RateReplay never moves a residual into a supported line to force a match.

The manifest hashes the profile content, tariff compiler content, account facts, replay input, reconciliation input, and reconciliation policy.
Changing a bill total, unsupported-line tuple, or policy therefore creates a different semantic calculation identity without altering the supported tariff result.

## Authoring and qualification workflow

1. Lock stable source records and the ordered component vector for the proposed service window.
2. Freeze an independent complete-bill golden and boundary cases for every rule, applicability predicate, and rounding boundary.
3. Add a strict declarative tariff definition without changing the frozen expected values.
4. Compile through `uv run ratereplay-tariff compile-e1` and inspect every emitted report.
5. Replay the frozen request through `uv run ratereplay-tariff replay-e1 tariffs/examples/e1-replay-input.json`.
6. Add deliberate-invalid and mutation cases that prove gaps, overlaps, invalid tiers, unknown units, source mismatches, rates, dates, applicability predicates, and tier boundaries fail the intended evidence.
7. Create or update an admission lock only after every gate passes.
8. Run `make qualification-m2` and `make check` before committing the admitted slice.

The reproducible Milestone 2 result is stored in `evidence/correctness/m2-e1-qualification.json`.
Its claims apply only to the locked July E-1 account class and service window.
