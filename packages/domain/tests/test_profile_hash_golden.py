import json
from pathlib import Path

from scripts.generate_golden_vectors import golden_profile

ROOT = Path(__file__).resolve().parents[3]


def test_canonical_profile_matches_frozen_diagnostic_vector() -> None:
    golden = json.loads(
        (ROOT / "data/golden/canonical-profile-content-v1.json").read_text(encoding="utf-8")
    )
    profile = golden_profile()
    assert profile.to_bytes().hex() == golden["expected_hex"]
    assert profile.sha256() == golden["expected_sha256"]
