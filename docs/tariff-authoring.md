# Tariff authoring

## Purpose

RateReplay tariff definitions are source-locked declarative inputs to a closed compiler.
Authoring a definition does not admit a tariff by itself.
Admission requires complete sources, eligibility, component coverage, goldens, mutation tests, optimizer equivalence, and a content-addressed lock.

The current V1 admits only July 2026 PG&E E-1, E-TOU-C, E-TOU-D, E-ELEC, and EV2-A for the locked bundled Tier 3 EV account predicate.
A new utility, service window, account predicate, or tariff revision requires a new source audit and separate acceptance gate.

## Repository artifacts

Each admitted tariff is represented by several independent files:

- `tariffs/sources.lock.json` records authoritative source identifiers, effective ranges, local snapshots, hashes, and redistribution policy.
- `tariffs/calendars` records source-locked holiday and exceptional-day calendars.
- `tariffs/definitions` contains declarative tariff versions.
- `tariffs/golden` contains complete-bill and boundary expected values.
- `tariffs/admission` contains the target account, candidate component matrix, per-tariff gate evidence, and compiler hash.
- `packages/tariffs` contains the closed schema, compiler, evaluator, comparison engine, and CLI.

Do not edit an admission lock merely to match a new compiler output.
A changed output requires explaining which source or declared rule changed and regenerating every affected independent expected value.

## Step 1: audit authoritative sources

Start from a stable regulator or utility identifier rather than a mutable tariffbook URL alone.
Record the document title, source owner, effective dates, service territory, sheet or rule identifier, retrieval location, retrieval time, content hash, redistribution decision, and the exact component it supports.

The component vector must cover every admitted service instant exactly once.
A source gap or overlap blocks compilation.
Time-of-use periods must lock the applicable timezone, weekdays, weekends, holidays, and exceptional days.

Update `docs/source-audit.md` when the source set or scope changes.
Keep restricted source files and customer material out of Git.

## Step 2: define the target account and comparison universe

Eligibility facts must be explicit, typed, dated where necessary, and fail closed when absent.
Do not infer enrollment, equipment, income, annual usage, service mode, or baseline territory from interval shape.

Classify every active charge component as supported or unsupported before ranking.
Unknown components are difference-making by default.
The candidate matrix must state whether each required component is present, source-complete, supported for replay, comparable, and admitted for optimization.

## Step 3: write the declarative definition

Create a new versioned JSON file under `tariffs/definitions`.
Use a new `tariff_version_id` for any difference-making rule or source change.
Reference only source IDs and calendar IDs present in the locks.

The compiler accepts the closed schema implemented in `packages/tariffs/ratereplay_tariffs/schema.py` and `ir.py`.
It rejects unknown fields and operators.
Rates and thresholds must convert to exact integer microdollars, watt-hours, seconds, dates, or rational expressions as declared by the schema.

Compile one definition with:

```sh
uv run ratereplay-tariff compile \
  tariffs/definitions/pge-etoud-2026-07.json
```

The output includes the normalized abstract syntax, source vector, eligibility report, component coverage, integer-bound proof, and compiler content hash.
A compiler error code is a failed authoring gate, not an instruction to bypass validation.

## Step 4: add independent goldens

Write complete-bill and boundary cases from the authoritative rules without importing the production evaluator.
Include every active component, eligibility boundary, time boundary, effective-date boundary, credit, minimum-bill rule, and rounding boundary used by the definition.

Expected energy and money must be exact.
Record the derivation steps and source IDs with the fixture.
Do not copy production output into an expected file.

Run the focused tariff tests:

```sh
UV_CACHE_DIR=/private/tmp/rate-replay-uv-cache \
  uv run pytest packages/tariffs/tests
```

Milestone 8 adds an independent five-tariff derivation runner under `benchmarks/reference` that imports no production calculation code.

## Step 5: add mutations and eligibility failures

For each difference-making rule, seed a contained incorrect value or boundary and require the intended mismatch.
Keep the unmodified nearby control passing.

Test missing eligibility facts, explicit ineligibility, an unknown active component, a missing calendar day, a source hash change, a component gap, a component overlap, a nonintegral energy conversion, and a numeric-bound violation where applicable.
Ranking must disappear rather than produce a partial winner.

## Step 6: prove replay and optimizer equivalence

Optimization admission requires the tariff to use only lowerable intermediate-representation operators.
Compare direct reference replay against the lowered objective across randomized valid profiles and account facts.
Add independent exhaustive-oracle cases that confirm the selected schedule belongs to the complete optimum set.

Every returned schedule must pass the separate replay verifier.
The off-peak heuristic remains a non-optimal baseline even when its cost happens to equal the exact result.

Run:

```sh
make qualification-m4
make qualification-m8-correctness
```

## Step 7: create the admission lock

Only after every prior check passes, create or update the versioned admission artifact under `tariffs/admission`.
It binds the definition hash, compiler content hash, source IDs and hashes, service window, target predicate, golden hashes, comparison status, optimization status, and gate evidence.

`load_admitted_tariff` rehashes every artifact, recompiles the definition, and compares the normalized version, sources, eligibility predicate, service windows, and compiler content hash.
Any mismatch fails with a stable admission error.

Update the candidate matrix and supported-charge documentation before advertising the tariff.
Do not add a plan to the public demo or README until the admission lock and complete qualification pass.

## Step 8: run the public and clean-checkout gates

Run the complete local verification and reproduce public artifacts:

```sh
make check
make qualification-m3
make qualification-m4
make demo-artifacts
make demo-artifacts-check
make clean-checkout-check
```

If a public number changes, update the generated result view from qualified evidence and preserve the older failed or superseded artifact.
Never edit expected output solely to make a regression pass.

## Review checklist

- Every source is authoritative, retrievable, hashed, effective-dated, and redistribution-reviewed.
- Every service instant has exactly one active component version.
- Every holiday and exceptional day is locked.
- Every account fact is explicit and missing facts fail closed.
- Every active component is classified before comparison.
- Every numeric value stays inside the exact integer contract.
- Independent goldens and mutations cover each difference-making rule.
- Direct replay and optimizer lowering agree.
- The separate verifier accepts controls and rejects seeded corruption.
- The admission artifact binds all definitions, sources, goldens, and compiler output.
- Public claims name the admitted account and service window.

See [tariff-methodology.md](tariff-methodology.md) and [billing-semantics.md](billing-semantics.md) for the normative calculation rules.
