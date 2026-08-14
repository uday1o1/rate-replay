"""Locked built-in simulated profile admission."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from ratereplay_domain.profile_hash import (
    CanonicalProfileContentV1,
    CanonicalReading,
    FlowDirection,
)


class SimulatedProfileError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SimulatedReading(_StrictModel):
    start_utc: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    duration_seconds: int = Field(gt=0)
    energy_wh: int = Field(ge=0)

    def start(self) -> datetime:
        return datetime.fromisoformat(self.start_utc.replace("Z", "+00:00"))


class SimulatedProfile(_StrictModel):
    profile_schema_version: Literal["simulated-profile-v1"]
    label: str = Field(pattern=r"^SIMULATED ")
    tariff_timezone: Literal["America/Los_Angeles"]
    interval_resolution_seconds: int = Field(gt=0)
    service_window_local: tuple[Literal["2026-07-01"], Literal["2026-08-01"]]
    source_timezone_interpretation: str = Field(min_length=1)
    total_energy_wh: int = Field(gt=0)
    transformation: str = Field(min_length=1)
    readings: tuple[SimulatedReading, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_vector(self) -> SimulatedProfile:
        if any(
            reading.duration_seconds != self.interval_resolution_seconds
            for reading in self.readings
        ):
            raise ValueError("all simulated readings must use the declared resolution")
        for index, (left, right) in enumerate(
            zip(self.readings, self.readings[1:], strict=False), start=1
        ):
            expected = left.start() + timedelta(seconds=left.duration_seconds)
            if right.start() != expected:
                raise ValueError(f"simulated reading vector is not contiguous at index {index}")
        if sum(reading.energy_wh for reading in self.readings) != self.total_energy_wh:
            raise ValueError("simulated reading energy does not equal the declared total")
        first = self.readings[0].start()
        final_end = self.readings[-1].start() + timedelta(
            seconds=self.readings[-1].duration_seconds
        )
        if first != datetime(2026, 7, 1, 7, tzinfo=UTC) or final_end != datetime(
            2026, 8, 1, 7, tzinfo=UTC
        ):
            raise ValueError("simulated readings do not cover the locked July service window")
        return self


@dataclass(frozen=True, slots=True)
class LockedSimulatedProfile:
    artifact_path: str
    artifact_sha256: str
    label: str
    content: CanonicalProfileContentV1


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def load_locked_simulated_profile(repository_root: Path) -> LockedSimulatedProfile:
    """Load and validate the content-addressed built-in July profile."""

    lock_path = repository_root / "data/demo/profile.lock.json"
    try:
        lock = json.loads(lock_path.read_bytes())
        artifact_path = lock["artifact_path"]
        expected_hash = lock["artifact_sha256"]
        if not isinstance(artifact_path, str) or not isinstance(expected_hash, str):
            raise TypeError
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise SimulatedProfileError(
            "SIMULATED_PROFILE_LOCK_INVALID",
            "The built-in simulated profile lock is invalid.",
        ) from error
    artifact = repository_root / artifact_path
    try:
        payload = artifact.read_bytes()
    except FileNotFoundError as error:
        raise SimulatedProfileError(
            "SIMULATED_PROFILE_MISSING",
            "The locked built-in simulated profile is unavailable.",
        ) from error
    observed_hash = _sha256(payload)
    if observed_hash != expected_hash:
        raise SimulatedProfileError(
            "SIMULATED_PROFILE_HASH_MISMATCH",
            "The built-in simulated profile does not match its content lock.",
        )
    try:
        profile = SimulatedProfile.model_validate_json(payload)
    except ValidationError as error:
        raise SimulatedProfileError(
            "SIMULATED_PROFILE_CONTRACT_INVALID",
            "The built-in simulated profile violates its frozen contract.",
        ) from error
    readings = tuple(
        CanonicalReading(
            start_utc_ns=int(reading.start().timestamp()) * 1_000_000_000,
            duration_seconds=reading.duration_seconds,
            energy_wh=reading.energy_wh,
            flow_direction=FlowDirection.IMPORT,
            source_unit="Wh",
            source_multiplier=0,
            source_reading_type="SIMULATED_INTERVAL_ENERGY",
            source_service_category="ELECTRICITY",
            source_commodity="ELECTRICITY",
            source_accumulation_behavior="DELTA_DATA",
            source_data_qualifier="SIMULATED",
            source_time_attribute="NOT_APPLICABLE",
            source_local_time_parameters_hash=None,
            source_timezone_offset_seconds=None,
            source_dst_offset_seconds=None,
            quality_flags=frozenset(),
        )
        for reading in profile.readings
    )
    start_ns = readings[0].start_utc_ns
    end_ns = readings[-1].start_utc_ns + readings[-1].duration_seconds * 1_000_000_000
    content = CanonicalProfileContentV1(
        parser_contract_version="simulated-profile-parser-v1",
        adapter_fingerprint="nrel-derived-frozen-demo-v1",
        finding_policy_version="simulated-profile-findings-v1",
        confirmation_policy_version="locked-simulated-profile-v1",
        billing_period_start_utc_ns=start_ns,
        billing_period_end_utc_ns=end_ns,
        tariff_timezone=profile.tariff_timezone,
        interval_resolution_seconds=profile.interval_resolution_seconds,
        readings=readings,
        findings=(),
        acknowledged_warning_ids=(),
    )
    return LockedSimulatedProfile(
        artifact_path=artifact_path,
        artifact_sha256=observed_hash,
        label=profile.label,
        content=content,
    )
