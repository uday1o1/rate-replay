# PG&E CSV admission contract

## Provenance and retention

The locked fixture is a real PG&E Green Button CSV export contributed to the public `dchassin/home_bill_analysis` repository.
The repository instructions identify it as the file produced by PG&E's Green Button download.
Its repository commit and exact artifact hash are frozen in `data/sources.lock.json`.
The GPL-3.0 repository license permits redistribution under its conditions, and the complete license is retained next to the fixture notices.

The retained file is already sanitized.
The four identifying prologue values for name, address, account number, and service are all exactly `SAMPLE`.
The review procedure scans the prologue for any other value, rejects additional pre-header fields, and confirms that no row contains an identifier outside the fixed provider fields.
No raw customer export may replace this fixture.

## Frozen structure

The file is UTF-8 with a byte-order mark.
The exact column header is `TYPE,DATE,START TIME,END TIME,USAGE,UNITS,COST,NOTES`.
The locked data rows use `Electric usage`, decimal `kWh`, and inclusive-looking minute labels such as `00:00` through `00:14` for one 15-minute interval.
The adapter interprets a row as the half-open local interval beginning at `START TIME` and ending one minute after the displayed `END TIME`.
It requires exactly 15-minute or 60-minute durations after that conversion.
The `COST` column is informational and never enters a calculation.

PG&E serves the locked account class in `America/Los_Angeles`, so the provider-specific adapter supplies that IANA zone.
The fixture covers July and August 2020, when daylight saving time is active, and therefore proves the ordinary DST-active offset only.
For a spring gap, a nonexistent source clock time is fatal.
For a fall-back overlap, duplicate local clocks must occur in source order and map once to the earlier fold and once to the later fold.
A third occurrence, missing mate, non-monotonic UTC result, or duration mismatch is fatal.
Synthetic transition fixtures must exercise those rules before the adapter is production-admitted in Milestone 1.

## Exact energy

Every locked usage value converts from decimal kilowatt-hours to an integral number of watt-hours.
The importer uses exact decimal arithmetic and rejects any nonintegral conversion with `NON_INTEGRAL_WATT_HOUR`.
It never rounds source energy.
