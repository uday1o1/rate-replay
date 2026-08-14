# RateReplay

RateReplay is an auditable household electricity bill-replay and flexible-load scheduling product under active implementation.
It is not an official utility bill and currently admits only source-complete service windows and account classes documented in this repository.

The authoritative implementation scope and acceptance gates are in [BUILD_PLAN.md](BUILD_PLAN.md).
Accepted milestone evidence is indexed under [docs/evidence](docs/evidence).

## Current supported workflow

The currently admitted calculations are historical replay and unchanged-usage comparison across July 2026 PG&E E-1, E-TOU-C, E-TOU-D, E-ELEC, and EV2-A for the locked bundled Tier 3 EV account class.
Current-plan replay emits auditable supported charge lines, exact provenance, optional user-entered unsupported lines, and a signed unexplained residual.
Alternative-plan results contain eligibility, comparable-component coverage, supported subtotals, and filed-source provenance without propagating the current bill's unsupported lines or residual.
RateReplay ranks plans and displays a supported-charge savings value only when every candidate is eligible and every difference-making component is covered.
It does not yet optimize flexible loads, forecast a future bill, claim utility-bill equivalence, or generalize these results beyond the locked account and service window.

Compile the source-locked tariff and reproduce the frozen complete-bill request with:

```sh
uv run ratereplay-tariff compile-e1
uv run ratereplay-tariff replay-e1 tariffs/examples/e1-replay-input.json
make qualification-m3
```

The authoring, numeric, eligibility, reconciliation, and admission rules are documented in [docs/tariff-methodology.md](docs/tariff-methodology.md).

## Foundation workflow

Install Python 3.12.13, Node 24.16.0, uv 0.11.23 or newer compatible 0.11 release, Corepack, and Docker Compose 5.4.0.
Then run:

```sh
make bootstrap
make check
make qualification-m3
```

The frozen 750 kWh profile produces supported subtotals of $277.28 for E-1, $302.53 for E-TOU-C, $260.21 for E-TOU-D, $302.78 for E-ELEC, and $268.90 for EV2-A.
Under only that locked comparable-cost contract, E-TOU-D is the lowest supported subtotal and is $17.07 below E-1.

The built-in public demo remains unavailable until its production artifacts pass the Milestone 6 release build.
