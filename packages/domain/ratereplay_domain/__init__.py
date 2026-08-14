"""Core immutable domain contracts for RateReplay."""

from ratereplay_domain.energy import EnergyAdmissionError, exact_watt_hours
from ratereplay_domain.profile_hash import CanonicalProfileContentV1
from ratereplay_domain.semantic_identity import SemanticCalculationIdentity

__all__ = [
    "CanonicalProfileContentV1",
    "EnergyAdmissionError",
    "SemanticCalculationIdentity",
    "exact_watt_hours",
]
