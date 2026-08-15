# RateReplay

RateReplay is an auditable electricity bill-replay and flexible-load scheduling application for reviewing how a source-locked historical tariff applies to interval energy data.
The repository is designed for reviewers and developers who want a reproducible example of exact billing arithmetic, fail-closed tariff comparison, verified scheduling, durable jobs, and privacy-aware deletion and restore behavior.

The public walkthrough uses only a simulated July 2026 profile and immutable generated artifacts.
It does not require a utility account, utility credentials, a shared demo account, or anonymous API writes.

The local V1 implementation and automated qualifications are complete through the portfolio-release work.
Formal acceptance remains deferred at Milestone 6 because the required five genuine first-time participant sessions have not occurred.
Synthetic persona results are development-only and count as zero genuine participants.

## What the demo shows

The simulated walkthrough covers five connected decisions:

1. Review a normalized 15-minute interval profile and its quality findings.
2. Replay supported historical charges while keeping explicit unsupported lines and the signed unexplained residual visible.
3. Compare E-1, E-TOU-C, E-TOU-D, E-ELEC, and EV2-A only after eligibility and difference-making component coverage pass.
4. Schedule a historical flexible-load counterfactual against a complete unchanged reference.
5. Inspect exact solver status, independent verification, provenance, and a redacted aggregate report.

The demo labels historical additions as past-period counterfactuals, not future forecasts.
It never copies an unsupported current-bill item or residual into an alternative plan.
It displays a winner or supported-charge difference only when every comparison gate is complete.

## Run the public demo

### Requirements

- Python 3.12.13.
- Node 24.16.0.
- uv 0.11.23 through the supported 0.11 release line.
- Corepack with pnpm 11.21.0.
- Docker Engine and Docker Compose 5.4.0 for the complete verification and service workflows.

Install the locked dependencies and build the static application:

```sh
make bootstrap
corepack pnpm build
```

Start the production Vite preview:

```sh
corepack pnpm --filter @ratereplay/web exec vite preview \
  --host 127.0.0.1 \
  --port 4173
```

Open `http://127.0.0.1:4173/#demo` in a browser.
The first screen should identify the profile as simulated, report 2,976 readings and 750,000 Wh, and show the import as ready for calculation.
Continue through the redacted report to exercise the complete static workflow.

The browser verifies the build-locked manifest, allowlist, and every content-addressed artifact before displaying results.
Regenerate and check those artifacts with:

```sh
make demo-artifacts
make demo-artifacts-check
```

The discoverable report view is [docs/results/example-redacted-report.json](docs/results/example-redacted-report.json).
The immutable release manifest is [artifacts/demo/manifest.v1.json](artifacts/demo/manifest.v1.json).

## Reproduce the calculation path

Compile and replay the source-locked E-1 definition, then run the five-plan comparison and optimizer qualifications:

```sh
uv run ratereplay-tariff compile-e1
uv run ratereplay-tariff replay-e1 tariffs/examples/e1-replay-input.json
make qualification-m3
make qualification-m4
```

The frozen 750 kWh profile produces supported subtotals of $277.28 for E-1, $302.53 for E-TOU-C, $260.21 for E-TOU-D, $302.78 for E-ELEC, and $268.90 for EV2-A.
Under only that locked comparable-component contract, E-TOU-D is $17.07 below E-1.
These values are historical supported-charge results for the admitted account and service window, not complete utility bills or future savings forecasts.

## Measured local results

The final frozen evaluation ran on an Apple M5 developer laptop with 10 logical CPUs, 24 GiB of memory, arm64 architecture, and macOS 26.5.
Each row links to the committed raw measurements and preserves cold and warm series separately.

| Measurement                                      | Repetitions |           p95 | Threshold | Result |
| ------------------------------------------------ | ----------: | ------------: | --------: | ------ |
| One-year warm import variance follow-up          |          10 |    427.883 ms | 15,000 ms | Pass   |
| Five-load warm optimization                      |          10 |  2,568.952 ms | 30,000 ms | Pass   |
| Warm cached comparison GET at concurrency 8      |          30 |     64.432 ms |  1,000 ms | Pass   |
| Warm scenario GET at concurrency 8               |          30 |    333.020 ms |  1,000 ms | Pass   |
| Import recovery after worker SIGKILL             |          10 | 23,589.761 ms | 30,000 ms | Pass   |
| Five-load scenario recovery after worker SIGKILL |          10 | 27,277.055 ms | 30,000 ms | Pass   |

The complete table, variation fields, and evidence paths are in [docs/results/m8-performance.csv](docs/results/m8-performance.csv) and [evidence/evaluation/m8-performance.json](evidence/evaluation/m8-performance.json).
The original failed `SHIFT_EXISTING` performance experiment remains preserved in [evidence/performance/m4-performance-v2-failed.json](evidence/performance/m4-performance-v2-failed.json) and was not rewritten as passing.

## Architecture and evidence

The system uses a React browser application, a FastAPI service, durable Python workers, PostgreSQL, primary and backup S3-compatible storage, and a separately protected encrypted deletion ledger.
The static public demo bypasses the private service path and loads only content-addressed simulated artifacts.
The reference release topology exposes only Caddy and keeps API, worker, database, metrics, and storage services on internal networks.

- [Architecture overview](docs/architecture.md)
- [Billing semantics](docs/billing-semantics.md)
- [Tariff authoring](docs/tariff-authoring.md)
- [Validation methodology](docs/validation-methodology.md)
- [Security and privacy](docs/security-and-privacy.md)
- [Limitations](docs/limitations.md)
- [Evidence index](docs/evidence/README.md)

Run the normal repository verification suite with:

```sh
make check
make dependency-audit
```

Run the native clean-checkout workflow with `make clean-checkout-check`.
The committed second-environment evidence records the same workflow on pinned Ubuntu 24.04.4 x86_64 in [evidence/reproducibility/m9-clean-container.json](evidence/reproducibility/m9-clean-container.json).

## Scope and release status

RateReplay currently admits only the locked July 2026 PG&E bundled Tier 3 EV account predicate and the five named tariff versions.
It is not an official utility bill, does not forecast future prices or usage, and does not claim hosted durability, hosted encryption, production ACME operation, multi-host scaling, or genuine user comprehension.
The repository evidence level is `LOCAL_REPRODUCIBLE`.

Milestones 0 through 5 are `ACCEPTED`.
Milestone 6 is `HUMAN_VALIDATION_DEFERRED`.
Milestones 7 through 9 remain `IMPLEMENTED_PENDING_GATE` until the genuine study and sequential acceptance checks pass.
Publication, deployment, and release creation require separate explicit authorization.

## License

RateReplay source is available under the [Apache License 2.0](LICENSE).
Third-party source and fixture notices remain under their recorded upstream terms in `third_party` and the source-lock metadata.
