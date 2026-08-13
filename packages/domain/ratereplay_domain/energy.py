"""Exact energy admission rules."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


class EnergyAdmissionError(ValueError):
    """A stable calculation-admission failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def exact_watt_hours(
    source_value: str | int | Decimal,
    *,
    source_unit: str,
    power_of_ten_multiplier: int = 0,
) -> int:
    """Convert a source value to integral watt-hours without rounding."""

    if isinstance(source_value, float):
        raise TypeError("Binary floating-point energy inputs are not accepted")
    try:
        value = Decimal(source_value)
    except (InvalidOperation, ValueError) as error:
        raise EnergyAdmissionError("INVALID_ENERGY_VALUE", "Energy value is not decimal") from error
    if not value.is_finite():
        raise EnergyAdmissionError("INVALID_ENERGY_VALUE", "Energy value must be finite")
    if value < 0:
        raise EnergyAdmissionError("NEGATIVE_ENERGY", "Import energy cannot be negative")
    normalized_unit = source_unit.strip().casefold()
    unit_multiplier = {"wh": Decimal(1), "kwh": Decimal(1000)}.get(normalized_unit)
    if unit_multiplier is None:
        raise EnergyAdmissionError("UNKNOWN_UNIT", f"Unsupported energy unit: {source_unit}")
    if not -12 <= power_of_ten_multiplier <= 12:
        raise EnergyAdmissionError("INVALID_MULTIPLIER", "Power-of-ten multiplier is out of range")
    watt_hours = value * unit_multiplier * (Decimal(10) ** power_of_ten_multiplier)
    integral = watt_hours.to_integral_value()
    if watt_hours != integral:
        raise EnergyAdmissionError(
            "NON_INTEGRAL_WATT_HOUR",
            "Source energy does not convert exactly to an integral watt-hour",
        )
    return int(integral)
