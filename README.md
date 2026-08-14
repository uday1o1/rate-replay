# RateReplay

RateReplay is an auditable household electricity bill-replay and flexible-load scheduling product under active implementation.
It is not an official utility bill and currently admits only source-complete service windows and account classes documented in this repository.

The authoritative implementation scope and acceptance gates are in [BUILD_PLAN.md](BUILD_PLAN.md).
Accepted milestone evidence is indexed under [docs/evidence](docs/evidence).

## Current supported workflow

The currently admitted calculation is historical PG&E E-1 replay for the locked July 2026 account class and service window.
It emits auditable supported charge lines, exact provenance, optional user-entered unsupported lines, and a signed unexplained residual.
It does not compare plans, optimize flexible loads, forecast a future bill, or claim savings.

Compile the source-locked tariff and reproduce the frozen complete-bill request with:

```sh
uv run ratereplay-tariff compile-e1
uv run ratereplay-tariff replay-e1 tariffs/examples/e1-replay-input.json
```

The authoring, numeric, eligibility, reconciliation, and admission rules are documented in [docs/tariff-methodology.md](docs/tariff-methodology.md).

## Foundation workflow

Install Python 3.12.13, Node 24.16.0, uv 0.11.23 or newer compatible 0.11 release, Corepack, and Docker Compose 5.4.0.
Then run:

```sh
make bootstrap
make check
make qualification-m2
```

The built-in public demo remains unavailable until its production artifacts pass the Milestone 6 release build.
