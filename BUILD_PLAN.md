# RateReplay Build Plan

Status: V1 implementation is complete through Milestone 9, with formal acceptance deferred at Milestone 6.

Planning snapshot: 2026-08-13.

## Implementation and acceptance status

This ledger separates implementation, automated verification, genuine human validation, and formal sequential acceptance.
The latest clean verification baseline is remote-confirmed commit `0d0ed1b7bc43a6905209fc214ed4a4cabf0edea5` on `origin/main`.
Its disposable clean-checkout workflow passed on 2026-08-14 in `America/Los_Angeles` with 605 Python tests passing, 13 environment-gated skips, 85.11 percent coverage, 18 web tests passing, and 11 browser journeys passing.
The same baseline passed dependency audits, current-tree credential scanning, a redacted gitleaks 8.30.1 scan across all 120 commits, the seeded credential control, the public-demo artifact checks, and the 110.24-second product-video check.

| Milestone | Implementation | Automated or local verification | Acceptance state | Exact deferred or blocked condition |
| --- | --- | --- | --- | --- |
| 0 | Complete | Full gate passed | `ACCEPTED` | None |
| 1 | Complete | Full gate passed | `ACCEPTED` | None |
| 2 | Complete | Full gate and source-linked golden qualification passed | `ACCEPTED` | None |
| 3 | Complete | Five-tariff comparison qualification passed | `ACCEPTED` | None |
| 4 | Complete | Optimizer, verifier, seeded-defect, and portfolio-core qualification passed | `ACCEPTED` | None |
| 5 | Complete | Authorization, durable-job, deletion, restore, retention, and privacy gates passed | `ACCEPTED` | None |
| 6 | Complete | Automated UI, accessibility, stateless-demo, degraded-flow, and report-redaction checks passed | `HUMAN_VALIDATION_DEFERRED` | Five genuine uncoached first-time-user sessions have not occurred, and synthetic sessions count as zero |
| 7 | Complete | Local restore, rollback, deployment, observability, fault, security, and abuse qualifications passed | `IMPLEMENTED_PENDING_GATE` | Sequential acceptance waits for Milestone 6 human validation; no implementation work is blocked |
| 8 | Complete | Frozen correctness, independent golden, optimizer-oracle, performance, crash-recovery, chart, and result checks passed | `IMPLEMENTED_PENDING_GATE` | Sequential acceptance and the frozen study result wait for Milestone 6 human validation |
| 9 | Complete | Public documentation, example report, video, license and secret audits, native clean checkout, and pinned Linux qualification passed | `IMPLEMENTED_PENDING_GATE` | Sequential acceptance waits for Milestones 6 through 8; publication still requires explicit authorization |
| 10 | Not started by design | Not applicable to the V1 gate | `NOT_APPLICABLE_TO_V1` | This section defines optional post-release extensions, and no extension has been selected or authorized |

No V1 implementation milestone is blocked by unavailable hardware, credentials, data, or infrastructure.
The pinned Linux qualification supplies the required second-environment evidence.
The only acceptance prerequisite requiring external participation is the genuine Milestone 6 study.

To resume, conduct and record exactly five genuine sessions by following `docs/user-study-handoff.md`, then run:

```sh
make qualification-m6-study
make qualification-m7-restore
make qualification-m7-deployment
make finalize-m8-evaluation
make qualification-m8
make check
make dependency-audit
make clean-checkout-check
make demo-video-check
```

Commit and push the genuine study plus regenerated Milestone 8 evidence, confirm that `HEAD` equals `origin/main`, and then regenerate the final second-environment evidence with:

```sh
make qualification-m9-clean-container
make m9-clean-container-check
```

After genuine results replace the deferred state, regenerate the Milestone 8 summary, CSV, SVG, evidence indexes, README status wording, limitations wording, and any study-dependent demonstration wording.
Do not regenerate the static demo artifacts, protocol, or video unless the admitted inputs, interface, or frozen protocol changed.
Do not mark Milestones 6 through 9 `ACCEPTED` until every command above passes in sequence.
Do not publish, deploy, release, or begin a Milestone 10 extension without separate explicit authorization.

RateReplay is an auditable household electricity bill-replay and flexible-load scheduling product.
A residential customer imports interval usage, reconstructs supported charges from versioned official tariffs, compares eligible plans, and tests how a feasible EV or appliance schedule would change cost.
The product must explain every supported calculation, preserve every unsupported item as an explicit residual, and never present an estimate as an official utility bill.

## 1. Product thesis

Existing utility tools can compare rate plans using a customer's historical usage.
RateReplay answers the harder counterfactual question: what would the customer pay if only loads with an explicitly supplied reference schedule were moved under realistic constraints?

The defensible contribution is the combination of:

- Standards-based Green Button ingestion.
- A typed, versioned, and testable tariff language.
- Deterministic bill reconstruction with source provenance.
- Honest reconciliation against a user-entered bill total.
- Constraint-aware EV and appliance scheduling.
- An independent verifier for every returned schedule.
- A complete upload-to-report consumer workflow.

The project is not a thin calculator or an API dashboard.
Its technical center is an executable tariff compiler, exact time and money semantics, a durable calculation system, and a constrained optimizer.

## 2. Target user and primary journey

The first supported user is a bundled-service PG&E residential electricity customer with one primary meter, no solar, no CCA or Direct Access service, no CARE, FERA, or Medical Baseline treatment, no active bill-protection credit, Income Tier 3 base-service-charge treatment, and a qualifying electric vehicle.
The locked public-demo service window is the half-open local-date range `[2026-07-01, 2026-08-01)` in `America/Los_Angeles`.
User calculations outside an admitted source-complete service window must be rejected rather than silently repriced with another rate vintage.

The private-account journey is:

1. Create a private account.
2. Upload Green Button ESPI XML or a supported PG&E CSV export.
3. Review coverage, interval length, units, gaps, duplicates, timezone behavior, and import direction.
4. Select the current tariff, billing periods, baseline territory, heating source, and applicable eligibility facts.
5. Reconstruct each supported bill line item and inspect unsupported charges and the unexplained residual.
6. Compare the same historical usage under supported alternative tariffs.
7. Add an EV reference schedule and optional appliance reference schedules, then declare whether each load already exists in the imported profile or is a hypothetical addition replayed over the same historical service window.
8. Compare the reference schedule, a simple off-peak heuristic, and an optimal or explicitly labeled best-found feasible schedule.
9. Inspect every shifted interval, constraint, cost line item, tariff source, effective date, and exclusion.
10. Export a redacted report without the raw interval history or utility identifiers.

The built-in public demo mirrors the complete journey with one immutable, precomputed simulated import, account-fact set, replay, comparison, flexible-load scenario, verification record, and redacted report.
Public demo controls may navigate the frozen evidence and select only among explicitly precomputed variants, but they may not upload, create jobs, persist visitor state, alter constraints, or invoke authenticated API mutations.
The demo manifest and every referenced artifact are content-addressed static files generated and validated during the release build.
The public web path sends no visitor-specific demo data to the API and uses no shared demo account or anonymous authorization bypass.
No utility account, credential, paid data source, or physical device may be required for the public demonstration.

## 3. Job-description evidence

The project selection used the active SimplifyJobs new-graduate corpus captured on 2026-08-13 as the primary design input.
The software-engineering section contained 227 active roles, and 174 official descriptions were usable in the retained crawl.
A purposive 30-role sample covered consumer product, fintech, cloud, storage, infrastructure, aerospace, and data-intensive systems.
The sample is not random, and the following counts are project-design signals rather than labor-market prevalence estimates.

| Repeated responsibility or proof signal | Roles containing it |
| --- | ---: |
| Production deployment, operation, monitoring, or debugging | 29 of 30 |
| Backend services, APIs, or persistent data | 27 of 30 |
| Cross-team or product collaboration | 27 of 30 |
| Visible user, customer, or product outcome | 26 of 30 |
| Scale, performance, availability, or reliability | 23 of 30 |
| Tests, reviews, documentation, or explicit quality practices | 22 of 30 |
| Frontend or full-stack responsibility | 22 of 30 |
| Metrics, feedback, experiments, or iteration | 21 of 30 |
| Algorithms, data structures, systems design, or architecture | 21 of 30 |
| Security, privacy, correctness, or data-integrity concern | 19 of 30 |
| Cloud, containers, or delivery infrastructure | 18 of 30 |
| Explicit end-to-end ownership | 13 of 30 |

The larger 174-description scan found Python in 91 descriptions, TypeScript or JavaScript in 89, APIs or microservices in 82, Java in 70, C++ in 66, SQL or databases in 66, performance language in 63, cloud in 48, Go in 46, testing or CI/CD in 42, containers or Kubernetes in 38, and distributed systems in 21.
These are keyword-presence counts and must not be treated as independent requirements.

RateReplay demonstrates the repeated hiring signals through one coherent product rather than through disconnected technology demonstrations.
The tariff engine demonstrates domain modeling and correctness.
The optimizer demonstrates algorithms and verification.
The import, job, API, database, security, and UI paths demonstrate production ownership.
The benchmark, recovery, and user-comprehension studies create evidence that can be discussed in an interview.

## 4. Existing products and novelty boundary

PG&E already offers personalized rate comparisons.
NuWatt and similar products already model tariffs and flexible assets.
Emporia and other energy products already schedule connected devices.
NREL REopt already demonstrates sophisticated energy-system optimization.

RateReplay must not claim to invent rate comparison, residential scheduling, or energy optimization.
Its narrower contribution is an open and auditable implementation that replays versioned tariff rules, exposes reconciliation residuals, and changes only loads declared movable by the user.

The README may truthfully claim that the project provides:

- Reproducible calculations tied to source documents and hashes.
- A visible coverage matrix for supported and unsupported charge rules.
- Versioned independently checked verification records for generated schedules.
- A no-hardware counterfactual workflow.
- Public golden fixtures and failure cases.

The README may not claim:

- That a result is an official bill.
- That every PG&E customer is supported.
- That an optimized schedule is behaviorally realistic beyond declared constraints.
- That listed savings will occur in practice.
- That OpenEI is an authoritative tariff source.
- That the product is the first electricity rate optimizer.

## 5. V1 scope

### 5.1 Supported

- One utility: PG&E.
- One residential electric service agreement and one meter.
- Import-only usage without solar generation.
- Green Button ESPI Atom XML.
- One versioned PG&E CSV format after fixture validation.
- Fifteen-minute or hourly energy intervals that exactly cover an admitted billing period without an undocumented split assumption.
- The July 2026 E-1 tariff as the first mandatory vertical slice.
- E-TOU-C, E-TOU-D, E-ELEC, and EV2-A as candidate tariff families that become supported only after the per-tariff admission gate passes for the same July 2026 service window.
- A portfolio release matrix containing at least three admitted tariffs: E-1, one general residential time-of-use tariff, and one EV-eligible tariff.
- Account inputs required by supported baseline and eligibility rules.
- Historical bill replay.
- Cross-tariff comparison with unchanged usage.
- One EV load and multiple schedulable appliance loads with explicit valid reference schedules.
- Interruptible EV charging.
- Contiguous or interruptible appliance execution.
- Redacted report export.
- Built-in NREL-derived simulated household profiles.

### 5.2 Explicitly unsupported in V1

- Solar, export compensation, net energy metering, and batteries.
- Community Choice Aggregation generation charges.
- CARE, FERA, and Medical Baseline discounts.
- Income Tier 1 and Income Tier 2 base-service-charge classifications.
- Active E-TOU-C bill protection and any other cumulative or retrospective bill-protection credit.
- Demand response and event-based credits.
- Real-time wholesale pricing.
- Multiple meters or service points.
- EV-B second-meter accounts.
- Gas and water billing.
- Deposits, taxes, arrears, payment plans, and non-energy account adjustments unless explicitly modeled.
- Utility credential storage.
- Green Button Connect My Data OAuth.
- Automatic device control.
- Home Assistant integration.
- Native mobile applications.
- Multiple utilities.
- Applying a current rate vintage to historical usage dates through an undocumented calendar projection.

Unsupported scope must remain visible in the UI and report.
No unsupported charge may be silently modeled as zero.

## 6. Acceptance definition

The project is portfolio-ready only when all of the following are true:

- A clean checkout can run the complete built-in demo through the documented workflow.
- At least one independently sourced conforming Green Button XML fixture, one malicious or semantically invalid ESPI corpus, and one sanitized provider-produced supported PG&E CSV fixture behave as specified.
- Spring-forward and fall-back parsing and local-time classification are represented by passing synthetic fixtures without claiming PG&E bill coverage outside admitted windows.
- Every supported tariff rule has an authoritative source reference and at least one golden calculation.
- Every encoded tariff version passes coverage and ambiguity validation.
- Every admitted tariff has an executable dated eligibility predicate and a complete component-version vector for the locked service window.
- A comparison is ranked only when every difference-making component is supported across all candidates and eligibility is known.
- Bill replay preserves unsupported charges and the unexplained residual.
- Small scheduling problems match exhaustive enumeration.
- The reference evaluator and solver lowering agree for every optimizable tariff rule composition used by the demo.
- Every returned schedule passes an independently implemented verifier.
- A solver or heuristic candidate replaces the valid reference schedule only when it is strictly better under that candidate path's complete declared objective tuple.
- A killed worker resumes or retries without producing duplicate successful results.
- Unauthorized cross-account data access is rejected.
- Equal semantic calculation hashes in different accounts remain separately owned and cannot interfere.
- Every difference-making calculation-contract change creates a new semantic calculation identity without overwriting an older result.
- Different current-bill reconciliation inputs or policies create different immutable replay results and cannot reuse an older residual.
- Raw uploads expire and can be deleted through the real user path.
- Account, import, and profile deletion races cannot publish or recreate data after their lifecycle generation changes.
- A durable external `PREPARED` deletion record exists before the database fencing transaction, and unresolved preparation forces fail-closed restore quarantine rather than acting as a suppressive deletion decision.
- Committing `DELETION_PENDING_LEDGER` fences ordinary work before `REQUESTED` becomes suppressive, and an acknowledged `REQUESTED` or `COMPLETED` record can never coexist with an `ACTIVE` target.
- The generation-authorized deletion job can run while ordinary work is fenced, and deletion completion waits for all older-generation writers and uploads to quiesce.
- A deletion sweep cannot remove its own lifecycle row, control operation, attempt, phase checkpoint, receipt verifier, or suppression state before verified completion.
- Restoring a backup that predates a deletion reapplies verified `REQUESTED` and `COMPLETED` records and resolves every `PREPARED` record before any restored service becomes reachable.
- Redacted exports pass the deny-by-default field allowlist and contain no exact load schedule, daily series, or raw interval history.
- Every frozen performance acceptance charter passes or preserves its failed result and justified successor without rewriting history.
- Deployment claims do not exceed the verified `LOCAL_REPRODUCIBLE` or `HOSTED_VALIDATED` evidence level.
- Measured result tables identify hardware, data manifest, tariff lock, code commit, and calculation version.
- A new user can finish the built-in demo without reading internal implementation documentation.
- The public demo is served only from the frozen static artifact allowlist, makes no mutation or job request, and exposes no shared mutable tenant state.
- At least four of five uncoached first-time participants complete the demo and correctly explain the residual, exclusions, reference schedule, historical-addition meaning, and optimization status under the frozen study protocol.

## 7. Technology decisions

### 7.1 Application shape

Use a modular monolith with one web client, one API process, one worker process, one PostgreSQL database, and one object-storage abstraction.
Do not split the system into networked microservices in V1.
The module boundaries must remain explicit so later extraction is possible without imposing distributed failure modes now.

### 7.2 Proposed stack

- Python 3.12 or the newest project-compatible stable Python selected at Milestone 0.
- FastAPI for the HTTP API and generated OpenAPI contract.
- Pydantic for external request validation and immutable domain input models.
- SQLAlchemy 2 and Alembic for persistence and migrations.
- PostgreSQL 16 or a later pinned compatible major version.
- PostgreSQL row locking with `FOR UPDATE SKIP LOCKED` for the leased worker queue.
- React with TypeScript for the web client.
- Vite for the initial client build unless deployment requirements justify a server-rendered framework.
- OR-Tools CP-SAT for integer scheduling after the feasibility spike.
- PyArrow or Polars for interval-table operations after a measured comparison on a one-year profile.
- A filesystem object-store adapter for local development.
- An S3-compatible adapter for deployed environments.
- OpenTelemetry-compatible traces and Prometheus-style metrics.
- Docker Compose for the reproducible local product path.

The final dependency versions must be pinned in lockfiles.
Milestone 0 must verify licenses and platform support before a package becomes an implementation dependency.

### 7.3 Numerical representations

Do not use binary floating point for bill totals or energy conservation.
Store energy as integer watt-hours in V1.
An input value is calculation-eligible only when its source value, unit, and multiplier convert exactly to an integral number of watt-hours.
The importer must reject a nonintegral result with the stable fatal code `NON_INTEGRAL_WATT_HOUR` and must never round source energy.
Store tariff rates as exact decimal values or integer microdollars per kilowatt-hour.
Perform charge arithmetic with explicit decimal quantization and utility-rule-specific rounding boundaries.
Store timestamps as UTC instants and retain the source timezone metadata needed to classify local tariff periods.

### 7.4 Authentication and release deployment

V1 private accounts use a local username and password contract rather than an external identity provider.
Usernames are 3 through 64 lowercase ASCII letters, digits, or underscores after one documented canonicalization pass, are unique by canonical value, and are never written to logs or traces.
Passwords are accepted as 12 through 128 UTF-8 characters, are hashed with a Milestone 0 benchmarked Argon2id configuration, and are never truncated or normalized silently.
V1 has no email address, email verification, or credential-recovery workflow.
Loss of the password means the account cannot be recovered, and this limitation must be visible before private data is uploaded.
The built-in demo requires no private account.

The reference hosted topology is one Linux host running versioned web, API, worker, and reverse-proxy containers, one PostgreSQL service on an encrypted volume, and one S3-compatible encrypted object store for raw and generated artifacts.
The reverse proxy terminates HTTPS, redirects HTTP, sets security headers, and exposes only the web and API entry points.
Deployment secrets are supplied outside images and the repository through root-readable runtime secret files or an equivalent managed secret injection mechanism.
Database backups are encrypted and copied to a separately credentialed object-store location with the declared 30-day maximum retention.
Application images are content-addressed, migrations are versioned and backed up before execution, and application rollback is permitted only while the deployed schema remains backward compatible.
Milestone 0 must freeze the host operating system, CPU architecture, container runtime, reverse proxy, PostgreSQL version, object-store implementation, secret mechanism, TLS mechanism, migration procedure, backup owner, monthly cost ceiling, and teardown procedure in a deployment ADR.

`LOCAL_REPRODUCIBLE` is the mandatory release evidence level and exercises the topology through Docker Compose with development certificates and isolated test data.
`HOSTED_VALIDATED` may be claimed only after explicit deployment authorization and a staging or production run that verifies real TLS, encryption configuration, backup and restore, retention, deletion, and rollback behavior.
If hosted deployment is not authorized, the repository must withhold hosted-operation claims while retaining a reproducible deployment specification.

## 8. Repository layout

```text
rate-replay/
  BUILD_PLAN.md
  README.md
  LICENSE
  SECURITY.md
  CONTRIBUTING.md
  pyproject.toml
  uv.lock
  package.json
  pnpm-lock.yaml
  Makefile
  compose.yaml
  .env.example
  apps/
    api/
      ratereplay_api/
      tests/
    worker/
      ratereplay_worker/
      tests/
    web/
      src/
      tests/
  packages/
    domain/
      ratereplay_domain/
      tests/
    ingestion/
      ratereplay_ingestion/
      tests/
    tariffs/
      ratereplay_tariffs/
      tests/
    optimizer/
      ratereplay_optimizer/
      tests/
    reports/
      ratereplay_reports/
      tests/
  tariffs/
    definitions/
    golden/
    sources.lock.json
  data/
    demo/
    schemas/
  benchmarks/
    fixtures/
    scripts/
    expected/
  docs/
    architecture.md
    billing-semantics.md
    tariff-authoring.md
    security-and-privacy.md
    validation-methodology.md
    limitations.md
    adr/
  migrations/
  scripts/
  tests/
    integration/
    end_to_end/
    security/
```

Generated uploads, reports, database files, solver traces, and benchmark outputs must be ignored unless a small reviewed artifact is intentionally committed.

## 9. External data and provenance

### 9.1 Green Button

The Green Button Alliance developer model and a pinned ESPI XSD release are the schema authorities for ESPI ingestion.
The schema version, schema hash, namespace set, and permitted extension policy must be locked before parser implementation.
The first release uses file-oriented Download My Data only.

The parser must resolve the Atom resource graph rather than assuming resources are nested in a useful order.
It must resolve `self`, `up`, and `related` links among `UsagePoint`, `MeterReading`, `ReadingType`, `IntervalBlock`, and `LocalTimeParameters` entries and reject missing, dangling, duplicate, or ambiguous relationships.

An ESPI reading becomes calculation-eligible only when all of the following are true:

- The `UsagePoint` service category and `ReadingType` commodity represent electricity.
- The reading kind and accumulation behavior represent interval energy consumption under the pinned ESPI code tables.
- The flow direction maps unambiguously to customer import under the pinned code tables.
- The unit is watt-hours or kilowatt-hours and the multiplier converts every value exactly to the canonical energy representation.
- The declared reading-type interval length matches every accepted interval duration.
- The time attribute, data qualifier, default quality, and attached reading-quality codes are in an explicit allowlist.
- The related local-time parameters are compatible with `America/Los_Angeles` for the represented UTC instants.
- Exactly one supported usage point and one supported meter-reading stream cover the selected billing period.

Known estimated or substituted quality codes may be admitted only under a named warning policy and may not appear in correctness goldens.
Unknown quality codes, non-electric commodities, demand or accumulated readings, net or reverse flows, and relationship ambiguity are fatal.

### 9.2 Tariffs

PG&E filed tariff documents are the authority for supported rate rules.
OpenEI may assist discovery but may not override a filed tariff.

`tariffs/sources.lock.json` must record:

- Stable source identifier.
- Source URL.
- Retrieval timestamp.
- Effective date range.
- SHA-256 hash.
- Media type.
- CPUC sheet number, advice-letter identifier, or another stable regulator identifier when available.
- Review method, reviewer identifier, review timestamp, and whether the review was independent or self-review.
- Extraction notes.
- Redistribution decision.
- Content-addressed archive location or documented stable retrieval path.
- Every normalized rule identifier extracted from the source.
- Every holiday or exceptional-day calendar source identifier, jurisdiction, calendar version, effective range, retrieval timestamp, and content hash used by a time-of-use rule.

Do not commit complete source PDFs unless their redistribution terms are verified.
If redistribution is not permitted, retain a private content-addressed source snapshot for development and commit normalized factual rules, stable regulator identifiers, citations, hashes, extraction notes, and minimal permitted golden evidence.
A mutable utility URL and hash without a retrievable snapshot or stable regulator archive do not satisfy the reproducibility gate.

### 9.3 Demo profiles

Use a small, reproducibly selected subset of NREL End-Use Load Profiles or transformations derived from those profiles under their applicable terms.
Every demo profile must be labeled simulated.
The repository must record source version, building archetype, weather location, interval resolution, transformations, and hash.
The static demo artifacts must be generated by invoking the released parser, billing, comparison, optimizer, verifier, and report code against the frozen simulated inputs rather than by hand-authoring result JSON.
The generation command must fail unless every referenced calculation manifest, verification record, redaction schema, and content hash passes the normal production validators.

### 9.4 Calculation time modes

V1 supports `HISTORICAL_REPLAY` only.
The service timestamps determine the applicable tariff component versions, season, day type, and time-of-use period.
The tariff source lock must provide a complete component-version vector for every service instant in the admitted billing period.

V1 tariff comparison replays the same service timestamps against alternative tariffs that were effective and eligible for that same period.
It does not apply a current rate vintage to older timestamps and does not project a historical load shape onto a different calendar.
Schedule optimization is confined to one admitted billing period, and every load occurrence must start and finish within that billing period.

The built-in public demo uses the local service window `[2026-07-01, 2026-08-01)`.
Additional periods require separate source-complete admission records and may not reuse the July rate vector.

## 10. Canonical interval model

The canonical input record is:

```text
IntervalReading
  reading_id: UUID
  profile_version_id: UUID
  start_utc: timestamp with timezone
  duration_seconds: positive integer
  energy_wh: integer
  flow_direction: IMPORT | EXPORT | REVERSE | UNKNOWN
  source_unit: string
  source_multiplier: integer
  source_reading_type: string
  source_service_category: string
  source_commodity: string
  source_accumulation_behavior: string
  source_data_qualifier: string
  source_time_attribute: string
  source_local_time_parameters_hash: SHA256 or null
  source_timezone_offset_seconds: integer or null
  source_dst_offset_seconds: integer or null
  quality_flags: set[QualityFlag]
  source_reference: random internal identifier with no source identifier material
```

`profile_version_hash` is the SHA-256 hash of the versioned `CanonicalProfileContentV1` representation rather than of persisted rows or API objects.
The hash input begins with the domain separator `RateReplay.ProfileContent.v1` and uses one frozen deterministic binary encoding with explicit field tags, byte lengths, signed integer widths, UTF-8 normalization rules, and no floating-point values.
It contains the parser-contract version, ESPI schema hash or CSV adapter fingerprint, finding-policy version, confirmation-policy version, admitted billing-period start and end UTC instants, tariff timezone identifier, canonical interval resolution, and the calculation-eligible readings in ascending `(start_utc, duration_seconds)` order.
Each reading contributes only its UTC start in integer nanoseconds, duration, integer watt-hours, flow direction, normalized unit and multiplier meaning, reading type, service category, commodity, accumulation behavior, data qualifier, time attribute, local-time-parameters hash, declared source offset semantics, and lexically sorted quality flags.
The representation also contains the immutable quality findings as canonically ordered tuples of stable code, severity, normalized field path, and calculation-relevant safe value, plus the canonically ordered warning identities acknowledged at confirmation.
It excludes owner and database identifiers, `reading_id`, `profile_version_id`, import identifiers, object keys, filenames, row insertion order, creation and confirmation timestamps, raw source identifiers, and the random `source_reference`.
The raw content hash remains part of import identity and provenance but is deliberately not part of normalized profile content, so semantically equivalent Atom entry order or provider wrappers can normalize to the same profile hash.
Hash creation fails on duplicate canonical interval keys, an unrecognized field, a noncanonical string, or any value not representable by the frozen encoding.
The complete canonical byte vector is reproducible through a diagnostic command, while public logs and reports expose only its hash.
Golden fixtures prove that random persistence identities and independent source-entry order do not change `profile_version_hash`, while every calculation-relevant interval, quality, policy, timezone, period, or acknowledgment change does.

The billable V1 path accepts only nonnegative, unambiguous import energy readings that satisfy the complete ESPI reading contract.
Export, reverse, or unknown flow remains visible in the quality report and blocks calculation.

Intervals use half-open semantics `[start_utc, end_utc)`.
No operation may infer that a naive local timestamp is unique during a fall-back transition.
Local tariff classification must use the IANA `America/Los_Angeles` rules plus the represented UTC instant.

The importer must emit stable finding codes for:

- Empty files.
- Unsupported content type.
- Oversized files.
- XML entity expansion.
- External entity references.
- Excessive nesting.
- Unknown units.
- Invalid multipliers.
- Nonintegral watt-hour conversions.
- Negative durations.
- Non-monotonic intervals.
- Duplicate intervals.
- Overlaps.
- Gaps.
- Multiple usage points.
- Mixed reading types.
- Mixed interval durations.
- Export or reverse flow.
- Timezone metadata conflicts.
- Source identifiers that require redaction.

The import pipeline must be two-phase.
Parsing creates an immutable draft and quality report.
User confirmation creates a calculation-eligible profile version.

The import finding policy is fail-closed.
Empty or oversized input, unsupported type, unsafe XML, schema failure, relation ambiguity, unsupported unit or reading type, invalid multiplier, nonintegral watt-hour conversion, negative energy or duration, duplicate semantic readings, overlap, a gap inside the selected billing period, multiple usage points, mixed reading types or durations, export or unknown flow, ambiguous local time, and timezone conflict are fatal.
Non-monotonic source entry order is a warning because normalization sorts by UTC after identity checks.
Data outside the selected billing period is an informational finding and is excluded only after its hash and range are recorded.
Known estimated or substituted readings are warnings that require explicit confirmation and remain visible in every calculation and report.
No fatal draft can be confirmed.

The selected billing period must have complete, disjoint, half-open interval coverage at one resolution.
An interval that crosses a time-of-use, season, billing-period, tariff-effective-date, baseline-day, or other semantic boundary must be rejected unless the source itself provides exact subinterval readings.

The supported PG&E CSV adapter must lock the exact headers, delimiter, encoding, unit, timezone representation, DST disambiguation, locale, and row semantics from a sanitized provider-produced fixture.
A project-authored CSV alone cannot admit the adapter.
Unknown header fingerprints or ambiguous fall-back timestamps are fatal.

## 11. Tariff language and compiler

### 11.1 Design goal

Tariffs must be data, not arbitrary Python code.
The language must be expressive enough for the supported rules and restrictive enough to validate coverage, units, dates, and ambiguities before evaluation.

### 11.2 Tariff version contract

```text
TariffVersion
  tariff_version_id: stable string
  utility: PG&E
  plan_code: string
  admitted_service_windows: list[half-open local-date range]
  component_versions: ordered list[TariffComponentVersion]
  timezone: America/Los_Angeles
  currency: USD
  eligibility_predicate: versioned executable predicate
  eligibility_questions: ordered list[EligibilityQuestion]
  comparison_component_keys: set[ChargeComponentKey]
  optimization_capability: SUPPORTED | UNSUPPORTED_WITH_REASON
  charge_rules: ordered list[ChargeRule]
  unsupported_rules: list[UnsupportedRule]
  source_ids: list[SourceId]
  source_hashes: list[SHA256]
  schema_version: string
  compiler_version: string
```

Each `TariffComponentVersion` records its own effective range, source sheet, source hash, extracted rule identifiers, and precedence.
Compilation must prove that exactly one complete component vector applies to every service instant in each admitted window.

Eligibility evaluation returns `ELIGIBLE`, `INELIGIBLE`, or `UNKNOWN` with predicate version, answers, source rules, and effective dates.
Only `ELIGIBLE` tariffs may enter a ranked comparison.
Missing facts never default to eligibility.

The V1 account-fact contract is:

```text
AccountFacts
  schema_version: string
  service_window: half-open local-date range
  service_provider: PG&E
  service_mode: BUNDLED
  meter_count: 1
  primary_meter_only: true
  income_tier: TIER_3
  care_enrolled: false
  fera_enrolled: false
  medical_baseline: false
  cca_service: false
  direct_access_service: false
  active_bill_protection: false
  solar_or_export: false
  baseline_territory: P | Q | R | S | T | V | W | X | Y | Z
  baseline_quantity_code: BASIC | ALL_ELECTRIC
  qualifying_technologies: set[EV | HEAT_PUMP | ELECTRIC_WATER_HEAT]
  user_attested_at: timestamp
```

The API validates this schema against the target account class before replay.
Each tariff predicate may require additional dated facts, but new facts require a schema version and cannot be inferred from interval usage.

### 11.3 Supported rule nodes

- `FixedDailyCharge`.
- `FixedMonthlyCharge`.
- `TimeOfUseEnergyCharge`.
- `TieredEnergyCharge`.
- `BaselineAllowance`.
- `BaselineCreditOrSurcharge`.
- `MinimumBillAdjustment`.
- `ExplicitUnsupportedCharge`.

Each charge rule must declare units, applicability, effective dates, rounding behavior, and the line-item key it emits.

### 11.4 Comparable-cost contract

Every charge rule declares a stable `ChargeComponentKey` and whether that component can differ between candidate tariffs for the target account and service window.
The comparison compiler computes the intersection and difference of supported component keys before any cost is shown.
An unsupported or unclassified component defaults to difference-making.
Treating a component as common and constant requires a source-linked proof artifact showing that its applicability and amount are identical across every candidate for the locked account and window.

A comparison is rankable only when all of the following are true:

- Every candidate is `ELIGIBLE` for the same account facts and service window.
- Every tariff-dependent or usage-dependent difference-making component is modeled for every candidate.
- No active bill protection, CCA generation charge, retrospective credit, or unsupported rider can change the ordering.
- Every candidate uses a complete source-locked component vector.

If any condition fails, the product may display separate supported subtotals but must not rank plans, calculate savings, or recommend a winner.
User-entered unsupported charges and the current-bill residual belong only to reconciliation of the current bill and may not be copied to alternative tariffs.
Every comparison result records its component coverage set and a machine-readable non-comparability reason when ranking is blocked.

### 11.5 Time schedule semantics

A time-of-use calendar must declare:

- Named seasons with complete date coverage.
- Weekday, weekend, and holiday day types.
- Local half-open time windows.
- Priority rules for holidays and exceptional dates.
- The exact versioned holiday and exceptional-day calendar identifiers from the tariff source lock.
- A price identifier for every supported local instant.

Compilation must reject gaps, overlaps, unknown holidays, impossible local times, ambiguous priorities, incomplete component vectors, and uncovered effective dates.
The evaluator classifies each interval by the local instant represented by its UTC timestamp.
If a source interval crosses any semantic boundary and the source does not provide finer readings, the calculation must reject the period instead of assuming uniform energy within the interval.

### 11.6 Tier semantics

Tier state resets only at the declared billing-period boundary.
Tier bounds use exact energy units and explicit inclusive or exclusive semantics.
Baseline allowance inputs and heating-source rules must be recorded in the calculation manifest.

### 11.7 Canonical charge IR and compiler outputs

The tariff AST lowers to a canonical integer `CompiledChargeIR` before either billing evaluation or solver construction.
The IR defines exact units, signed 64-bit bounds, addition, multiplication by rational rates, tier allocation, applicability predicates, min or max adjustments, and named rounding operators.
Compilation rejects any coefficient scale, intermediate bound, or denominator that cannot be represented exactly and safely.

The reference billing backend evaluates this IR with exact integers and rational or decimal arithmetic.
The optimizer backend may lower only the IR operators listed in its versioned capability table.
It must not reimplement tariff meaning from the source definitions.
An admitted tariff may be replayable but not optimizable when any IR operator lacks a proved exact lowering.
It is also not optimizable when an unsupported component could change as energy moves between feasible schedules.

The compiler produces:

- A validated immutable tariff AST.
- A normalized coverage calendar.
- A deterministic evaluator plan.
- A solver-lowering capability report with exact unsupported reasons.
- A source and rule coverage report.
- An eligibility predicate and comparison-component report.
- Golden-case identifiers.
- A content hash used in result manifests.

## 12. Billing engine

The engine receives a confirmed profile version, one admitted billing period, versioned account parameters, and exact tariff component vectors.
It produces immutable bill-period results.

The evaluation order is explicit:

1. Validate profile and account eligibility inputs.
2. Resolve the complete tariff component vector active for every interval.
3. Classify intervals into charge periods.
4. Aggregate energy while preserving billing-period tier state.
5. Calculate energy line items.
6. Calculate supported baseline adjustments.
7. Calculate fixed and minimum-bill adjustments.
8. Apply documented rounding at the declared boundaries.
9. Attach explicit unsupported charge placeholders.
10. Reconcile against an optional user-entered total.

Historical replay and alternative-plan comparison are separate result types.
Only the current-tariff replay may consume a user-entered bill total, user-entered unsupported line items, or an unexplained residual.
Alternative-plan results contain supported calculated charges, comparison coverage, eligibility status, and exclusions only.

The reconciliation identity is:

```text
supported calculated charges
+ user-entered unsupported charges
+ unexplained residual
= user-entered bill total
```

The residual must be signed, visible, and exportable.
The system may define a configurable review tolerance, but it may not force a match by moving the residual into another category.
The tolerance, warning thresholds, and result classification form a versioned reconciliation policy whose canonical hash is recorded in every current-replay result.

Every line item records:

- Rule identifier.
- Tariff version.
- Source identifier.
- Quantity.
- Rate.
- Pre-round value.
- Rounded value.
- Contributing interval range.
- Explanation key.
- Charge component key.
- Rounding operator and boundary.

Every displayed savings value must be derived from a rankable comparison under the comparable-cost contract.
If comparison is blocked, the UI and API return supported subtotals without a difference, winner, or recommendation.

## 13. Flexible-load model

```text
ScenarioLoadMode
  SHIFT_EXISTING | HISTORICAL_ADDITION

FlexibleLoad
  load_id: UUID
  physical_asset_key: stable scenario-local identifier
  kind: EV | DISHWASHER | WASHER | DRYER | POOL_PUMP | CUSTOM
  mode: ScenarioLoadMode
  execution_spec: InterruptibleModulatingSpec | ContiguousFixedShapeSpec
  occurrences: ordered list[LoadOccurrence]

InterruptibleModulatingSpec
  execution_type: INTERRUPTIBLE_MODULATING
  maximum_power_w: positive integer
  minimum_power_when_active_w: nonnegative integer

ContiguousFixedShapeSpec
  execution_type: CONTIGUOUS_FIXED_SHAPE
  fixed_slot_shape_wh: nonempty ordered list[nonnegative integer]

LoadOccurrence
  occurrence_id: UUID
  required_energy_wh: positive integer
  earliest_start_utc: timestamp
  deadline_utc: timestamp
  reference_schedule: ordered list[ReferenceSlot]

ReferenceSlot
  slot_start_utc: timestamp
  duration_seconds: positive integer
  energy_wh: nonnegative integer

ScenarioElectricalConstraints
  site_import_cap_w: positive integer or null
  flexible_load_aggregate_cap_w: positive integer or null
  energy_basis: METER_SIDE
```

Every occurrence requires a complete feasible reference schedule containing exactly one `ReferenceSlot` for every canonical profile slot in the admitted billing period.
The slot starts and durations must exactly equal the canonical profile slot vector in the same order, and energy must be zero outside the occurrence window.
For `SHIFT_EXISTING`, the reference schedule is the original flexible component already present in the imported meter profile.
For `HISTORICAL_ADDITION`, the reference schedule is the user's hypothetical unoptimized schedule over the admitted historical service window and is not present in the imported profile.
It asks what the supported historical bill would have been if that load had existed on those same service timestamps.
It is not a forecast, a current-rate projection, or a claim about future behavior.

The scenario builder computes the fixed background per slot as follows:

```text
SHIFT_EXISTING: background[t] = measured[t] - sum(existing reference schedules[t])
HISTORICAL_ADDITION: background[t] = measured[t]
```

A mixed scenario subtracts only `SHIFT_EXISTING` reference schedules and adds every candidate schedule exactly once.
Any negative background value, slot mismatch, or energy mismatch is fatal.
The unchanged reference profile is `background + all reference schedules`.
The heuristic and solver-candidate profiles replace only the declared reference schedules.

The sum of every reference schedule must equal its occurrence's `required_energy_wh` and must independently satisfy its time, execution-model, and scenario-level site constraints.
Scenario admission must also prove that the combined unchanged reference profile satisfies every occurrence constraint and aggregate site or flexible-load cap.
An invalid reference returns a stable pre-solve validation code with a constraint witness and does not create a solver job.
Recurring behavior is represented by explicit bounded occurrences rather than an implicit recurrence grammar.
Every `FlexibleLoad` represents one physical asset, and its occurrence windows must be pairwise disjoint under half-open interval semantics in V1.
The scenario validator rejects overlapping windows for one load with `OVERLAPPING_LOAD_OCCURRENCES` before constructing a solver job, even when the supplied reference schedules happen not to overlap in their positive-energy slots.
Separate physical devices require separate load identifiers, and the API rejects reuse of one `physical_asset_key` by multiple loads in the same scenario.
Meter-side energy includes charging losses, so V1 makes no battery-side efficiency claim.
Every occurrence uses half-open window semantics `[earliest_start_utc, deadline_utc)`.
Both endpoints must equal canonical profile-slot boundaries, and the occurrence must remain within the admitted billing period.
Positive interruptible energy may appear only in canonical slots fully contained in that window.
A fixed-shape start is a canonical slot start, and its final slot must end no later than `deadline_utc`.
The schema rejects a nonaligned endpoint with `NON_ALIGNED_OCCURRENCE_BOUNDARY` and never rounds or clips it.
UTC instants disambiguate repeated local times during a fall-back transition.
An `INTERRUPTIBLE_MODULATING` occurrence may place zero energy in any allowed slot and may place positive energy only when its exact average power is between `minimum_power_when_active_w` and `maximum_power_w`.
It has no contiguity requirement in V1.
For interruptible modulating loads, integer slot energy maps to average meter power using an exact declared conversion.
For a slot of `duration_seconds`, bounds are enforced by exact integer inequalities such as `energy_wh * 3600 <= maximum_power_w * duration_seconds`, with the analogous active minimum bound.
Any site import cap is an average-power cap at the interval resolution and is not presented as an instantaneous electrical or breaker-safety guarantee.
A `CONTIGUOUS_FIXED_SHAPE` occurrence chooses exactly one allowed start, and its nonnegative shape is copied without modulation into consecutive canonical slots.
Its `required_energy_wh` must equal the exact sum of `fixed_slot_shape_wh`, its shape length must fit completely inside the occurrence window, and its reference schedule must contain that exact shape at one allowed start.
The schema rejects every other field combination rather than interpreting nullable flags.

The UI must make every assumption editable and show the original decomposition, reconstructed unchanged profile, and feasibility preview.
The optimizer may move only the declared flexible-load energy.
All background usage remains unchanged.

V1 must not infer flexible loads from aggregate household usage.
Every `SHIFT_EXISTING` schedule is labeled user-supplied and requires an attestation that it represents consumption already included in the imported profile.
V1 must not claim the solver-generated behavior is convenient or automatic.

## 14. Optimization formulation

Use a time-indexed integer model at the canonical calculation slot resolution.
All explicit load occurrences must be fully contained in one admitted billing period.

For each interruptible modulating load `l` and slot `t`, define integer delivered energy `x[l,t]` and a binary activation variable when `minimum_power_when_active_w` is positive.
For each fixed-shape appliance, define an allowed start variable and derive slot energy from the declared shape.
Occurrence-indexed variables for one load may exist only after the pairwise-disjoint occurrence-window invariant has been proved, so no two occurrences can consume the physical asset's power allowance in the same slot.
Constrain each occurrence by its allowed window, total energy, execution-spec invariants, exact power conversion where applicable, and scenario-level power caps.
Construct the counterfactual meter profile as fixed background plus candidate flexible schedules.
Evaluate cost through the optimizer lowering of the same `CompiledChargeIR` used by historical replay.

The primary objective is total supported bill cost.
The staged objective tuple is deterministic and ordered:

1. Lower supported cost.
2. Fewer changed occurrence-slot entries.
3. Lower sum of occurrence completion-slot indices.
4. Lower stable slot-order score.

Canonical occurrence order sorts by `deadline_utc`, `earliest_start_utc`, the raw bytes of `load_id`, and the raw bytes of `occurrence_id`.
Canonical schedule-vector order expands occurrences in canonical occurrence order and expands each occurrence by ascending `slot_start_utc`.
The changed occurrence-slot count is the number of entries in that vector whose candidate `energy_wh` differs from the corresponding reference value, even when changes from different loads cancel in the aggregate meter profile.
An occurrence completion-slot index is the one-based index of the last canonical profile slot with positive candidate energy for that occurrence.
The completion objective is the exact sum of those indices across all occurrences.
The stable slot-order score is the exact sum of each candidate `energy_wh` multiplied by its one-based position in the canonical schedule vector, which prefers earlier energy when all higher-priority objectives tie.
The compiler must prove signed 64-bit safety for every objective bound before creating the model.

The solver may omit an IR term only when the compiler proves that the term is constant across every feasible schedule in the scenario.
Any marginal-cost simplification must emit a machine-checkable proof record listing the invariant energy totals, billing-period confinement, omitted components, and algebraic reason each component is constant.
If a tariff operator lacks an exact lowering or valid invariance proof, optimization is unavailable for that tariff.
Do not maintain two divergent billing implementations.

Cost coefficients and intermediate expressions use exact integer units chosen by the compiler.
The compiler proves signed 64-bit safety for every variable and expression before solving.
Displayed cost is always recomputed by the reference evaluator after solving.

Lexicographic tie breakers use staged solves rather than one potentially overflowing weighted objective.
The solver enters a lower-priority stage only after the preceding stage returns `OPTIMAL`, and it fixes that proved optimum as an equality before applying the next objective.
If the primary stage returns `FEASIBLE`, the system verifies that incumbent, skips every lower-priority stage, and labels the result best found with an open cost bound.
If a lower-priority stage returns `FEASIBLE` or `UNKNOWN`, the system preserves the best verified incumbent available at that stage, labels the overall selection best found, and records the highest objective stage proved optimal and the first open stage.
The overall solver status is `OPTIMAL` only when all four objective stages are proved optimal.
Selection is a separate deterministic step after verification and reference-evaluator scoring.
The selector computes the complete four-stage objective tuple for both the verified solver incumbent and the valid reference schedule and selects the incumbent only when its tuple is lexicographically strictly smaller.
If the incumbent tuple is equal to or worse than the reference tuple, the selected schedule is the reference, while the incumbent, its bound, its solver status, and the comparison reason remain in diagnostic evidence.
An `OPTIMAL` solver response may therefore produce the reference as the selected schedule when the reference is itself in the final optimum set.
Reproducible portfolio runs pin the OR-Tools version, use one search worker, set a fixed random seed, use a deterministic-time budget, and record every parameter.
Wall-clock-limited multiworker runs may be offered separately but are not claimed bit-for-bit deterministic.

The versioned off-peak heuristic uses the same feasible scheduling constraints but never uses the supported bill total as its optimization objective.
For every canonical slot, the tariff compiler emits an integer `off_peak_rank` from the applicable modeled time-of-use energy rate, where lower exact rates receive lower ranks and all slots receive rank zero when the admitted tariff has no time-varying energy rate.
The applicable rate is the exact sum of all time-varying per-energy coefficients active in that slot before nonlinear billing-period adjustments, equal rates share one rank, and distinct rates receive dense ascending ranks starting at zero.
The heuristic first minimizes the exact sum of `off_peak_rank * energy_wh` over all flexible-load entries and then, only after that proxy objective is proved optimal, minimizes the stable slot-order score defined above.
The heuristic uses the same pinned single-worker deterministic solver contract and preserves any verified incumbent when its proxy bound remains open.
Its search status is `HEURISTIC_PROXY_OPTIMAL` only when both proxy stages are proved optimal, `HEURISTIC_BEST_FOUND` when either proxy stage remains open with a verified incumbent, or `HEURISTIC_NO_INCUMBENT` when no verified incumbent is available.
The heuristic selector compares the verified incumbent and reference under the ordered pair of proxy-rank objective and stable slot-order score and selects the incumbent only when that pair is strictly smaller.
Its separate selection outcome is `HEURISTIC_INCUMBENT_SELECTED`, `HEURISTIC_REFERENCE_DOMINATES`, or `HEURISTIC_REFERENCE_FALLBACK`.
An equal or worse heuristic pair returns the reference with `HEURISTIC_REFERENCE_DOMINATES` and preserves the incumbent, search status, and proxy bound as diagnostic evidence.
No incumbent returns the valid reference with `HEURISTIC_REFERENCE_FALLBACK`.
Its displayed cost is always recomputed by the reference billing evaluator, and the UI must not describe the heuristic as bill-optimal.
The heuristic contract version, rank-calendar hash, solver parameters, search status, selection outcome, and fallback reason are part of the scenario result and calculation manifest.

The optimizer returns:

- Solver status.
- Objective value.
- Best objective bound and a precisely defined optimality gap when the solver status permits it.
- Highest objective stage proved optimal and first open stage or null.
- Scheduled energy per slot.
- Selected schedule source and the complete incumbent-versus-reference objective comparison.
- Versioned verification record.
- Cost breakdown from a fresh billing-engine evaluation.
- Deterministic result hash.

Only `OPTIMAL` results may be labeled optimal.
A `FEASIBLE` result with an open bound is labeled best found under the recorded limit.
`UNKNOWN`, `MODEL_INVALID`, and unverified results are never displayed as successful schedules.
Because every admitted scenario contains a feasible reference candidate, solver `INFEASIBLE` is a `MODEL_CONTRACT_VIOLATION` rather than a normal user constraint outcome and must fail closed with internal diagnostic evidence.
The product never selects a solver candidate whose complete declared objective tuple is equal to or worse than the valid reference schedule's tuple.

An independent verifier must not import solver-internal constraint-building code or solver-lowering code.
It rechecks profile decomposition, energy conservation, occurrence windows, exact slot power, site caps, contiguity, unchanged background load, reference-schedule replacement, and recomputed bill cost.

The exhaustive oracle must enumerate feasible schedules without importing production constraint construction and must score candidates only through the public reference billing interface.
Small cases must compare feasibility, the complete set of schedules with the optimal four-stage objective tuple, every objective value, and membership of the returned schedule in that set with exhaustive enumeration.
Randomized cross-backend tests must cover every optimizable IR operator composition and each rounding boundary admitted by a public tariff.
The unchanged-load and off-peak heuristic baselines must use the same billing engine.

## 15. Persistence model

Core relational tables include:

- `users`.
- `sessions`.
- `imports`.
- `raw_objects`.
- `profile_versions`.
- `interval_readings` or partitioned interval objects.
- `import_quality_findings`.
- `tariff_versions`.
- `tariff_component_versions`.
- `tariff_source_records`.
- `tariff_admissions`.
- `billing_periods`.
- `scenarios`.
- `scenario_loads`.
- `scenario_reference_schedules`.
- `comparison_coverage`.
- `jobs`.
- `job_attempts`.
- `object_upload_registrations`.
- `scenario_results`.
- `calculation_manifests`.
- `report_exports`.
- `deletion_intents`.
- `data_scope_lifecycle`.
- `deletion_control_operations`.
- `deletion_audit`.
- `deletion_ledger_receipts`.

Immutable versions must be append-only at the application layer.
Mutable user labels and display preferences must not change a prior result manifest.

The calculation manifest records:

- Code commit.
- Environment lock hash.
- Operation-request hash.
- Semantic calculation hash.
- Calculation-contract version.
- Profile version hash.
- Tariff AST hashes.
- Source document hashes.
- Account-parameter hash.
- Billing-period identity hash.
- Reconciliation-input hash or null.
- Reconciliation-policy hash or null.
- Solver name and version.
- Solver configuration.
- Complete tariff component-version vector.
- Eligibility predicate version and result.
- Comparison component coverage.
- Scenario load mode and reference-schedule hashes.
- Heuristic contract version, rank-calendar hash, solver configuration, search status, selection outcome, and fallback reason or null.
- Account, import, and profile lifecycle generations captured at submission.
- Solver-lowering capability and invariance-proof hashes.
- Calculation schema version.
- Start and completion times.
- Warning codes.

The calculation schema version governs manifest serialization, while the calculation-contract version governs result semantics and reuse.

### 15.1 Operation idempotency and semantic calculation identity

Operation idempotency and semantic result reuse are separate contracts.

```text
OperationRequestIdentity
  owner_user_id
  route_id
  idempotency_key
  request_schema_version
  canonical_payload_hash
```

An operation identity returns the original asynchronous operation when the authenticated owner repeats the same route, key, schema, and canonical payload.
It does not decide whether a calculation produced under older semantics may be reused.

```text
SemanticCalculationIdentity
  job_kind
  request_schema_version
  calculation_contract_version
  environment_lock_hash
  tariff_compiler_version or null
  billing_evaluator_version or null
  profile_version_hash or null
  tariff_ast_hashes
  component_vector_hashes
  account_facts_hash or null
  billing_period_identity_hash or null
  reconciliation_inputs_hash or null
  reconciliation_policy_hash or null
  comparison_coverage_version or null
  scenario_and_reference_hashes
  heuristic_contract_version or null
  heuristic_rank_calendar_hash or null
  heuristic_solver_configuration_hash or null
  solver_lowering_version or null
  solver_name_and_version or null
  solver_configuration_hash or null
  verifier_version or null
  report_template_version or null
```

The canonical semantic calculation hash is the domain-separated hash of that normalized structure.
The unique successful-result key is `(owner_user_id, semantic_calculation_hash)`.
The `profile_version_hash` field is exactly the canonical profile-content hash defined in Section 10 and never a hash of a persistence record containing random identifiers.
Every field that can change a supported charge, eligibility result, comparison rank, feasible schedule, selected tie break, verification result, or exported report must change the semantic calculation identity.
For a current replay, `reconciliation_inputs_hash` is the domain-separated hash of the exact entered bill total and a canonical sequence of unsupported-line-item tuples containing the stable request item key, exact amount, and every result-visible label or value.
The request schema assigns each unsupported line item a stable key, and canonicalization sorts the tuples by that key while rejecting duplicate keys.
The `reconciliation_policy_hash` covers the exact review tolerance, warning boundaries, classification rules, and policy version.
A replay without reconciliation inputs uses null for both reconciliation fields and cannot reuse a reconciled replay result.
The code commit remains in the manifest, while `calculation_contract_version` must increase for every semantic code change even when the external request schema is unchanged.
A nonsemantic refactor may preserve the calculation contract only when unchanged goldens, mutation tests, and semantic-version review prove that outputs and admissibility are unchanged.
Older results remain immutable and visibly versioned, while an intentional recalculation under a newer contract creates a new semantic identity.

Import identities additionally include the raw content hash, adapter identifier, ESPI schema hash, parser-contract version, and finding-policy version.
Report identities include the accepted result hash, redaction-policy version, and report-template version.
Automated contract fixtures prove that irrelevant object-key ordering does not change an identity and that every difference-making version or input does change it.
Automated contract fixtures also prove that changing the entered bill total, any unsupported-line-item tuple, reconciliation policy, heuristic contract, or rank calendar creates a distinct semantic result without overwriting the older result.

## 16. HTTP API

The initial API is versioned under `/v1`.

```text
POST   /v1/auth/register
POST   /v1/auth/login
POST   /v1/auth/logout
POST   /v1/account/deletion-intents
DELETE /v1/account
GET    /v1/deletions/{deletion_id}

POST   /v1/imports
GET    /v1/imports/{import_id}
POST   /v1/imports/{import_id}/confirm
DELETE /v1/imports/{import_id}

GET    /v1/profiles
GET    /v1/profiles/{profile_id}
DELETE /v1/profiles/{profile_id}

GET    /v1/tariffs
GET    /v1/tariffs/{tariff_version_id}

POST   /v1/replays
GET    /v1/replays/{replay_id}

POST   /v1/comparisons
GET    /v1/comparisons/{comparison_id}

POST   /v1/scenarios
GET    /v1/scenarios/{scenario_id}
POST   /v1/scenarios/{scenario_id}/cancel

GET    /v1/jobs/{job_id}
GET    /v1/reports/{scenario_id}
POST   /v1/reports/{scenario_id}/exports
```

`POST /v1/imports` requires an authenticated RateReplay session, CSRF protection, one streamed multipart file, and a declared adapter.
It never accepts a PG&E username, password, token, or other utility account credential.
It returns `202 Accepted` with an import identifier and state URL.
The durable import job described in Milestone 1 must exist before this asynchronous contract is exposed.
Import confirmation requires a selected admitted billing period, explicit confirmation of the sole supported usage point, a PG&E service attestation, and acknowledgment of every nonfatal quality warning.
Zero or multiple supported usage points remain fatal in V1.

`POST /v1/replays` requires a confirmed profile version, one admitted billing period, one exact tariff version, versioned account facts, and optional current-bill reconciliation inputs.
`POST /v1/comparisons` requires a successful replay and an explicit candidate tariff list, and it returns coverage and eligibility before any ranked cost output.
`POST /v1/scenarios` requires a successful rankable tariff context or one replayable tariff, scenario electrical constraints, and complete versioned flexible-load reference schedules.
Scenario admission validates the combined reference fallback before job creation, returns `422 Unprocessable Entity` with stable constraint codes and witnesses when the fallback is invalid, and never submits a predictably infeasible solver model.
Every compute submission returns a job identifier, immutable operation-request hash, and semantic calculation hash when the semantic inputs are resolved.

All list responses use opaque cursor pagination with a pinned default and maximum page size.
Every resource response includes owner-scoped identity, lifecycle state, schema version, created time, and safe warning codes.
Error responses use a versioned problem schema with stable code, safe message, field paths when applicable, and request identifier.
Object identifiers are unguessable but never substitute for authorization.

Mutating endpoints accept an idempotency key where repeating the operation is meaningful.
The server binds each idempotency key to the authenticated user, route, request-schema version, and canonical operation-payload hash.
Reusing a key with a different request returns a conflict.
Idempotency records remain available for at least 24 hours and never expire while their associated asynchronous operation is nonterminal.

Account deletion uses a recoverable two-step contract followed by a durable ledger-commit protocol.
Before either request, the browser generates and retains a cryptographically random 256-bit deletion receipt secret and a separate idempotency key.
`POST /v1/account/deletion-intents` requires the authenticated session, CSRF protection, the idempotency key, and the receipt secret in a dedicated nonlogged authorization header.
It stores only a slow hash of the receipt secret, creates an opaque deletion intent identifier that will also be the deletion identifier, and makes no lifecycle change.
The database permits only one nonterminal deletion intent or prepared deletion event per target, returns the original operation only for the same idempotent request, and rejects a second independently keyed intent with `409 DELETION_ALREADY_PENDING`.
The canonical operation payload includes a domain-separated hash of the supplied receipt secret, so reusing the key with another secret returns a conflict without logging either value.
Repeating the intent request with the same owner, route, key, request schema, and secret returns the same identifier while the normal account session remains active.
An unconsumed intent with no acknowledged `PREPARED` record expires exactly 15 minutes after creation, has no deletion authority by itself, and is removed by retention.
Once `PREPARED` is acknowledged, intent expiry is suspended and the same event must reach `REQUESTED` or a positively proved `ABORTED` state before another intent can exist for that target.
Retrying its idempotency key after expiry returns `410 INTENT_EXPIRED` and never resurrects the intent, while a new intent requires a new secret and idempotency key.
`DELETE /v1/account` requires the authenticated session, CSRF protection, the unexpired owner-bound deletion intent identifier, and proof of the same receipt secret.
Before changing the database lifecycle, the deletion coordinator derives the restore-suppression scope token and proposed next generation and idempotently appends a durable `PREPARED` record under the stable deletion event identity to the separately credentialed ledger.
`PREPARED` proves that an authenticated deletion attempt may cross a database backup boundary, but it is not a suppressive deletion decision and may coexist temporarily with an `ACTIVE` target.
An unresolved `PREPARED` record is nevertheless a mandatory restore-quarantine hold and may never be ignored or expired by wall-clock time alone.
The deletion coordinator and a bounded startup and periodic preparation reconciler enumerate unresolved ledger preparations, join them to the exact intent and target under least-privilege control-plane credentials, and continue the same event without requiring another browser request.
When the live database still proves the unconsumed intent, original generation, and target identity named by `PREPARED`, that reconciler may execute the already-authorized fencing transaction rather than aborting solely because the HTTP worker crashed.
After verifying the exact `PREPARED` record, one database transaction locks the target and intent, consumes the intent exactly once, records the preparation identity and digest, changes the target from `ACTIVE` to `DELETION_PENDING_LEDGER` at the prepared generation, creates an independently retained deletion-control operation, rejects new ordinary work, requests cancellation of existing ordinary work, and revokes every normal account session.
The client therefore possesses both polling credentials before preparation or the destructive transition begins.
If preparation fails, the database remains unchanged and the authenticated client may retry the same event identity.
If preparation succeeds but the fencing transaction has a definite noncommit outcome, a serializable reconciliation transaction must prove that the same event never consumed the intent or changed the original target generation and must invalidate that intent before the coordinator may append terminal `ABORTED` for that event.
An ambiguous database outcome, unavailable primary, missing transaction log, or failed reconciliation leaves `PREPARED` unresolved and forces quarantine rather than permitting `ABORTED` or restored service.
If the fencing transaction commits, deletion has started, ordinary access cannot resume, and the deletion-control operation retries the idempotent `REQUESTED` transition without requiring a normal account session.
The DELETE endpoint may return a deletion-accepted response only after the ledger acknowledges `REQUESTED`, although the receipt endpoint may expose safe `PREPARED`, `DELETION_PENDING_LEDGER`, and later phases while work continues.
If any response is lost, the client polls the precreated deletion identifier with the retained receipt secret to distinguish an unconsumed prepared attempt from `DELETION_PENDING_LEDGER`, `DELETING`, or a later terminal state.
An attempt proved `ABORTED`, or an unprepared intent that expires under the live original-generation account, permits an authenticated retry with a new intent, while every fenced status requires no authenticated retry and cannot be cancelled back to `ACTIVE`.
`GET /v1/deletions/{deletion_id}` requires the receipt secret in a dedicated authorization header, stores only a slow hash of that secret, and returns only lifecycle status and safe artifact-class counts.
The receipt secret must not appear in URLs, logs, traces, analytics, or browser referrers, and the random deletion identifier must be redacted from telemetry.
The deletion-intent and deletion-status responses use `Cache-Control: no-store` and may not be retained by a service worker or shared cache.
At the transition to `DELETION_PENDING_LEDGER`, the receipt-verifier row and deletion-control operation are outside the swept account scope and retain no username, account fact, profile hash, or other user-derived value.
It remains available while deletion is nonterminal and for exactly 30 days after completion, after which the verifier is destroyed and the status endpoint returns `410 Gone`.

Large uploads use streaming limits and never load the complete file into memory.
The exact request, response, state-machine, validation, and authorization schemas are generated into OpenAPI and checked for backwards-incompatible drift by `make check`.

## 17. Durable job execution

The shared job abstraction supports versioned `IMPORT`, `REPLAY`, `COMPARISON`, `SCENARIO`, `REPORT`, `RETENTION`, and `DELETION` job kinds with kind-specific immutable request schemas.
Milestone 1 implements the complete lease and fencing core for `IMPORT`, and Milestone 5 reuses and extends that core rather than replacing an in-process execution path.

Every job declares exactly one scope mode:

- `ACTIVE_SCOPE` is mandatory for user imports, replays, comparisons, scenarios, and reports, and it requires every captured scope to remain `ACTIVE` at the exact generation.
- `DELETING_SCOPE` is reserved for the one data-sweep operation created after an acknowledged `REQUESTED` event, and it requires that target to remain `DELETING` at the exact fenced generation.
- `SYSTEM_SCOPE` is reserved for versioned retention and orphan-cleanup operations created by named schedulers, and it grants only the artifact classes and lifecycle predicates in that job's immutable request.

The preparation reconciler is a control-plane operation rather than a user-selectable job, and its registry permission is limited to enumerating unresolved event identities, locking their exact intent and lifecycle rows, performing the prepared fence or proved abort, and appending the matching next ledger event.
It may not read normalized interval data, calculation results, reports, or another target's receipt verifier.

No caller may choose its own scope mode.
The job-kind registry fixes the permitted mode, required target fields, and authorization predicate for every request schema version.
An import or profile deletion job also requires its owning account to remain `ACTIVE` at the captured generation.
If account deletion begins, it fences every child deletion job and the account deletion job assumes responsibility for their rows, objects, uploads, and cleanup records.

The state machine is:

```text
queued -> leased -> running -> succeeded
                         |-> failed
                         |-> cancelled
leased or running --lease expiry--> queued for next attempt
queued, leased, or running --cancellation--> cancelled
queued, leased, or running --attempt budget exhausted--> failed
```

Each lease has an owner, acquisition time, expiration time, heartbeat, attempt number, and monotonically increasing fencing generation.
Workers acquire jobs with a transaction and `FOR UPDATE SKIP LOCKED`.
Every heartbeat, cancellation acknowledgment, and finalize operation conditionally updates the row only when the current owner and fencing generation still match.
An expired attempt can never extend its lease or publish after a newer generation is issued.

Each logical calculation is identified by `(owner_user_id, semantic_calculation_hash)` and has at most one successful result row within that owner scope.
The database unique constraint, result lookup path, and result authorization path use that compound semantic identity, while operation idempotency uses the separate identity in Section 15.1.
V1 forbids cross-tenant calculation-result or artifact reuse even when semantic calculation hashes match.
Workers write report or trace artifacts to immutable owner-scoped and attempt-scoped staging keys.
Finalization transactionally inserts or selects the unique result, records the accepted artifact keys, and transitions the matching fenced attempt to a terminal state.
Artifacts from rejected, expired, or crashed attempts remain unpublished and are removed by an idempotent orphan sweeper.

Every account, import, and profile that can be deleted has an `ACTIVE`, `DELETION_PENDING_LEDGER`, `DELETING`, or `DELETED` lifecycle state and a monotonically increasing lifecycle generation.
Every `ACTIVE_SCOPE` job captures the account generation and every relevant import or profile generation when it is created.
Its lease acquisition, heartbeat, retry, artifact acceptance, and finalization require every captured scope to remain `ACTIVE` at the matching generation.
After authorization and CSRF validation, the coordinator first appends and verifies the idempotent external `PREPARED` record for the stable event identity, target scope token, restore-key version, original lifecycle generation, and proposed next generation.
Only after that proof may the database fencing transaction change the target scope from `ACTIVE` to `DELETION_PENDING_LEDGER`, increment its generation to the prepared value, consume the intent, record the preparation digest, fence new dependent work, request cancellation of queued, leased, and running `ACTIVE_SCOPE` jobs, revoke normal sessions for account deletion, and create exactly one target-bound control-plane operation outside the swept user-data scope.
The control operation then appends and verifies an idempotent `REQUESTED` restore-suppression record for that same event, scope token, preparation digest, and fenced generation.
The `REQUESTED` append is allowed only while the target is `DELETION_PENDING_LEDGER` at that exact generation and contains the matching durable preparation identity.
After the ledger acknowledges `REQUESTED`, a database transaction changes the target from `DELETION_PENDING_LEDGER` to `DELETING` without changing the fenced generation and creates exactly one control-plane-scoped `DELETING_SCOPE` sweep job.
If the append or the post-append database transaction fails, the target remains non-active and fenced, the control operation retries from its durable phase, and the receipt reports the exact pending phase without permitting ordinary access or cancellation.
An acknowledged `REQUESTED` record is therefore the authoritative restore-suppression decision and can never coexist with an `ACTIVE` target under the protocol, while an unresolved `PREPARED` record is a non-suppressive quarantine hold that requires reconciliation.
A lost response at any outcome is resolved through the already-held deletion identifier and receipt secret rather than by assuming whether deletion began.
The sweep job may lease, heartbeat, retry, and enter its finalization transaction only while the target remains `DELETING` at the captured generation.
An older or duplicate deletion job cannot operate after the generation changes.

Deletion progresses through persisted, retryable phases `FENCE`, `DRAIN`, `SWEEP`, `VERIFY`, and `COMPLETE`.
`FENCE` records every older-generation job attempt and registered object upload that can still touch the target and revalidates the durable `REQUESTED` suppression-ledger acknowledgment before destructive work begins.
Every staged upload or multipart upload must register its attempt-scoped key and upload identifier transactionally before transferring bytes.
`DRAIN` waits until every recorded older-generation lease has terminated or expired and every registered upload has completed, verifiably aborted, or reached a cleanup-owned terminal state that the storage adapter proves cannot accept or finalize more bytes.
A worker that loses heartbeat or finalization must synchronously delete its staged object or abort its multipart upload when possible and must leave a durable cleanup record when immediate cleanup fails.
`SWEEP` removes user-data database rows, cache entries, exports, accepted objects, staged objects, failed multipart uploads, sessions, and idempotency records for the target generation and all earlier generations.
`SWEEP` must not remove or mutate the active lifecycle control row, deletion-control operation, current deletion job and attempt, phase checkpoint, receipt-verifier row, suppression-ledger identity, or minimum deletion-audit fields.
Those control-plane records are stored outside the swept user-data ownership scope, contain only the explicitly permitted deletion metadata, and remain available through crash recovery and fenced finalization.
`VERIFY` performs a final strongly consistent prefix listing and database check only after no older-generation writer can start or finish another write.
An object-store adapter that cannot provide the required read-after-write and listing consistency cannot pass `HOSTED_VALIDATED`.
After `VERIFY` finds zero prohibited live artifacts, the control operation appends and verifies the idempotent `COMPLETED` suppression-ledger record.
One database transaction then checks the current deletion job fence and the target's `DELETING` state and generation, marks the deletion job successful, changes the lifecycle state to `DELETED`, replaces resumable phase state with the permitted minimum tombstone, and makes completion visible to the receipt endpoint.
The receipt may not report completion before that transaction commits.
Every phase is idempotent, and a deletion-worker crash resumes from the persisted phase without relaxing its generation checks.
The deletion receipt endpoint remains independent of the deleted account session and exposes no user-derived identifiers.

Required failure behavior includes:

- A worker crash before calculation begins releases through lease expiry.
- A worker crash after staged artifact creation but before commit leaves only a sweepable unpublished object.
- Retrying a completed idempotent calculation returns the existing result.
- Cancellation before leasing prevents execution.
- Cancellation after leasing or during execution is cooperative, fences finalization, and never publishes a partial result as successful.
- Permanent validation failures do not retry.
- Transient storage or database failures retry with bounded exponential backoff and jitter.
- Retry exhaustion produces a terminal failure with a safe stable code and preserves attempt diagnostics without private interval data.
- A stale worker racing a replacement worker loses the fenced finalize operation even when it finishes first at the object store.
- A worker racing account, import, or profile deletion loses heartbeat and finalization after the lifecycle generation changes.
- The authorized `DELETING_SCOPE` job continues only at the exact post-transition generation while every `ACTIVE_SCOPE` job remains fenced.
- A target remains fenced in `DELETION_PENDING_LEDGER` while a failed `REQUESTED` append or post-append database transition retries, and it never returns to `ACTIVE`.
- A primary loss after `PREPARED` but before database fencing or `REQUESTED` leaves a restore-quarantine hold that cannot be cleared without positive commit or noncommit evidence for that event.
- An HTTP worker crash immediately after `PREPARED` is recovered by the preparation reconciler, which either performs the exact authorized fence or retains the hold without relying on a user retry.
- Deletion cannot report completion while an older-generation lease, staged upload, or multipart upload can still produce an object.
- A crash during or after `SWEEP` cannot remove the control records required to resume `VERIFY`, append `COMPLETED`, and finalize the deletion atomically.

Do not add Redis or a separate queue service unless PostgreSQL behavior fails a measured requirement.

## 18. Web product

The web application contains:

- A public landing and built-in demo entry.
- Account and session flow.
- Upload drop zone with privacy notice.
- Import-quality report.
- Profile and billing-period review.
- Tariff eligibility questionnaire.
- Bill replay with expandable line items.
- Reconciliation view with unsupported charges and residual.
- Tariff comparison table.
- Eligibility and comparable-component coverage view that blocks ranking when coverage is incomplete.
- EV and appliance constraint editor.
- Reference, heuristic, optimal, or best-found result comparison with status-accurate labels.
- Measured-profile decomposition view showing background, existing flexible load, historical hypothetical addition, and reconstruction checks.
- Daily and monthly cost charts.
- Interval heatmap showing shifted energy.
- Pre-solve constraint-admission and solver-status explanation.
- Tariff source and provenance view.
- Redacted report preview and export.
- Deletion and retention controls.

All charts must have accessible tabular equivalents.
The complete workflow must support keyboard navigation, visible focus, useful error messages, and responsive layout.
Color may not be the only carrier of savings, warnings, or unsupported scope.
The UI may use the word savings only for a rankable comparison and must otherwise use supported-subtotal difference or no comparison.
Daily charts are private diagnostic allocations rather than independent daily bills.
They allocate traceable interval energy charges by service day and place fixed, minimum-bill, tier-reset, residual, and other billing-period adjustments in separately labeled period-level rows that reconcile to the result total.

The V1 redacted export uses a deny-by-default field allowlist.
It may include the admitted billing period, aggregate energy, supported charge components, unsupported component names without user-entered text, signed residual, comparison coverage, aggregate shifted energy, tariff provenance, solver and verifier status, result versions, and limitations.
It excludes raw interval readings, daily series, exact reference or optimized slots, occurrence windows, filenames, source identifiers, utility identifiers, report-internal object keys, free-form user text, and sensitive account facts.
The report preview renders the exact redacted export schema rather than a richer private object.
A new export field requires a redaction-policy version change, privacy review, and schema snapshot approval.

## 19. Privacy and security

Interval usage can reveal household occupancy and behavior.
Treat it as sensitive data even when the source file contains no obvious name or address.

Required controls include:

- No utility credentials in V1.
- The frozen local username and password contract with Argon2id hashing, bounded login work, and no credential recovery.
- Secure, HTTP-only, same-site session cookies.
- Session rotation on login and privilege changes, bounded idle and absolute lifetimes, and server-side revocation on logout or deletion.
- CSRF protection for cookie-authenticated mutations.
- Strict origin and CORS allowlists plus a restrictive content security policy for deployed environments.
- Per-object authorization on every import, profile, replay, comparison, scenario, job, result, export, and report.
- Encryption in transit.
- Managed encryption at rest for raw objects, normalized PostgreSQL data, reports, and backups in deployed environments.
- Raw upload retention of no more than 24 hours from upload creation whether or not confirmation occurs.
- Confirmed raw uploads enter immediate deletion when normalization no longer needs them, with 24 hours as a maximum rather than a target.
- User-triggered deletion for raw and normalized data.
- PII and utility-identifier stripping before normalized persistence.
- File size, row count, nesting, and decompression limits.
- External entity and network resolution disabled for XML.
- No interval values, filenames, addresses, meter identifiers, or report contents in logs.
- Redacted sharing by default.
- No detailed interval-history export in V1.
- No exact load slot, occurrence-window, or daily-series field in the redacted export.
- Rate limits on authentication, upload, and job creation.
- A content-addressed, read-only public demo artifact set containing simulated data only, with no anonymous API mutation, upload, job, shared account, server-side visitor state, or private-data reference.
- Local dependency, container, secret, and static-analysis gates through `make check` and `make dependency-audit`.

The deletion test must exercise the user-facing path and verify database rows, object-store objects, staged artifacts, caches, exports, sessions, idempotency records, and queued, leased, running, retryable, and finalizing work.
It must inject deletion before and after `PREPARED`, at the initial fencing transaction, before and after `REQUESTED`, at the post-request state transition, lease acquisition, heartbeat, artifact staging, result finalization, report export, retry creation, orphan sweeping, every persisted deletion phase, `COMPLETED`, and the atomic terminal transition.
The tests must prove that a changed lifecycle generation prevents data from reappearing, that no `REQUESTED` or `COMPLETED` record coexists with an active target, that an unresolved `PREPARED` record always holds a restore in quarantine, and that `SWEEP` cannot delete the control state needed to resume.
Deletion-audit tombstones may retain only a random deletion event identifier, the slow deletion receipt verifier and its fixed expiry, a keyed opaque restore-suppression scope token, restore-key version, deletion generation, completion time, artifact-class counts, and non-user-derived status codes.
The scope token is a domain-separated HMAC of the random internal scope identifier under a restore-only key and is unavailable to application request paths.
Tombstones may not retain profile hashes, filenames, utility identifiers, request bodies, account facts, or interval-derived values.

Before the primary database fencing transaction, the deletion coordinator appends a `PREPARED` record containing the deletion event identity, scope token, restore-key version, original generation, proposed generation, preparation time, and intent-proof digest to a separately credentialed append-only ledger outside the primary database backup set.
The only legal append-only event chains are `PREPARED -> REQUESTED -> COMPLETED` and `PREPARED -> ABORTED`.
The ledger rejects another first event with the same deletion identity, a mismatched scope token or generation in a later event, `ABORTED` after `REQUESTED`, `REQUESTED` after `ABORTED`, and every duplicate whose canonical bytes differ from its original event.
The ledger adapter must provide durable conditional append by event identity and phase, strongly consistent chain reads, complete enumeration of unresolved preparations, and a signed or keyed integrity receipt for every acknowledged canonical record.
An adapter that cannot prove those properties cannot pass `LOCAL_REPRODUCIBLE` or `HOSTED_VALIDATED`.
`PREPARED` is not suppressive and may coexist with an `ACTIVE` database row, but every restore treats an unresolved preparation as a quarantine hold because the missing database transition may have existed only after the restored backup.
After the primary database has durably fenced the exact prepared target in `DELETION_PENDING_LEDGER`, the deletion-control operation appends `REQUESTED` with the same identity, token, preparation digest, restore-key version, fenced generation, and request time.
The restore process suppresses both `REQUESTED` and `COMPLETED` entries so a crash cannot reactivate a deletion that was accepted.
The coordinator may append `ABORTED` only after authoritative serializable database and durable transaction-outcome evidence proves that the prepared event did not fence the target, the target remains `ACTIVE` at the original generation, and the matching intent has been invalidated.
After `VERIFY`, the deletion job appends `COMPLETED` with the same identity and completion time before the receipt may report completion.
`LOCAL_REPRODUCIBLE` uses a separate append-only ledger volume excluded from the database-backup fixture, while `HOSTED_VALIDATED` requires a separately credentialed durable store.
Every unresolved `PREPARED` chain is retained indefinitely until reconciled, and every terminal chain is retained for at least the maximum backup retention plus seven days after its terminal event.
The ledger is encrypted, integrity checked, access audited, and covered by a tested key-rotation procedure.
Every deletable database and object-store scope retains enough random internal scope identity for the restore tool to recompute the token without retaining user-derived identifiers.

Encrypted backups, if enabled, expire within 30 days and are not rewritten for an individual deletion.
The product must state that deleted data may remain in encrypted backups until that deadline and must verify restoration procedures do not silently reintroduce expired live records.
Every restore occurs in a network-isolated quarantine environment.
The restore tool rolls forward every available transaction log, loads the latest verified ledger, validates every event chain, deletes every scope matched by `REQUESTED` or `COMPLETED`, and reruns retention expiry.
For each unresolved `PREPARED`, it compares the event identity, scope token, original and proposed generations, preparation digest, restored lifecycle row, deletion-control state, and authoritative durable transaction outcome.
If positive evidence proves that the fence committed, the restored target remains inaccessible while the coordinator idempotently completes `REQUESTED` and suppression.
If positive evidence proves that the fence did not commit, the target is still `ACTIVE` at its original generation, and the intent is durably invalidated, the coordinator may append `ABORTED` and remove the hold.
If the evidence is missing, ambiguous, stale, or unavailable, the restore remains quarantined and no target state is guessed from the age of the preparation.
No restored service becomes reachable until every preparation is terminal, every suppressive scope is absent, and the complete reconciliation artifact passes verification.
A missing, stale, unverifiable, or undecryptable deletion ledger fails the restore closed.
The restore drill must start from a backup that predates at least one deletion and one retention expiry and must inject primary loss after `PREPARED`, after database fencing, and after `REQUESTED`.
Encryption configuration, key ownership, rotation, backup expiry, and object-store multipart cleanup are deployment acceptance items rather than documentation-only claims.

## 20. Observability

Structured logs contain request IDs, safe user pseudonyms, job IDs, stable error codes, durations, and versions.
Logs never contain interval readings or user-provided bill details.

Metrics include:

- Import requests and outcomes.
- Parser duration and peak memory.
- Quality findings by code.
- Scenario queue depth.
- Lease age and retry count.
- Scenario latency by workload size.
- Solver status and duration.
- Report generation latency.
- API error rate.
- Deletion completion and failure.

Traces connect upload confirmation, job creation, worker execution, result persistence, and report retrieval.
Tracing attributes follow the same privacy restrictions as logs.

## 21. Testing strategy

### 21.1 Unit tests

- Unit and multiplier conversion.
- Exact integer-watt-hour admission and nonintegral rejection.
- UTC and local-time conversion.
- Half-open interval classification.
- Decimal and rounding rules.
- Every tariff AST node.
- Every canonical charge IR operator and bound check.
- Eligibility predicate tri-state behavior.
- Comparable-component coverage decisions.
- Existing-load subtraction and historical-addition reconstruction.
- Discriminated flexible-load variants and rejection of every invalid field combination.
- Full reference-slot identity, duration, order, zero-outside-window, and fixed-shape validation.
- Pairwise-disjoint occurrence windows per physical load, unique scenario-local physical asset keys, and `OVERLAPPING_LOAD_OCCURRENCES` rejection.
- Half-open occurrence windows, exact canonical-boundary alignment, and `NON_ALIGNED_OCCURRENCE_BOUNDARY` rejection.
- Constraint construction.
- Independent schedule verification.
- Reconciliation arithmetic.
- Stable hashes and manifests.
- Canonical profile-content serialization that ignores random persistence identities and source order while changing for every calculation-relevant field, finding, policy, period, timezone, or warning acknowledgment.

### 21.2 Property tests

- Import normalization is idempotent.
- Reordering independent source entries does not change normalized output.
- Energy is conserved through parsing and scheduling.
- Charge totals equal the sum of emitted line items after declared rounding.
- Complete TOU calendars classify every supported instant exactly once.
- Adding a nonnegative fixed charge cannot reduce a bill.
- An unchanged-load scenario reproduces historical replay.
- A returned solver candidate replaces the valid reference only when its complete four-stage tuple is lexicographically strictly smaller, and an equal or worse tuple returns the reference.
- A heuristic incumbent replaces the valid reference only when its complete proxy objective pair is lexicographically strictly smaller.
- Subtracting and restoring all `SHIFT_EXISTING` reference schedules exactly reconstructs the measured profile.
- A `HISTORICAL_ADDITION` changes only the admitted historical counterfactual and never emits forecast language.
- A non-rankable comparison never emits savings, a winner, or a recommendation.
- Semantically irrelevant request-key ordering preserves the semantic calculation hash, while every difference-making calculation version changes it.
- Changing the entered bill total, unsupported-line-item tuple, reconciliation policy, heuristic contract, or heuristic rank calendar changes the semantic calculation hash.

### 21.3 Golden tariff tests

Synthetic tariff definitions may exercise generic calendar and IR boundaries outside the admitted July window, but they must be labeled synthetic and cannot support a PG&E bill claim.

- One example for every rule type.
- One interval immediately before, at, and after each time boundary.
- Summer and winter transitions.
- Weekday, weekend, and holiday transitions.
- Missing, stale, mismatched, and mutated holiday-calendar source locks.
- Billing periods crossing an effective-date change.
- Tier boundaries.
- Baseline territory and heating-source variants.
- Minimum-bill behavior.
- Rounding boundaries.

Golden expected values must be frozen before the corresponding production evaluator is implemented.
Each golden fixture includes source page and rule identifiers, inputs, hand-derived intermediate values, rounding boundaries, expected outputs, author, review method, and review status.
A transparent reference worksheet or script may support the derivation but may not import production tariff, compiler, billing, solver, or fixture-generation code.
Every admitted tariff version requires at least one complete-bill golden and boundary goldens for every rule it uses.
Mutation tests must prove that changing each rate, date, time boundary, tier, applicability predicate, or rounding operator breaks an appropriate golden.

### 21.4 Import tests

- Valid ESPI XML.
- Independently sourced ESPI conformance samples with permitted redistribution.
- Forward and backward Atom link order.
- Missing, dangling, duplicate, and ambiguous ESPI relations.
- Wrong commodity, service category, reading kind, accumulation behavior, time attribute, data qualifier, and reading quality.
- Valid supported PG&E CSV.
- Twenty-three-hour spring-forward day.
- Twenty-five-hour fall-back day.
- Hourly and fifteen-minute data.
- Duplicate, missing, overlapping, and non-monotonic intervals.
- Unknown units and multipliers.
- Multiple usage points.
- Import and export directions.
- XML entity expansion and oversized payloads.
- Fatal, warning, and informational finding-policy behavior.
- Complete selected-period coverage and every semantic boundary type.
- Supported PG&E CSV header fingerprint and unknown-format rejection.

### 21.5 Optimizer tests

- Exhaustive enumeration on small horizons.
- Feasible EV windows and invalid reference windows rejected before solver submission with the intended stable constraint witness.
- Interruptible and contiguous loads.
- Multiple loads competing for a power cap.
- Multiple nonoverlapping occurrences for one physical load and rejected overlapping occurrences that would otherwise double its physical power allowance.
- Existing-load and historical-addition scenarios that prove no double counting.
- Historical-addition scenarios that remain on the admitted service timestamps and expose no forecast claim.
- Aligned and nonaligned occurrence-window boundaries, including repeated local times represented by distinct UTC instants.
- Fixed-shape appliances that cannot be power-modulated.
- Exact changed-entry, completion-index, and stable slot-order objective values under canonical occurrence and schedule-vector ordering.
- A primary `FEASIBLE` result skips lower-priority stages, while an open lower-priority stage records the highest proved stage and never receives an overall `OPTIMAL` label.
- Solver timeout with a nonzero optimality gap.
- Independent cost recomputation.
- Mutation tests that deliberately violate each constraint.
- Cross-backend randomized equivalence for every admitted optimizable IR composition.
- Signed 64-bit overflow and non-exact coefficient rejection.
- Deterministic off-peak proxy ranking, verified incumbent handling, and reference fallback.
- Solver and heuristic incumbents with better, equal, and worse complete tuples relative to the reference.
- Optimal, best-found, pre-solve invalid-reference, internal model-contract-violation, unknown, and model-invalid status labeling.

### 21.6 Integration tests

- Import to confirmation.
- Confirmation to bill replay.
- Scenario creation to worker result.
- Crash after lease acquisition.
- Import-worker crashes before parsing, during parsing, before draft publication, and after draft publication.
- Stale attempt finalization after a replacement lease.
- Crash before and after staged artifact creation and before fenced database finalization.
- Orphan sweep without removal of an accepted artifact.
- Duplicate idempotency key.
- Cancellation race.
- Account, import, and profile deletion races at lease acquisition, heartbeat, retry, artifact staging, finalization, export, and sweeping.
- An authorized deletion job leases and retries while its target is `DELETING`, while ordinary work cannot lease at that generation.
- Deletion returns no accepted response before `REQUESTED`, and a retry after an acknowledged `PREPARED` or `REQUESTED` append reuses the same event identity and canonical record.
- A failed `PREPARED` append leaves the database unchanged, while a failed `REQUESTED` append leaves the target fenced in `DELETION_PENDING_LEDGER` and an acknowledged suppressive event can never coexist with an `ACTIVE` target.
- An unresolved `PREPARED` record survives simulated primary loss and keeps a restore quarantined until positive outcome evidence permits `REQUESTED` or `ABORTED`.
- An HTTP-process crash after `PREPARED` is discovered by the startup reconciler and completes the same event without creating a second intent or requiring the client to retry.
- Crashes before and after `SWEEP`, `VERIFY`, the `COMPLETED` append, and the atomic terminal transition preserve enough control state to resume without reviving user data.
- Deletion waits for an in-flight staged or multipart upload, removes its completed object, performs the final sweep, and only then reports completion.
- A deletion-worker crash resumes each persisted deletion phase idempotently.
- A restore from a backup predating deletion and retention expiry reapplies verified suppressive events and reconciles every unresolved preparation before the restored service can leave quarantine.
- Equal semantic calculation hashes in separate accounts without cross-tenant reuse or interference.
- A calculation-contract version change creates a new owner-scoped semantic result without overwriting the older result.
- Distinct current-bill totals, unsupported-line-item tuples, and reconciliation-policy hashes create distinct owner-scoped semantic results and residuals.
- Storage failure and bounded retry.
- A generated authorization matrix that rejects cross-account access for every direct and indirect profile, import, replay, comparison, scenario, job, result, and report identifier.
- Deletion-intent creation, idempotent recovery, expiry, owner binding, receipt-secret verification, single consumption, and status authorization.
- Concurrent deletion-intent creation permits only one nonterminal event per target, and an acknowledged preparation suspends expiry until `REQUESTED` or proved `ABORTED`.
- Lost-response injection before intent commit, after intent commit, before deletion commit, and after deletion commit, proving that the client can safely recover or poll without a normal session after deletion starts.
- Raw retention expiry.
- Complete deletion.

### 21.7 Browser tests

- Built-in demo from landing page to report.
- Two independent browser contexts navigating the static demo without issuing a mutation or job request and without observing or changing shared visitor state.
- Real fixture upload from selection to quality confirmation.
- Unsupported-account warning.
- Tariff comparison.
- Feasible EV scenario.
- Invalid reference-window and aggregate-cap explanations before solver submission.
- Keyboard-only completion.
- Session expiry.
- Deletion confirmation.
- Authenticated upload rejects a missing application session and never requests utility credentials.
- Redacted export snapshot contains only allowlisted aggregate fields and no exact load schedule or daily series.

## 22. Performance and reliability evaluation

Use ingestion-only profiles containing one month, six months, and one year of fifteen-minute intervals.
Use tariff-backed replay, comparison, and optimization profiles only for source-complete admitted service windows.
The frozen July 2026 public benchmark uses one billing period, one and three admitted tariff candidates, and zero, one, and five flexible loads.
A separate one-year synthetic-tariff engineering benchmark may test scaling, but it must be labeled synthetic and may not support a PG&E bill or savings claim.

Record:

- Import wall time.
- Import peak resident memory.
- Bill replay wall time.
- Solver wall time.
- Report generation time.
- API p50, p95, and p99 latency under a documented local load.
- Worker recovery time after termination.
- Duplicate-result count.
- Database and object-store sizes.
- Cold-start and warm-cache results as separate series.
- Result variation across at least ten measured repetitions where the operation is nondeterministic or affected by system load.

Milestone 0 must produce a versioned acceptance charter for ingestion, replay, API, memory, durable-import recovery, and duplicate-result behavior after its feasibility spikes and before Milestone 1 implementation begins.
Milestone 4 must extend that charter with optimization workloads, solver limits, scenario latency, and scenario-worker recovery before performance tuning begins.
Each charter freezes numeric thresholds or exact invariants, named hardware, workload hashes, process topology, lease and heartbeat settings, warmup, cache state, repetition count, aggregation rules, and permitted variance.
Duplicate successful results have the threshold zero, and every other published performance or recovery claim must have an explicit pass threshold rather than a record-only metric.
The final README reports measured results and named hardware rather than invented scale claims.

The frozen workload manifest defines exact input hashes, admitted tariff vectors, load schedules, solver parameters, concurrency, process state, cache policy, repetition count, and named hardware.
An initial user-facing target is a warm cached comparison response under one second, an uncached July comparison under three seconds, and a July optimization scenario under ten seconds on the named developer machine.
Missing a frozen target fails the corresponding charter.
A later scope or target revision requires a new charter version, written justification, and publication of the original failed result beside the new result.
The release gate evaluates the newest pre-execution charter but may not erase or relabel a prior failure.

## 23. Public evidence artifacts

The final repository includes:

- A two-minute product demonstration.
- An architecture diagram.
- A supported-charge matrix.
- A tariff source lock.
- Golden calculation tables.
- Parser quality examples.
- Optimizer-versus-exhaustive-oracle results.
- Worker crash-recovery evidence.
- Operation-idempotency and semantic-calculation identity fixtures.
- Deletion quiescence and pre-deletion-backup restore evidence.
- Measured latency and memory tables.
- A privacy and threat-model document.
- A limitations document.
- A redacted example report.
- The versioned redacted-export allowlist and schema snapshot.
- Reproduction commands for every published result.

Any anonymized real-bill reconciliation must have explicit permission and must not commit sensitive source data.
The public proof remains valid with simulated profiles and independently constructed golden bills if no real bill can be shared.

### 23.1 Frozen user-comprehension protocol

The release study uses at least five first-time participants who have not read implementation documents and do not receive coaching during the task.
Each participant starts at the public demo, completes import review, bill replay, comparison, load scheduling, and report review, then answers five fixed questions about the residual, unsupported scope, reference schedule, historical-addition meaning, and solver status.
Success requires independent workflow completion and correct answers to all five questions, including that a historical addition is not a future forecast.
At least four of five participants must succeed.

The study artifact records the frozen script, task completion, wrong turns, question scores, duration, and anonymized observations.
It records every participant, including failures, and does not contain utility data or other sensitive personal information.
Failure requires an explanation-design revision and a new versioned study run with fresh first-time participants rather than removal of the failed participant.

## 24. Milestone plan

### Milestone 0 - Feasibility, sources, and repository foundation

Deliverables:

- Initialize the Python and TypeScript workspaces.
- Pin formatters, linters, type checkers, test runners, and dependency locks.
- Add local gates for formatting, linting, typing, unit tests, secret scanning, and dependency review.
- Lock the ESPI schema, namespaces, code tables, relationship rules, and independently sourced conformance fixtures.
- Acquire one sanitized provider-produced PG&E CSV fixture, verify permission to retain its structure, freeze its header and time contract, and record the redaction procedure before CSV implementation begins.
- Archive source metadata, stable regulator identifiers, component effective dates, and hashes for July 2026 E-1 and every candidate tariff.
- Lock every holiday and exceptional-day calendar source, version, jurisdiction, effective range, and content hash used by a candidate time-of-use tariff.
- Decide redistribution and content-addressed retention policy for each source.
- Define the exact target-account predicate, supported-charge matrix, comparison-component matrix, and per-tariff admission checklist.
- Create the frozen July 2026 simulated profile and hand-derived E-1 golden worksheet before implementing the production evaluator.
- Spike secure relationship-aware ESPI parsing.
- Specify the canonical charge IR, exact numeric bounds, reference-evaluator boundary, solver-lowering boundary, four-stage objective tuple, off-peak proxy heuristic, and independent oracle boundary.
- Spike one small exact E-1 scheduling objective and an independent exhaustive oracle.
- Freeze the V1 integer-watt-hour admission rule and its fatal nonintegral conversion behavior against every locked input fixture.
- Freeze `CanonicalProfileContentV1`, its domain separator, binary encoding, field inclusions and exclusions, ordering, and golden byte vectors before normalized persistence implementation.
- Freeze the first performance acceptance charter after measured feasibility spikes and before Milestone 1 implementation.
- Record architecture decisions for money, time, storage, comparison, source composition, solver semantics, operation versus semantic identity, local authentication, job scope modes, deletion quiescence, prepared-event reconciliation, restore suppression, and the reference deployment topology.
- Freeze the static public-demo manifest schema, artifact allowlist, content-addressing rule, generation command, and prohibition on anonymous API mutation.
- Specify the client-prepared deletion-intent and lost-response recovery contract before authentication implementation.
- Specify the external `PREPARED`, database fence, `REQUESTED`, `COMPLETED`, and proved-noncommit `ABORTED` protocol, control-plane storage boundary, restore-quarantine reconciliation, and atomic terminal transition before authentication implementation.

Acceptance gate:

- Every rule required by the July E-1 vertical slice has an authoritative retrievable source and frozen independent expected value.
- The July E-1 component vector covers every service instant exactly once.
- Candidate tariff eligibility and source gaps are documented before any candidate is advertised as supported.
- Every candidate time-of-use calendar has a complete, source-locked holiday and exceptional-day dependency or remains unadmitted.
- The parser accepts an independently sourced conforming fixture and rejects malicious XML, broken relationships, wrong commodities, and unsupported reading semantics.
- A real provider-produced PG&E CSV fixture passes provenance, sanitization, redistribution, header, unit, timezone, and DST review, or CSV is removed from V1 scope and acceptance before Milestone 1.
- Every accepted fixture converts exactly to integer watt-hours, and a synthetic nonintegral conversion fails with `NON_INTEGRAL_WATT_HOUR` without rounding.
- Canonical profile hash goldens remain unchanged under random persistence identities and source-entry reordering and change under every calculation-relevant mutation.
- The reference IR evaluation, solver lowering, and independent exhaustive oracle agree on the spike.
- The authentication and deployment ADR resolves every decision listed in Section 7.4.
- The demo ADR proves that the complete public walkthrough can be generated from simulated static artifacts without a shared account, anonymous API mutation, or server-side visitor state.
- The deletion-intent contract has state-machine tests for idempotent creation, expiry, consumption, response loss, and status access after normal-session revocation.
- The deletion protocol model proves that `PREPARED` precedes the database fence, the fence precedes `REQUESTED`, no `REQUESTED` or `COMPLETED` record coexists with `ACTIVE`, unresolved preparation blocks restore exposure, and sweep and terminal crashes preserve resumable control state.
- The first performance acceptance charter is committed and machine-readable.
- `make check` passes from a clean checkout.

Commit expectation:

- One repository-foundation commit.
- One evidence-and-feasibility commit if the source work is substantial.

### Milestone 1 - Canonical ingestion

Deliverables:

- Implement streaming ESPI parsing.
- Implement the locked PG&E CSV adapter.
- Resolve the Atom relationship graph and enforce the pinned ESPI reading contract.
- Normalize exact energy, UTC time, direction, quality, local-time parameters, and redacted identifiers.
- Persist immutable draft imports and quality findings.
- Implement the production local username and password contract, Argon2id hashing, server-side sessions, logout revocation, CSRF protection, and owner checks for import and profile resources.
- Implement the reusable PostgreSQL job, attempt, lease, fencing-generation, heartbeat, bounded-retry, and terminal-publication primitives needed for durable `IMPORT` jobs.
- Run every asynchronous import through that durable worker path before exposing the `202 Accepted` API.
- Do not expose private upload through an unauthenticated or temporary single-user bypass.
- Implement confirmation and raw-file expiry.
- Add the import-quality API and minimal UI.

Acceptance gate:

- All conforming, malformed, relationship, commodity, quality, DST, duplicate, overlap, gap, multiplier, nonintegral-energy, direction, and security fixtures behave according to the frozen finding policy.
- Reimporting identical semantic content with different persistence UUIDs, random source references, and independent Atom entry order yields the same canonical profile-content hash.
- Changing any calculation-relevant interval, quality finding, policy version, billing period, timezone, or acknowledged warning identity changes the canonical profile-content hash.
- Unsupported data cannot become calculation-eligible.
- A selected billing period cannot confirm without complete, disjoint, one-resolution coverage.
- The CSV adapter accepts the sanitized provider-produced locked format and rejects unknown fingerprints or ambiguous local timestamps.
- Worker termination before parsing, during parsing, before draft publication, and after draft publication recovers within the frozen charter without duplicate confirmed drafts or terminal results.
- Duplicate import submissions and stale import finalizers obey owner-scoped idempotency and fencing rules.
- Registration, login, logout, session expiry, CSRF rejection, and the generated import and profile authorization matrix pass before private upload is exposed.
- Logs contain no sensitive fixture values.

Commit expectation:

- Separate authentication, parser, persistence, API, and UI commits when each leaves the repository passing.

### Milestone 2 - E-1 vertical slice from source to replay

Deliverables:

- Implement the typed tariff schema, component-version model, canonical charge IR, and reference evaluator.
- Implement source, version, eligibility, and admission locks.
- Implement coverage, component composition, units, bounds, eligibility, date, tier, rounding, and ambiguity validators.
- Encode July 2026 E-1 only.
- Implement billing-period resolution, auditable line items, optional current-bill reconciliation, and provenance views for E-1.
- Produce normalized AST, IR, eligibility, component-vector, source, and golden-coverage reports.

Acceptance gate:

- July E-1 compiles deterministically and covers the complete locked service window.
- Deliberate gaps, overlaps, invalid tiers, unknown units, and source mismatches fail.
- Every E-1 rule is linked to a pre-frozen independent golden and retrievable source identifier.
- Complete-bill, tier, baseline, income-tier applicability, fixed-charge, and rounding goldens pass.
- Charge totals equal emitted line items, and unsupported items and residuals remain visible without a forced match.
- Reconciliation results record the exact reconciliation-input and policy hashes needed by the later semantic-reuse key.
- Mutating every encoded E-1 rate or boundary breaks an intended golden.
- Compiler output hashes are stable.

### Milestone 3 - Tariff admission and comparable plan replay

Deliverables:

- Admit E-TOU-C, E-TOU-D, E-ELEC, and EV2-A one at a time only after each source, eligibility, component, golden, and IR gate passes.
- Implement time-of-use, baseline-credit, minimum-bill, and other newly required IR operators through independent goldens before the dependent tariff is admitted.
- Implement tri-state dated eligibility evaluation and target-account validation.
- Implement comparison-component coverage compilation and blocked-comparison reasons.
- Implement alternative-plan replay without current-bill residual propagation.
- Build eligibility, coverage, rankable comparison, blocked comparison, and provenance views.

Acceptance gate:

- At least three tariffs are admitted for the same July 2026 account and service window, including E-1, one general TOU tariff, and one EV-eligible tariff.
- Every admitted tariff passes its independently frozen complete-bill, eligibility, time, component-version, and rounding goldens.
- Missing account facts yield `UNKNOWN`, and ineligible tariffs cannot enter a ranking.
- A tariff-dependent unsupported component blocks ranking and savings output.
- User-entered unsupported charges and residuals appear only on the current replay.
- Mutating coverage or eligibility rules causes the intended comparison gate to fail.

### Milestone 4 - Flexible loads and verified optimization

Deliverables:

- Implement existing-load and historical-addition domain models, reference schedules, exact profile decomposition, appliance shapes, and site caps.
- Implement unique physical-asset identity and pairwise-disjoint occurrence-window validation for every load.
- Implement aligned half-open occurrence windows and stable rejection of nonaligned boundaries.
- Implement the reconstructed reference baseline and the versioned off-peak proxy heuristic contract.
- Implement exact CP-SAT lowering from admitted `CompiledChargeIR` operators with bound validation and staged lexicographic solves.
- Implement independent verification, reference cost recomputation, and versioned verification records.
- Implement pre-solve reference-validation explanations and fail-closed solver contract-violation handling.
- Add optimizer property, mutation, randomized cross-backend, and independent exhaustive-oracle tests.
- Freeze the optimization and scenario-worker additions to the performance acceptance charter before performance tuning.

Acceptance gate:

- Existing-load and historical-addition fixtures prove exact reconstruction and no double counting.
- Overlapping occurrences for one physical asset fail before solver construction with `OVERLAPPING_LOAD_OCCURRENCES`.
- Every historical addition remains on the admitted service timestamps and is labeled as a counterfactual rather than a forecast.
- Every nonaligned occurrence endpoint fails with `NON_ALIGNED_OCCURRENCE_BOUNDARY`.
- Small instances match the independent exhaustive oracle on feasibility, every value in the four-stage objective tuple, the complete final optimum set, and membership of the returned schedule in that set.
- Every optimizable public tariff composition passes randomized reference-versus-lowering equivalence tests.
- Every produced schedule passes the independent verifier and reference cost recomputation.
- A solver or heuristic incumbent replaces the reference only when it strictly improves its complete declared objective tuple, and equal or worse incumbents remain diagnostic evidence.
- The off-peak proxy heuristic is deterministic under its locked environment, records its contract version, rank-calendar version, search status, and selection outcome, and falls back to the verified reference schedule when it has no incumbent.
- Deliberately corrupted schedules fail for the intended constraint.
- Reproducible single-worker deterministic-time runs are identical under the same locked environment and manifest.
- Optimal, best-found, pre-solve invalid-reference, internal model-contract-violation, unknown, and model-invalid statuses remain distinct in the API and UI.
- A tariff with an unsupported IR operator refuses optimization with an exact reason.

The portfolio-ready core checkpoint is reached after Milestone 4 if a local private account can run the frozen simulated fixture through import, replay, comparison, optimization, verification, and provenance without disposable authentication code.
The public static walkthrough is generated from those accepted results in Milestone 6 and never becomes a shared mutable account.
Implementation must continue through the remaining milestones.

### Milestone 5 - Production data and job path

Deliverables:

- Extend authorization and ownership checks to replay, comparison, scenario, job, result, export, and report resources and implement account deletion with session revocation.
- Implement client-prepared deletion intents, owner-bound single consumption, receipt-secret verification, and response-loss recovery before enabling account deletion.
- Implement idempotent scenario submission.
- Implement separate operation-idempotency and semantic-calculation identities with version-sensitive result uniqueness.
- Extend the Milestone 1 PostgreSQL job primitives to replay, comparison, scenario, report, retention, and deletion jobs with fixed scope modes, rescue, cancellation, retry exhaustion, and conditional terminal result publication.
- Implement owner-scoped and attempt-scoped artifact staging, owner-scoped unique successful results, and orphan sweeping.
- Implement account, import, and profile lifecycle generations and fence every ordinary dependent lease, heartbeat, retry, and finalizer against deletion.
- Implement the generation-authorized deletion job, persisted deletion phases, upload registration, quiescence barrier, final strong-consistency sweep, and separately durable deletion ledger.
- Implement external preparation before fencing, the least-privilege startup and periodic preparation reconciler, the fenced `DELETION_PENDING_LEDGER` control operation, `REQUESTED` and `COMPLETED` transitions, strictly proved `ABORTED`, sweep-exempt control state, restore reconciliation, and the atomic successful-job and `DELETED` transition.
- Implement object-store and database encryption configuration, raw-upload TTL, deletion, and backup-retention jobs.
- Implement immutable manifests and audit events.
- Add metrics and traces.

Acceptance gate:

- Worker-kill and stale-finalizer race tests recover without duplicate, overwritten, or stale successful results.
- Every crash point around staged artifacts and fenced database finalization leaves either one accepted result or a sweepable unpublished artifact.
- Reused idempotency keys behave correctly.
- A calculation-contract change creates a separately owned semantic result while an operation retry returns its original operation.
- Changing a current replay's entered bill total, unsupported-line-item tuple, or reconciliation policy creates a distinct semantic result without overwriting or reusing the older residual.
- The complete generated resource authorization matrix rejects cross-account and indirect object-ID access.
- Identical semantic calculation hashes in two accounts produce separately owned results and do not reveal, block, reuse, or delete each other's rows or artifacts.
- Retention and live-data deletion remove all scoped artifacts, while documented encrypted backup expiry remains truthful.
- Deletion races at every lease and publication boundary cannot recreate deleted data, and the receipt endpoint remains usable without an account session.
- Losing any deletion-intent or account-deletion response leaves the client able to retry safely or poll the stable precreated deletion identifier with the already held receipt secret.
- A failed preparation leaves the database unchanged, a failed `REQUESTED` transition leaves the account fenced and pollable, an acknowledged suppressive event cannot coexist with an active account, and recovery retries the same deletion event identity.
- Simulated primary loss after `PREPARED` and before `REQUESTED` leaves a verified quarantine hold that cannot be cleared from elapsed time or an old backup's active row.
- A deletion job remains runnable only at its exact `DELETING` generation, and completion waits until every older writer and upload is unable to create another artifact.
- A crash at every phase from `SWEEP` through atomic terminal finalization preserves the sweep-exempt deletion control state needed to finish.
- Abandoned and confirmed raw uploads both expire within the maximum TTL.
- Telemetry contains no interval or bill data.

### Milestone 6 - Complete consumer workflow

Deliverables:

- Finish the immutable static demo walkthrough and private-account onboarding and upload.
- Finish eligibility and bill-period inputs.
- Finish replay, residual, comparison, and source views.
- Finish EV and appliance editor.
- Finish reference, heuristic, optimal, best-found, invalid-reference, model-contract-status, heatmap, decomposition, and explanation views.
- Finish redacted report generation.
- Add accessibility and responsive behavior.

Acceptance gate:

- Browser tests complete every primary and degraded user journey.
- The public demo completes from landing page to redacted report using only content-addressed simulated static artifacts and issues no upload, mutation, job, or authenticated API request.
- Two simultaneous public-demo browser contexts cannot observe, overwrite, cancel, prolong, or otherwise affect one another because the server holds no visitor-specific demo state.
- At least four of five first-time users pass the frozen uncoached comprehension protocol.
- The redacted report contains no raw identifier or detailed interval history, and V1 exposes no option to include interval history.
- The redacted report contains no daily series, occurrence window, or exact reference or optimized load slot and contains only allowlisted aggregate fields.
- A non-rankable comparison displays exclusions without a winner or savings value.
- Optimal and best-found solver results use different visible language.
- Existing and historical-addition flexible loads show a correct reconstructed reference profile.

### Milestone 7 - Reliability, security, and operations

Deliverables:

- Add rate limiting and abuse controls.
- Add backup and restore documentation.
- Add database migration rollback tests where safe.
- Add dependency, container, static, and secret scans.
- Add operational dashboards and alerts.
- Add fault injection for database, storage, and worker failures.

Acceptance gate:

- Defined service-level indicators are observable.
- Failure modes produce safe user-visible states.
- Security checks pass without ignored critical findings.
- A restore drill and deployment rollback procedure are documented and exercised under `LOCAL_REPRODUCIBLE`.
- A real hosted validation is required before `HOSTED_VALIDATED` appears in documentation, and otherwise all hosted-operation claims remain explicitly withheld.
- The restore drill starts from a backup that predates deletion and retention expiry, reapplies the separately protected ledger, resolves or deliberately leaves quarantined every prepared event, proves expired or deleted live records are not reintroduced, and confirms that backups age out within the declared maximum.
- A missing or unverifiable ledger or an unresolved preparation makes the restore fail closed before network exposure.

### Milestone 8 - Frozen evaluation

Deliverables:

- Freeze the final demo, golden, optimizer, and performance run manifests without changing the earlier acceptance charters.
- Run parser and billing correctness reports.
- Run optimizer-oracle comparisons.
- Run performance and crash-recovery benchmarks.
- Conduct the user-comprehension study.
- Publish raw machine-readable results and generated charts.

Acceptance gate:

- All claims in the README map to a committed or reproducible artifact.
- Every bill golden has a source-linked derivation artifact that imports no production calculation code.
- Every advertised optimizable tariff has a passing solver-lowering equivalence report.
- Every displayed tariff ranking has a complete eligibility and comparison-component coverage artifact.
- Result tables include uncertainty or repeated-run variation where applicable.
- Negative cases and limitations are present.
- No result depends on private source data.
- The user-study report includes every participant and satisfies the frozen four-of-five threshold.

### Milestone 9 - Portfolio release

Deliverables:

- Finish public README, architecture, methodology, security, tariff-authoring, and limitations documents.
- Produce the demo video and example report.
- Verify clean-checkout setup on a second environment or fresh container.
- Audit licenses, secrets, generated artifacts, stale claims, and repository history.
- Prepare truthful resume bullets and interview talking points outside the source tree if they are personal.

Acceptance gate:

- A clean checkout reproduces the main demo and verification suite.
- All required source files are tracked.
- The worktree is clean.
- Public claims match measured scope.
- Publication waits for explicit authorization.

### Milestone 10 - Extensions after release

Eligible extensions are:

- Another California utility.
- CCA generation components.
- Solar and battery modeling.
- Green Button Connect My Data authorization.
- Calendar or Home Assistant export.
- Carefully designed device control.

Each extension requires a new source audit, threat-model update, correctness suite, and separate milestone gate.

## 25. Commit discipline

Every milestone must end in a focused commit after its acceptance gate passes.
Large milestones must use submilestone commits for independent working slices such as parser, domain, API, UI, and tests.
Do not combine unrelated cleanup with a milestone commit.
Do not commit generated private data, secrets, raw bills, local database files, or benchmark caches.
Do not push or publish without explicit authorization.

## 26. Kill gates and pivots

The project must stop and revise its central claim if any of the following occur:

- Filed tariff rules cannot be implemented without material undocumented assumptions.
- Golden calculations cannot be reconciled with the encoded rules.
- The supported-charge matrix is too narrow to produce a useful comparison.
- Fewer than three tariff families can pass the common July 2026 eligibility and comparable-component admission gate.
- Green Button files cannot be normalized safely under the supported contract.
- A genuine provider-produced PG&E CSV fixture cannot be obtained, sanitized, or interpreted without undocumented assumptions, in which case CSV must be removed from V1 rather than simulated.
- The optimizer needs a divergent billing approximation that cannot be proven equivalent.
- Existing flexible load cannot be separated by an explicit reference schedule without negative background energy or another hidden inference.
- A ranked comparison would depend on an unsupported tariff-specific charge, credit, or eligibility fact.
- Users interpret the result as an official bill after the explanation design is tested.
- Users interpret a historical addition as a future forecast after the explanation design is tested.
- Sensitive source data is required for the public demonstration.

A valid pivot is a local-only tariff replay tool with simulated profiles and no account system.
An invalid pivot is hiding unsupported charges or presenting partial calculations as exact bills.

## 27. Risks and mitigations

### Tariff complexity

Keep a strict coverage matrix and one locked utility scope.
Treat every unimplemented rider or adjustment as unsupported and block ranking whenever it can change the comparison ordering.

### Time semantics

Use UTC intervals, IANA local classification, half-open ranges, and dedicated DST fixtures.
Reject intervals that require an undocumented energy split.

### Money precision

Use exact decimal or integer representations and source-specific rounding tests.
Never compare bill values using an arbitrary floating-point epsilon.

### Optimizer realism

Require a valid reference schedule, prove exact background reconstruction, move only declared flexible loads, and expose every constraint.
Compare the optimal or best-found result with the reconstructed reference and simple heuristic schedules using status-accurate language.

### Private data

Avoid utility credentials, minimize retention, strip identifiers, sanitize telemetry, and provide deletion.
Encrypt normalized data and backups at rest and state the backup-deletion delay explicitly.

### Source drift

Lock every tariff component version, stable regulator identifier, retrievable source, and hash.
New rates require a new immutable version and golden review.

### Scope growth

Do not add solar, batteries, multiple utilities, or device control before the portfolio release gate.

## 28. Authoritative references

- [Green Button Connect My Data](https://www.greenbuttondata.org/cmd.html)
- [Green Button developer model](https://green-button.github.io/developers/)
- [PG&E usage and Green Button access](https://www.pge.com/en/save-energy-and-money/energy-usage-and-tips/understand-my-usage.html)
- [PG&E current and historical electric tariffs](https://www.pge.com/tariffs/en/rate-information/electric-rates.html)
- [PG&E residential pricing guide](https://www.pge.com/assets/pge/docs/account/rate-plans/residential-electric-rate-plan-pricing.pdf)
- [PG&E E-1 filed schedule](https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_E-1.pdf)
- [PG&E E-TOU-C filed schedule](https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_E-TOU-C.pdf)
- [PG&E E-TOU-D filed schedule](https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_E-TOU-D.pdf)
- [PG&E E-ELEC filed schedule](https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_E-ELEC.pdf)
- [PG&E EV2-A filed schedule](https://www.pge.com/tariffs/assets/pdf/tariffbook/ELEC_SCHEDS_EV2%20(Sch).pdf)
- [OpenEI Utility Rate Database API](https://apps.openei.org/services/doc/rest/util_rates/)
- [NREL End-Use Load Profiles](https://www.nrel.gov/buildings/end-use-load-profiles)
- [NREL REopt](https://www.nrel.gov/reopt/curriculum/what-is-reopt)
- [Google OR-Tools CP-SAT guide](https://developers.google.com/optimization/cp/cp_solver)
- [OR-Tools CP-SAT response contract](https://github.com/google/or-tools/blob/stable/ortools/sat/cp_model.proto)
- [OR-Tools solver parameter contract](https://github.com/google/or-tools/blob/stable/ortools/sat/sat_parameters.proto)

## 29. Representative official role references

- [TikTok Backend Software Engineer, Creator Strategy](https://lifeattiktok.com/search/7672976491146004741)
- [Uniswap Software Engineer, Early Career](https://jobs.ashbyhq.com/uniswap/fb4d4137-f003-4669-beb7-2a5caca88012/application?embed=true)
- [Sentry Software Engineer, New Grad](https://jobs.ashbyhq.com/sentry/5c3196c7-f3d6-4dba-9c41-c886df4b2421/application?embed=true)
- [Cohesity Software Engineer, New Grad](https://cohesity.wd5.myworkdayjobs.com/Cohesity_Careers/job/Santa-Clara-CA---USA-Office/Software-Engineer_R01282)
- [Salesforce Software Engineer, College Grad](https://salesforce.wd12.myworkdayjobs.com/External_Career_Site/job/California---San-Francisco/Software-Engineering-AMTS--College-Grad-_JR355250-1)
- [Samsara Software Engineer I, New Grad](https://www.samsara.com/company/careers/roles/8097345?gh_jid=8097345)
- [Lightfield Early Career Infrastructure Software Engineer](https://jobs.ashbyhq.com/Lightfield/9a7ef2f9-577a-4242-b884-719e3cdf4420/application?embed=true)
- [Amazon EFA Network Software Engineer I](https://amazon.jobs/en/jobs/10481932/efa-network-software-engineer-i-annapurna-labs)
- [DoorDash Software Engineer I](https://job-boards.greenhouse.io/doordashusa/jobs/7263610)
- [Capital One Associate Software Engineer, New Grad](https://capitalone.wd12.myworkdayjobs.com/Capital_One/job/Toronto-ON/Associate--Software-Engineer--New-Grad-Card-Expansion_R247320)
- [Cerebras Software Engineer, New Grad](https://jobs.ashbyhq.com/cerebras/99c289fa-8fc6-49f7-b7e8-78ac4e9d99ac/application)
- [Notion Software Engineer, Early Career](https://jobs.ashbyhq.com/notion/297b4ece-765f-4eea-b1b8-46057cb6501f/application)
- [Intuit Software Engineer I](https://jobs.intuit.com/job/mountain-view/software-engineer-1/27595/87369448720)
- [Sigma Computing Software Engineer, New Grad](https://job-boards.greenhouse.io/sigmacomputing/jobs/7690411003)
- [Deliveroo Software Engineer, New Grad](https://jobs.ashbyhq.com/deliveroo/2b69d23b-30b5-46c8-95e8-48258ec05636/application)
- [Anduril Early Career Software Engineer](https://boards.greenhouse.io/andurilindustries/jobs/4802146007)

Role pages can close after the planning snapshot.
The implementation requirements remain tied to the repeated responsibilities summarized above, not to the continued availability of one posting.

## 30. Final positioning

The strongest interview story is:

> I built an executable tariff system that can defend every supported calculation, refuse non-comparable plan rankings, reconstruct the load that may be shifted, and prove that a lower-cost returned schedule is feasible under the user's declared constraints.

That story is credible only if the repository contains the golden calculations, residual policy, independent verifier, failure tests, and measured end-to-end product evidence described in this plan.
