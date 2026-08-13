# RateReplay

RateReplay is an auditable household electricity bill-replay and flexible-load scheduling product under active implementation.
It is not an official utility bill and currently admits only source-complete service windows and account classes documented in this repository.

The authoritative implementation scope and acceptance gates are in [BUILD_PLAN.md](BUILD_PLAN.md).
Milestone evidence is added under `docs/evidence` only after its gate passes.

## Foundation workflow

Install Python 3.12.13, Node 24.16.0, uv 0.11.23 or newer compatible 0.11 release, Corepack, and Docker Compose 5.4.0.
Then run:

```sh
make bootstrap
make check
```

The built-in public demo remains unavailable until its production artifacts pass the Milestone 6 release build.
