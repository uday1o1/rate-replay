# Milestone 4 evidence

State: `ACCEPTED` after the commands in this document and the clean-checkout gate pass.

## Portfolio-core user path

`apps/api/tests/test_portfolio_core_api.py` creates a production-authenticated private account through the public API.
It installs the content-locked simulated July profile, creates the immutable E-1 replay, preserves the entered unsupported line and signed residual, ranks all five admitted tariffs, submits a complete historical-addition reference schedule, runs the exact and heuristic solvers, and reads the independent verification and provenance hashes.
The browser suite exercises the matching user-visible import, replay, comparison, pre-solve explanation, optimal, best-found, and unsuccessful-model states.
No disposable authentication bypass or utility credential is used.

## Correctness qualification

Run:

```sh
make qualification-m4
```

The locked result is `evidence/correctness/m4-optimizer-qualification.json`.
The qualification proves exact measured-profile reconstruction, complete reference validation, a four-stage optimal exact result, deterministic repetition, explicit heuristic non-optimality, and verification hashes for the frozen historical-addition workload.
The independent three-slot oracle enumerates every feasible 70 Wh schedule, obtains the complete final optimum set `[[70, 0, 0]]`, and confirms that the returned schedule belongs to it with the same four-value objective tuple.
A one-watt-hour seeded corruption fails with `VERIFIER_ENERGY_CONSERVATION_FAILED` while the nearby unmodified result passes.
Randomized reference-versus-lowering tests cover all five optimization-admitted public tariff compositions.

## Performance history

The original `performance-acceptance-v2` workload is preserved as a failure in `evidence/performance/m4-performance-v2-failed.json`.
Its `SHIFT_EXISTING` reference was not present in the measured profile and correctly failed before solver construction with `NEGATIVE_FIXED_BACKGROUND`.
`performance-acceptance-v3` changes only the workload mode to the already admitted `HISTORICAL_ADDITION` contract and leaves every numeric threshold unchanged.

Run:

```sh
make benchmark-m4-optimization
```

The ten measured repetitions in `evidence/performance/m4-optimization-performance-v3.json` produced a one-load p95 of 656.535459 ms and a five-load p95 of 2,508.453792 ms on the named Apple M5 development machine.
The thresholds are 10,000 ms and 30,000 ms respectively.
Every result hash was deterministic within its workload and every selected schedule was independently verified.
Scenario-worker recovery remains explicitly scheduled for the durable scenario-worker implementation in Milestone 5 and is not claimed as accepted here.

## Verification commands

```sh
make check
make qualification-m4
make integration-m4
make clean-checkout-check
```

Milestone 4 is accepted only after every command passes and origin contains the verified milestone commit.
