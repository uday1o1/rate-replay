# Limitations

## Tariff and account scope

RateReplay V1 admits only July 2026 PG&E E-1, E-TOU-C, E-TOU-D, E-ELEC, and EV2-A for the locked bundled Tier 3 EV account predicate.
The admitted service window is `[2026-07-01, 2026-08-01)`.
Results do not generalize to another month, account class, baseline territory, service mode, utility, CCA, or tariff revision.

Eligibility facts must be supplied explicitly.
Missing or unknown facts block ranking rather than being inferred.

## Bill scope

RateReplay calculates only the supported components declared in the candidate matrix.
It is not an official utility bill and does not claim complete utility-bill equivalence.
Taxes, local charges, adjustments, arrears, deposits, corrections, and other unsupported items can remain outside the calculated subtotal.

An entered current-bill total may include explicit user-entered unsupported lines and a signed unexplained residual.
Those values remain visible only in current-bill reconciliation and are excluded from alternative-plan results.

A supported-charge difference is not a complete-bill savings forecast.
It is valid only under the locked historical account, profile, service window, and component-coverage contract.

## Time and forecast scope

V1 performs historical replay.
It does not predict future rates, future usage, future weather, future eligibility, or future savings.
`HISTORICAL_ADDITION` is a counterfactual on admitted past timestamps.

The system rejects intervals that cross a tariff boundary without exact subinterval readings.
It does not interpolate, split, or estimate energy across a missing boundary.

## Data input scope

Green Button support is limited to the locked relationship and ReadingType contract.
The PG&E CSV adapter is limited to the reviewed provider-produced structure, units, timezone, and DST rules.
Unknown columns, formats, commodities, directions, units, time semantics, or nonintegral watt-hour conversions fail closed.

The public demo uses a simulated NREL-derived profile.
No real customer bill or interval file is included in public evidence.

## Optimization scope

V1 models flexible loads through the declared execution types, timing windows, energy requirements, reference schedules, and aggregate electrical constraints.
It does not model device control, uncertain availability, battery state of charge, solar generation, demand response, degradation, thermal dynamics, or user comfort.

Every load requires a complete user-supplied reference schedule.
RateReplay does not infer the baseline from observed household behavior.

The exact solver's `OPTIMAL` label applies only to the four declared objectives and the encoded model.
It does not prove that the model captures every real-world preference or utility rule.
The off-peak heuristic carries no optimality claim.

## Performance scope

Published core measurements were collected on the manifest-locked Apple M5 development laptop.
Release-topology measurements use the named local Docker environment.
Synthetic engineering workloads support reproducibility and scaling observations, not customer-scale or hosted-service capacity claims.

The repository does not claim multi-host scaling, geographic availability, production throughput, service-level objectives, or performance on different hardware.
Cold and warm series differ and must not be substituted for one another.

## Deployment scope

The implemented evidence level is `LOCAL_REPRODUCIBLE`.
Hosted operation has not validated managed secret delivery, hosted storage encryption, hosted durability, public ACME TLS, public DNS, hosted backup retention, hosted rollback, or hosted restore.

The V1 rate limiter is process-local and assumes one API process.
Multiple API processes require a shared limiter and a separate qualification.

The filesystem deletion ledger cannot detect atomic replay of the complete stream and all signed control files together.
A hosted claim requires conditional append, object lock, or an external witness.

The local restore topology does not archive PostgreSQL WAL.
A deployment that adds WAL archiving must qualify verified segment replay before deletion reconciliation.

## Authentication scope

V1 uses local usernames and passwords.
It stores no email address and provides no password recovery.
A lost password makes the private account unrecoverable.

The application does not implement utility OAuth, Green Button Connect My Data, SSO, MFA, delegated administration, or shared household accounts.

## Privacy and deletion scope

Raw uploads are scheduled for deletion after normalization and have a fixed 24-hour maximum lifetime.
Encrypted backups can retain deleted data until their fixed 30-day expiry.
Individual backups are not rewritten after each deletion.

The public redacted report omits exact schedules, interval timestamps, daily series, and identifiers.
It still exposes aggregate energy, supported costs, tariff provenance, solver status, and result hashes by design.

## Human validation status

The genuine five-person first-time-user study has not occurred.
The state is `HUMAN_VALIDATION_DEFERRED`, with zero genuine participants recorded.
Synthetic personas were used only to test the protocol and interface and count as zero participants.

Milestones 6 through 9 cannot be marked `ACCEPTED` until at least four of five genuine participants pass the frozen protocol and all downstream sequential checks pass.

## Publication status

The repository has not been published, deployed, released, or promoted by this implementation workflow.
Those external actions require explicit authorization.
Milestone 10 features remain post-release extensions and are not part of V1 acceptance.
