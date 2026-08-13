from decimal import Decimal

import pytest
from ratereplay_domain.energy import EnergyAdmissionError, exact_watt_hours


def test_exact_energy_conversion() -> None:
    assert exact_watt_hours("1.250", source_unit="kWh") == 1250
    assert exact_watt_hours(703, source_unit="Wh") == 703
    assert exact_watt_hours(7, source_unit="Wh", power_of_ten_multiplier=3) == 7000


def test_nonintegral_conversion_fails_without_rounding() -> None:
    with pytest.raises(EnergyAdmissionError) as raised:
        exact_watt_hours(1, source_unit="Wh", power_of_ten_multiplier=-1)
    assert raised.value.code == "NON_INTEGRAL_WATT_HOUR"


@pytest.mark.parametrize("value", [float("nan"), 1.5])
def test_binary_float_is_rejected(value: float) -> None:
    with pytest.raises(TypeError):
        exact_watt_hours(value, source_unit="Wh")  # type: ignore[arg-type]


def test_invalid_energy_inputs_are_stable() -> None:
    with pytest.raises(EnergyAdmissionError, match="negative") as negative:
        exact_watt_hours(Decimal("-1"), source_unit="Wh")
    assert negative.value.code == "NEGATIVE_ENERGY"
    with pytest.raises(EnergyAdmissionError) as unit:
        exact_watt_hours(1, source_unit="joule")
    assert unit.value.code == "UNKNOWN_UNIT"
