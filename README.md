# RateReplay

RateReplay is an auditable household electricity bill-replay and flexible-load scheduling product under active implementation.
It is not an official utility bill and currently admits only source-complete service windows and account classes documented in this repository.

The authoritative implementation scope and acceptance gates are in [BUILD_PLAN.md](BUILD_PLAN.md).
Accepted milestone evidence is indexed under [docs/evidence](docs/evidence).

## Current supported workflow

The currently admitted calculations are historical replay, unchanged-usage comparison, and verified flexible-load scheduling across July 2026 PG&E E-1, E-TOU-C, E-TOU-D, E-ELEC, and EV2-A for the locked bundled Tier 3 EV account class.
Current-plan replay emits auditable supported charge lines, exact provenance, optional user-entered unsupported lines, and a signed unexplained residual.
Alternative-plan results contain eligibility, comparable-component coverage, supported subtotals, and filed-source provenance without propagating the current bill's unsupported lines or residual.
RateReplay ranks plans and displays a supported-charge savings value only when every candidate is eligible and every difference-making component is covered.
Historical additions are labeled counterfactual rather than forecast, and every published schedule passes a separate verifier before it can replace the unchanged reference.
Exact results distinguish optimal, best-found, and unsuccessful solver statuses, while the off-peak heuristic explicitly makes no bill-optimality claim.
RateReplay does not forecast a future bill, claim utility-bill equivalence, or generalize these results beyond the locked account and service window.

Compile the source-locked tariff and reproduce the frozen complete-bill request with:

```sh
uv run ratereplay-tariff compile-e1
uv run ratereplay-tariff replay-e1 tariffs/examples/e1-replay-input.json
make qualification-m3
make qualification-m4
```

The authoring, numeric, eligibility, reconciliation, and admission rules are documented in [docs/tariff-methodology.md](docs/tariff-methodology.md).

## Foundation workflow

Install Python 3.12.13, Node 24.16.0, uv 0.11.23 or newer compatible 0.11 release, Corepack, and Docker Compose 5.4.0.
Then run:

```sh
make bootstrap
make check
make qualification-m4
```

The frozen 750 kWh profile produces supported subtotals of $277.28 for E-1, $302.53 for E-TOU-C, $260.21 for E-TOU-D, $302.78 for E-ELEC, and $268.90 for EV2-A.
Under only that locked comparable-cost contract, E-TOU-D is the lowest supported subtotal and is $17.07 below E-1.
The frozen historical-addition workload measured a one-load optimization p95 of 656.535 ms against a 10,000 ms threshold and a five-load p95 of 2,508.454 ms against a 30,000 ms threshold on the named Apple M5 development machine.
The earlier performance-v2 `SHIFT_EXISTING` workload failed exact decomposition with `NEGATIVE_FIXED_BACKGROUND`; that result remains preserved and was not relabeled as passing.

The portfolio-ready core can be reproduced from a clean tree with:

```sh
make clean-checkout-check
```

That gate installs dependencies from locks, runs the full repository verification suite, regenerates the accepted M3 and M4 qualifications, and exercises the production-authenticated API journey from simulated import through verified optimization.

The built-in public demo remains unavailable until its production artifacts pass the Milestone 6 release build.

## Data retention and deletion

Confirmed raw uploads enter immediate deletion when normalization no longer needs them, and every raw upload has a fixed 24-hour maximum lifetime.
Account, import, and profile deletion remove their scoped live database rows, object-store data, queued work, and generated artifacts through generation-fenced workflows.
Account deletion also revokes sessions and safely subsumes any child deletion already in progress while preserving its receipt status.
Deleted data may remain only in separately encrypted backups until the backup reaches its fixed maximum age of 30 days.
Individual backups are not rewritten after a deletion.
Every restore remains quarantined until the separately protected deletion ledger is verified and suppressive deletion records have been reapplied.
The complete backup, quarantine, and exposure-gate procedure is documented in [docs/operations/backup-restore.md](docs/operations/backup-restore.md).
The repository currently claims only `LOCAL_REPRODUCIBLE`; hosted encryption, restore, and retention behavior remains unclaimed until an authorized hosted qualification passes.
