# Validation methodology

## Evidence states

RateReplay tracks implementation progress separately from milestone acceptance.
`IMPLEMENTED_PENDING_GATE` means the software, automated tests, documentation, and locally executable qualification are complete while an acceptance prerequisite remains open.
`HUMAN_VALIDATION_DEFERRED` means the frozen genuine five-person study has not occurred.
`ACCEPTED` is used only when every gate in the milestone has passed.

Synthetic personas are protocol-development inputs only.
They never count as participants and cannot satisfy a human gate.

Evidence levels are also explicit.
`LOCAL_REPRODUCIBLE` covers results reproduced on the named local host or pinned local container topology.
`HOSTED_VALIDATED` is withheld because no authorized hosted qualification has occurred.

## Correctness and performance separation

Correctness qualifications establish exact parsing, billing, eligibility, comparison, scenario, verification, persistence, and recovery behavior.
Performance benchmarks run only after the corresponding correctness gate passes.
A fast incorrect result cannot satisfy a performance gate.

Frozen acceptance charters define workloads, hashes, topology, repetitions, cache state, thresholds, and aggregation before measurement.
Cold and warm measurements remain separate.
Every nondeterministic or load-sensitive published series retains at least ten measured repetitions and reports p50, p95, p99, and variation.

Failed and high-variance experiments remain committed.
A corrected charter or follow-up may narrow the defect and produce a new result, but it does not delete or relabel the earlier result.

## Source and fixture validation

`data/sources.lock.json` and `tariffs/sources.lock.json` bind external material to exact hashes, stable identifiers, effective ranges, redistribution policy, and local paths.
Green Button uses the locked ESPI schema plus an independently sourced conforming fixture.
The PG&E CSV adapter uses a permission-reviewed provider-produced structural fixture and a separately documented redaction contract.

Parser positive cases cover relationship resolution, interval semantics, units, direction, time parameters, and exact watt-hour normalization.
Negative cases cover XML entities, expansion attacks, broken relationships, wrong commodities, unknown formats, unknown adapter values, nonintegral energy, malformed time, and resource bounds.

## Billing and comparison validation

Golden fixtures are written before or independently from the production evaluator.
The final Milestone 8 derivation runner imports no production calculation package and matches all five admitted tariff results.
Boundary suites cover effective dates, time periods, eligibility thresholds, minimum bills, credits, and rounding.

Mutation tests make one difference-making rule wrong at a time and require the intended mismatch.
Comparison tests require complete eligibility and active-component coverage for every displayed ranking.
Unknown eligibility and an unclassified active component suppress the winner and supported-charge difference.

Reproduce the billing and comparison evidence with:

```sh
make qualification-m2
make qualification-m3
```

The machine-readable outputs are under `evidence/correctness` and the human-readable interpretations are under `docs/evidence`.

## Optimizer validation

The optimizer lowers only operators admitted by the canonical tariff intermediate representation.
Randomized cross-backend tests compare direct reference replay with the lowered objective for every optimization-admitted tariff.

The frozen small-instance oracle independently enumerates every feasible schedule without importing solver constraint construction.
It verifies membership in the complete optimum set and agreement on all four objective stages.
A seeded one-watt-hour corruption must fail with `VERIFIER_ENERGY_CONSERVATION_FAILED`, while the unmodified controls pass.

Reproduce the portfolio optimizer gate with:

```sh
make qualification-m4
```

Run the broader final oracle and derivation checks with:

```sh
make qualification-m8-correctness
```

## Durable and security validation

Unit and property tests cover operation identity, semantic identity, generation fences, leases, retries, stale finalizers, authorization, deletion ordering, key rotation, migration, retention, and restore suppression.
PostgreSQL and S3-compatible integration suites exercise migrations, owner isolation, durable jobs, encrypted objects, backup verification, and deletion against real services.

Browser tests use the production Vite build.
They cover the complete static demo, manifest corruption, two independent browser contexts, keyboard and narrow-viewport behavior, private upload through deletion, session expiry, blocked comparisons, and unsuccessful solver states.

Bandit, the fixed-pattern secret scan, Python and JavaScript dependency audits, release configuration validation, and pinned Trivy image scans cover the repository and local release topology.
No ignored critical finding supports a passing result.

## Reliability and recovery validation

Durable worker qualifications terminate the active worker with SIGKILL only after the job reports `RUNNING`.
The gate requires lease expiry, a second attempt, worker restart, one successful result, and zero duplicate successful results.

The restore drill begins from a real encrypted PostgreSQL custom dump and object manifest that predate deletion and retention expiry.
It restores into fresh quarantine services, verifies the encrypted ledger, reapplies suppression, exercises failures after deletion checkpoints, and withholds exposure for a missing ledger, tampering, an unresolved preparation, or a missing historical key.

Run the local operational gates with:

```sh
make qualification-m7-restore
make qualification-m7-deployment
```

These commands are destructive only to their named isolated qualification projects and generated test secrets.
They do not establish hosted behavior.

## Performance evaluation

The frozen Milestone 8 manifest is `benchmarks/manifests/m8-evaluation-v1.json`.
It locks public profiles, independent golden inputs, candidate counts, load counts, solver settings, API concurrency, crash injection, thresholds, and the named Apple M5 host.

The evaluation records import time and peak memory, replay, comparison, optimization, report generation, release API latency, worker recovery, duplicates, and database and object-store size.
The deterministic summary and views are checked with:

```sh
make m8-manifest-check
make m8-correctness-check
make m8-performance-check
make m8-evaluation-check
```

The complete `make qualification-m8` command intentionally remains nonzero until genuine study evidence passes.
This failure is the expected deferred gate, not an automated-evaluation failure.

## Clean-checkout reproducibility

`make clean-checkout-check` exports the tracked tree, installs from locks, runs the normal verification suite, regenerates Milestones 3 and 4, and exercises the authenticated portfolio-core API journey.

Milestone 9 also exports an exact remote-confirmed commit into a disposable checkout and runs the workflow in a pinned Ubuntu 24.04.4 x86_64 Playwright image.
It downloads exact Python, Node, uv, Compose, Make, and ripgrep tools and verifies their SHA-256 values before use.
The committed result is `evidence/reproducibility/m9-clean-container.json`.

Validate the recorded artifact with:

```sh
make m9-clean-container-check
```

Generating a new result requires a clean worktree whose `HEAD` exactly matches `origin/main`:

```sh
make qualification-m9-clean-container
```

GitHub Actions is not counted as a second environment because the current jobs are blocked before execution by the repository owner's account billing state.
No passing CI claim is made.

## Public result map

| Claim class                    | Primary evidence                                              | Reproduction                                                           |
| ------------------------------ | ------------------------------------------------------------- | ---------------------------------------------------------------------- |
| Public demo integrity          | `artifacts/demo/manifest.v1.json`                             | `make demo-artifacts-check`                                            |
| Parser correctness             | `evidence/evaluation/m8-parser-correctness.json`              | `make qualification-m8-correctness`                                    |
| Five-tariff golden agreement   | `evidence/correctness/m8-independent-golden-derivations.json` | `make qualification-m8-correctness`                                    |
| Ranking coverage               | `evidence/evaluation/m8-comparison-coverage.json`             | `make qualification-m8-correctness`                                    |
| Optimizer and oracle agreement | `evidence/evaluation/m8-optimizer-oracle.json`                | `make qualification-m8-correctness`                                    |
| Local performance              | `evidence/evaluation/m8-performance.json`                     | `make m8-performance-check`                                            |
| Worker crash recovery          | `evidence/evaluation/m8-crash-recovery.json`                  | `make m8-evaluation-check`                                             |
| Restore and rollback           | `evidence/reliability`                                        | `make qualification-m7-restore` and `make qualification-m7-deployment` |
| Second Linux environment       | `evidence/reproducibility/m9-clean-container.json`            | `make m9-clean-container-check`                                        |
| Genuine comprehension          | Not yet present                                               | `make qualification-m6-study`                                          |

## Human validation

The frozen protocol requires exactly five first-time participants who have not read implementation documents and receive no coaching.
Each participant completes the five-step demo and answers five fixed comprehension questions.
At least four participants must complete every step independently and answer every question correctly.

The current genuine participant count is zero.
See [user-study-handoff.md](user-study-handoff.md) for the exact deferred procedure and downstream checks.
