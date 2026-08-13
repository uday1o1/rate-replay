from ratereplay_optimizer.spike import exhaustive_spike_oracle, solve_interruptible_spike


def test_reference_ir_lowering_and_independent_oracle_agree() -> None:
    reference = (1, 0, 1, 0)
    flat_e1_rate = (325_610, 325_610, 325_610, 325_610)
    oracle_objective, optimum_set = exhaustive_spike_oracle(
        reference_wh=reference,
        rate_microdollars_per_kwh=flat_e1_rate,
        required_energy_wh=2,
        maximum_slot_energy_wh=1,
    )
    result = solve_interruptible_spike(
        reference_wh=reference,
        rate_microdollars_per_kwh=flat_e1_rate,
        required_energy_wh=2,
        maximum_slot_energy_wh=1,
    )
    assert result.objective == oracle_objective
    assert result.schedule_wh in optimum_set


def test_spike_is_repeatable_under_locked_parameters() -> None:
    first = solve_interruptible_spike(
        reference_wh=(0, 1, 0, 1),
        rate_microdollars_per_kwh=(325_610, 325_610, 325_610, 325_610),
        required_energy_wh=2,
        maximum_slot_energy_wh=1,
    )
    second = solve_interruptible_spike(
        reference_wh=(0, 1, 0, 1),
        rate_microdollars_per_kwh=(325_610, 325_610, 325_610, 325_610),
        required_energy_wh=2,
        maximum_slot_energy_wh=1,
    )
    assert first == second
