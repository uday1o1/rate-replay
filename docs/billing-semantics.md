# Billing semantics

## Calculation time

RateReplay V1 performs historical replay over an admitted half-open service window.
Every canonical instant is UTC nanoseconds, while tariff classification uses the declared local timezone derived from each interval start.
An interval that crosses a tariff boundary is rejected unless the source supplies exact subinterval readings.

The July 2026 tariff set does not support a future billing period.
Flexible-load `HISTORICAL_ADDITION` results place a user-supplied load on admitted past timestamps as a counterfactual.
They are not usage forecasts, rate forecasts, or future savings estimates.

## Exact numeric model

Energy enters billing only as integer watt-hours.
Nonintegral source conversion fails with `NON_INTEGRAL_WATT_HOUR` and is never rounded into admission.
Rates use integer microdollars per declared unit.
Intermediate values remain integers or exact rational values until a named tariff line-item boundary applies half-up cent rounding.
Displayed money is signed integer cents.

The compiler proves signed 64-bit bounds before a tariff can reach the evaluator or optimizer.
Floating-point arithmetic is prohibited from bill values and solver cost objectives.

## Supported, unsupported, and residual values

A historical replay contains supported calculated charge lines, optional explicit user-entered unsupported lines, and a signed unexplained residual.
The reconciliation identity is:

```text
entered bill total
  = supported calculated subtotal
  + explicit unsupported subtotal
  + signed unexplained residual
```

The residual remains visible because RateReplay does not invent a charge to force agreement with an entered total.
An explicit unsupported line is a current-bill reconciliation input only.
Neither it nor the residual is copied into an alternative tariff result.

The absence of a residual in a redacted scenario report does not establish complete utility-bill equivalence.
It means that report was built from the supported scenario calculation contract rather than an entered current-bill total.

## Tariff source composition

Each admitted tariff version is a declarative component vector linked to immutable source metadata and content hashes.
The vector must cover every service instant exactly once with no gap or overlap.
Time-of-use tariffs also lock their holiday and exceptional-day calendar dependency.

The compiler rejects a changed source hash, missing component interval, ambiguous effective date, unsupported operator, invalid eligibility predicate, or out-of-range integer expression.
The resulting compiler content hash identifies the normalized tariff meaning used by replay and optimization.

## Eligibility and comparison

Each candidate is evaluated against the same canonical profile, account facts, dated eligibility facts, and required component universe.
Eligibility can be `ELIGIBLE`, `INELIGIBLE`, or `UNKNOWN`.
Missing facts produce `UNKNOWN` and fail closed.

Every active component is classified before ranking.
Unknown and unsupported components are difference-making by default.
A winner and supported-charge difference appear only when every candidate is eligible and every difference-making component has complete support and source coverage.

The displayed value is the difference between supported historical subtotals under the locked comparison contract.
It is not a prediction of the customer's complete bill.
It is not a recommendation outside the admitted account predicate or service window.

## Reference and scheduled profiles

Every flexible load contains a complete user-supplied reference schedule.
RateReplay does not infer that reference from household behavior.
The reference supplies the unchanged comparison baseline and is validated for energy, timing, power, and overlap constraints before solving.

`SHIFT_EXISTING` requires the reference energy to exist inside the measured profile so it can be removed and rescheduled without producing a negative fixed background.
`HISTORICAL_ADDITION` leaves the measured profile as fixed background and adds the declared load as a past-period counterfactual.

The decomposition must reconstruct the measured profile exactly.
Any negative background, double counting, missing energy, or invalid reference blocks solver construction.

## Solver and verifier status

The exact solver uses four lexicographic stages:

1. Minimize supported cost.
2. Minimize changed reference entries.
3. Minimize the completion-index sum.
4. Minimize a stable slot-order score.

`OPTIMAL` means all four declared stages were proved optimal.
`BEST_FOUND` identifies the first open stage and exposes the applicable cost gap when available.
An unsuccessful model publishes no schedule.

The off-peak heuristic is a deterministic comparison baseline and never carries a bill-optimality claim.
Every schedule eligible for display passes a separate verifier that checks energy conservation, electrical constraints, schedule feasibility, and fresh tariff replay.
Verifier failure suppresses the schedule rather than degrading to an unverified result.

## Redacted report

The public redacted report contains aggregate energy, supported component totals, tariff provenance, solver and verifier status, result hashes, and fixed limitation codes.
It excludes exact interval timestamps, daily series, load identifiers, object keys, source identifiers, and the exact load schedule.
Its schema and allowlist are versioned and tested against prohibited fields.

See [tariff-methodology.md](tariff-methodology.md) for source-specific numeric and admission rules and [tariff-authoring.md](tariff-authoring.md) for the authoring workflow.
