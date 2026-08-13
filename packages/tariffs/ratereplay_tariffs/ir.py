"""Small canonical integer charge IR used by the Milestone 0 feasibility spike."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

SIGNED_INT64_MAX = 2**63 - 1
MICRODOLLARS_PER_CENT = 10_000
WATT_HOURS_PER_KILOWATT_HOUR = 1_000


class ChargeIRError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Tier:
    upper_bound_wh: int | None
    rate_microdollars_per_kwh: int


@dataclass(frozen=True, slots=True)
class TieredEnergyCharge:
    rule_id: str
    line_item_key: str
    tiers: tuple[Tier, ...]
    rounding: str = "HALF_UP_CENT_AT_LINE_ITEM"


@dataclass(frozen=True, slots=True)
class FixedDailyCharge:
    rule_id: str
    line_item_key: str
    rate_microdollars_per_day: int
    rounding: str = "HALF_UP_CENT_AT_LINE_ITEM"


@dataclass(frozen=True, slots=True)
class FixedMonthlyCharge:
    rule_id: str
    line_item_key: str
    amount_microdollars: int
    rounding: str = "HALF_UP_CENT_AT_LINE_ITEM"


@dataclass(frozen=True, slots=True)
class CompiledChargeIR:
    version: str
    tariff_version_id: str
    tiered_energy: TieredEnergyCharge
    fixed_daily: FixedDailyCharge
    fixed_monthly: tuple[FixedMonthlyCharge, ...] = ()

    def validate_bounds(self, *, maximum_energy_wh: int, maximum_days: int) -> None:
        if maximum_energy_wh < 0 or maximum_days <= 0:
            raise ChargeIRError("INVALID_BOUND", "Bounds must be nonnegative and nonzero")
        previous = 0
        for tier in self.tiered_energy.tiers:
            if tier.rate_microdollars_per_kwh < 0:
                raise ChargeIRError("NEGATIVE_RATE", "V1 spike rates must be nonnegative")
            if tier.upper_bound_wh is not None:
                if tier.upper_bound_wh <= previous:
                    raise ChargeIRError("INVALID_TIER", "Tier bounds must be strictly increasing")
                previous = tier.upper_bound_wh
        if not self.tiered_energy.tiers or self.tiered_energy.tiers[-1].upper_bound_wh is not None:
            raise ChargeIRError("UNCOVERED_TIER", "Final tier must be unbounded")
        max_rate = max(tier.rate_microdollars_per_kwh for tier in self.tiered_energy.tiers)
        energy_numerator = maximum_energy_wh * max_rate
        fixed_numerator = maximum_days * self.fixed_daily.rate_microdollars_per_day
        monthly_numerator = sum(abs(charge.amount_microdollars) for charge in self.fixed_monthly)
        if (
            max(
                energy_numerator,
                fixed_numerator,
                monthly_numerator,
                energy_numerator + fixed_numerator + monthly_numerator,
            )
            > SIGNED_INT64_MAX
        ):
            raise ChargeIRError("INT64_OVERFLOW", "Compiled bound exceeds signed 64-bit safety")


@dataclass(frozen=True, slots=True)
class ChargeLine:
    rule_id: str
    line_item_key: str
    quantity: str
    rate: str
    pre_round_microdollars: Fraction
    rounded_cents: int
    rounding: str


@dataclass(frozen=True, slots=True)
class ChargeResult:
    lines: tuple[ChargeLine, ...]

    @property
    def total_cents(self) -> int:
        return sum(line.rounded_cents for line in self.lines)


def round_half_up_cents(microdollars: Fraction) -> int:
    if microdollars < 0:
        return -round_half_up_cents(-microdollars)
    quotient, remainder = divmod(
        microdollars.numerator, microdollars.denominator * MICRODOLLARS_PER_CENT
    )
    threshold = microdollars.denominator * MICRODOLLARS_PER_CENT
    return quotient + (1 if remainder * 2 >= threshold else 0)


def evaluate_compiled_ir(
    ir: CompiledChargeIR, *, energy_wh: int, billing_days: int
) -> ChargeResult:
    """Evaluate canonical IR using exact integers and fractions."""

    ir.validate_bounds(maximum_energy_wh=energy_wh, maximum_days=billing_days)
    remaining = energy_wh
    lower = 0
    lines: list[ChargeLine] = []
    for index, tier in enumerate(ir.tiered_energy.tiers, start=1):
        tier_capacity = remaining
        if tier.upper_bound_wh is not None:
            tier_capacity = min(remaining, tier.upper_bound_wh - lower)
        if tier_capacity > 0:
            raw = Fraction(
                tier_capacity * tier.rate_microdollars_per_kwh,
                WATT_HOURS_PER_KILOWATT_HOUR,
            )
            lines.append(
                ChargeLine(
                    rule_id=ir.tiered_energy.rule_id,
                    line_item_key=f"{ir.tiered_energy.line_item_key}.tier_{index}",
                    quantity=f"{tier_capacity} Wh",
                    rate=f"{tier.rate_microdollars_per_kwh} microdollars/kWh",
                    pre_round_microdollars=raw,
                    rounded_cents=round_half_up_cents(raw),
                    rounding=ir.tiered_energy.rounding,
                )
            )
            remaining -= tier_capacity
        if remaining == 0:
            break
        if tier.upper_bound_wh is not None:
            lower = tier.upper_bound_wh
    fixed_raw = Fraction(billing_days * ir.fixed_daily.rate_microdollars_per_day)
    lines.append(
        ChargeLine(
            rule_id=ir.fixed_daily.rule_id,
            line_item_key=ir.fixed_daily.line_item_key,
            quantity=f"{billing_days} days",
            rate=f"{ir.fixed_daily.rate_microdollars_per_day} microdollars/day",
            pre_round_microdollars=fixed_raw,
            rounded_cents=round_half_up_cents(fixed_raw),
            rounding=ir.fixed_daily.rounding,
        )
    )
    for fixed_monthly in ir.fixed_monthly:
        raw = Fraction(fixed_monthly.amount_microdollars)
        lines.append(
            ChargeLine(
                rule_id=fixed_monthly.rule_id,
                line_item_key=fixed_monthly.line_item_key,
                quantity="1 billing cycle",
                rate=f"{fixed_monthly.amount_microdollars} microdollars",
                pre_round_microdollars=raw,
                rounded_cents=round_half_up_cents(raw),
                rounding=fixed_monthly.rounding,
            )
        )
    return ChargeResult(lines=tuple(lines))


def e1_july_2026_ir(*, baseline_wh: int) -> CompiledChargeIR:
    return CompiledChargeIR(
        version="compiled-charge-ir-v1",
        tariff_version_id="pge-e1-2026-06-01",
        tiered_energy=TieredEnergyCharge(
            rule_id="E1_TOTAL_ENERGY_2026_06_01",
            line_item_key="bundled_energy",
            tiers=(
                Tier(upper_bound_wh=baseline_wh, rate_microdollars_per_kwh=325_610),
                Tier(upper_bound_wh=None, rate_microdollars_per_kwh=407_020),
            ),
        ),
        fixed_daily=FixedDailyCharge(
            rule_id="BSC_TIER3_2026_06_01",
            line_item_key="base_services_charge",
            rate_microdollars_per_day=793_430,
        ),
        fixed_monthly=(
            FixedMonthlyCharge(
                rule_id="CALIFORNIA_CLIMATE_CREDIT_2026",
                line_item_key="california_climate_credit",
                amount_microdollars=-36_180_000,
            ),
        ),
    )
