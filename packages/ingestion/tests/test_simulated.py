from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from ratereplay_ingestion.simulated import (
    SimulatedProfileError,
    load_locked_simulated_profile,
)

ROOT = Path(__file__).resolve().parents[3]


def _copy_profile(tmp_path: Path) -> Path:
    target = tmp_path / "repository"
    (target / "data/demo").mkdir(parents=True)
    shutil.copy(ROOT / "data/demo/profile.lock.json", target / "data/demo/profile.lock.json")
    shutil.copy(
        ROOT / "data/demo/july-2026-simulated-profile.json",
        target / "data/demo/july-2026-simulated-profile.json",
    )
    return target


def test_locked_simulated_profile_builds_canonical_content() -> None:
    artifact = load_locked_simulated_profile(ROOT)

    assert artifact.label.startswith("SIMULATED ")
    assert len(artifact.content.readings) == 2_976
    assert sum(reading.energy_wh for reading in artifact.content.readings) == 750_000
    assert artifact.content.interval_resolution_seconds == 900
    assert len(artifact.content.sha256()) == 64


def test_simulated_profile_rejects_content_lock_mismatch(tmp_path: Path) -> None:
    repository = _copy_profile(tmp_path)
    profile = repository / "data/demo/july-2026-simulated-profile.json"
    profile.write_bytes(profile.read_bytes() + b"\n")

    with pytest.raises(SimulatedProfileError) as captured:
        load_locked_simulated_profile(repository)

    assert captured.value.code == "SIMULATED_PROFILE_HASH_MISMATCH"


def test_simulated_profile_rejects_invalid_locked_contract(tmp_path: Path) -> None:
    repository = _copy_profile(tmp_path)
    profile_path = repository / "data/demo/july-2026-simulated-profile.json"
    payload = json.loads(profile_path.read_bytes())
    payload["readings"][1]["start_utc"] = "2026-07-01T07:16:00Z"
    serialized = (json.dumps(payload, indent=2) + "\n").encode()
    profile_path.write_bytes(serialized)
    lock_path = repository / "data/demo/profile.lock.json"
    lock = json.loads(lock_path.read_bytes())
    lock["artifact_sha256"] = hashlib.sha256(serialized).hexdigest()
    lock_path.write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(SimulatedProfileError) as captured:
        load_locked_simulated_profile(repository)

    assert captured.value.code == "SIMULATED_PROFILE_CONTRACT_INVALID"
