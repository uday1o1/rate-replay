# Milestone 0 source audit

Status: Accepted for the July 2026 E-1 implementation boundary.

## Green Button

The schema authority is the Green Button Alliance OpenESPI ESPI 4.0 schema at repository commit `06666dc82396b53ed14e7a5c45266ed54015c1ce`.
The verbatim schema hash is `5c40d509744cacb1813eb672305cfe83bb9b2ff494e254f269b093ce52010a33`.
The frozen namespaces, code meanings, relationship rules, limits, quality policy, and extension policy are in `data/contracts/espi-admission-v1.json`.

The accepted independent fixture is the EnergyOS sample retained by `cew821/greenbutton` at commit `01b82199fc267f85542ed991724cf97273bb69bd`.
It contains one electric UsagePoint, one hourly MeterReading stream, Pacific LocalTimeParameters, and schema-conforming interval energy.
Its SHA-256 is `da68f74a2c9bcaf796af3aa540463cb662f1264da01e97432fe8c18dfeae1224`.

The fixture and schema are retained verbatim under their notices.
The malicious and semantic-invalid corpus is repository-authored and contains no private data.

## PG&E CSV

The provider-produced fixture was obtained from `dchassin/home_bill_analysis` at commit `0847c16952862b6176d2ceadc9cb6921e479b5c4`.
The source repository directs users to save the file produced by PG&E's Green Button download, and the fixture has the corresponding provider prologue and interval header.
All four identifier values are exactly `SAMPLE` before repository retention.
Its SHA-256 is `fab3fccd5f24070b892473ddefbab95c06689f9e41b0eb5981db2d18cbd8a0ac`.

The fixture is redistributed verbatim under the source repository's GPL-3.0 license.
The full header, unit, timezone, inclusive-end-label, DST, redaction, and exact-energy rules are frozen in `docs/csv-admission-contract.md`.
The locked fixture covers ordinary daylight-saving time but not a transition, so synthetic spring and fall transition fixtures remain mandatory before the Milestone 1 CSV adapter is admitted.

## Tariff authority and July E-1 vector

PG&E filed schedules are the calculation authority.
The mutable tariffbook PDFs were used only for discovery and page cross-checks.
The reproducible sources are PG&E's stable Advice 7846-E and Advice 7921-E archives, with exact hashes in `tariffs/sources.lock.json`.
Complete PDFs are not committed because redistribution permission was not established.

The July E-1 vector contains two independently versioned components.
Advice 7846-E supplies applicability, unbundled components, baseline rules, and special conditions effective March 1, 2026.
Advice 7921-E supplies total energy rates, base-service rates, and the California Climate Credit effective June 1, 2026.
Both components are effective throughout the half-open service window `[2026-07-01, 2026-08-01)`, so the selected vector has no source gap or overlap.

The frozen Territory T basic summer expected values are 31 service days, 6.5 kWh per day, and 201.5 kWh of Tier 1 allowance.
For 310 kWh, the independent worksheet applies 201.5 kWh at USD 0.32561, 108.5 kWh at USD 0.40702, 31 days at USD 0.79343, and one USD 36.18 California Climate Credit in the August bill cycle.
Its line-item cent results are 6,561, 4,416, 2,460, and -3,618, for a total of 9,819 cents.

## Candidate tariffs

No candidate tariff is advertised as production-supported at Milestone 0.
The admission matrix records extracted July rates, implementation requirements, eligibility gaps, comparison components, and calendar dependencies for E-TOU-C, E-TOU-D, E-ELEC, and EV2-A.
E-TOU-C, E-ELEC, and EV2-A apply their time periods every day and therefore have no holiday dependency.
E-TOU-D excludes locally observed holidays from weekday peak treatment, so its July dependency is locked to the California Senate 2026 calendar and its July 3 observed Independence Day entry.

E-TOU-C still requires an executable active-bill-protection exclusion.
E-TOU-D still requires the time calendar compiler and complete eligibility predicate.
E-ELEC still requires a dated qualifying-technology predicate.
EV2-A still requires EV qualification and a source-complete implementation of the filed annual 800-percent baseline eligibility test.
These gaps are admission blockers rather than zero-valued assumptions.

## Demo profile

The simulated input shape comes from NREL's 2021 ResStock AMY2018 California Single-Family Detached aggregate under DOI `10.25984/1876417` and CC-BY-4.0.
The raw 32.8 MB aggregate is not committed.
The generator verifies its SHA-256, selects the July 2018 interval-ending shape, remaps month, day, and clock time to July 2026 Pacific time, and normalizes to exactly 750,000 Wh with largest-remainder integer allocation.
The generated profile is always labeled simulated.
