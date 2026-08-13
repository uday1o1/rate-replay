# Third-party notices

The committed ESPI schema comes from the Green Button Alliance OpenESPI repository at commit `06666dc82396b53ed14e7a5c45266ed54015c1ce`.
It is retained verbatim under the Apache License 2.0 and includes NAESB copyright notices in the schema.
The applicable license is in `third_party/licenses/openespi-Apache-2.0.txt`.

The independently sourced Green Button XML fixture comes from `cew821/greenbutton` at commit `01b82199fc267f85542ed991724cf97273bb69bd`.
The fixture contains an EnergyOS Apache License 2.0 notice, and the containing repository's MIT license is retained in `third_party/licenses/cew821-greenbutton-MIT.txt`.

The sanitized provider-produced PG&E CSV fixture comes from `dchassin/home_bill_analysis` at commit `0847c16952862b6176d2ceadc9cb6921e479b5c4`.
It is retained verbatim under GPL-3.0, whose complete text is in `third_party/licenses/home-bill-analysis-GPL-3.0.txt`.
No RateReplay source code is derived from that fixture or repository.

The simulated demo profile is a transformation of the NREL End-Use Load Profiles for the U.S. Building Stock, DOI `10.25984/1876417`, licensed under CC-BY-4.0.
The raw NREL aggregate is not committed.
The source object, hash, archetype, and deterministic transformation are recorded in `data/sources.lock.json` and `data/demo/profile.lock.json`.
