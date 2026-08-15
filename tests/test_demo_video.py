from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast

from scripts.validate_demo_video import (
    MANIFEST_PATH,
    MAXIMUM_DURATION_SECONDS,
    METADATA_PATH,
    MINIMUM_DURATION_SECONDS,
    VIDEO_PATH,
    validate_metadata,
)

ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _metadata() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(METADATA_PATH.read_text(encoding="utf-8")))


def test_demo_video_metadata_is_bound_to_the_tracked_artifacts() -> None:
    metadata = validate_metadata(probe_video=False)

    assert metadata["video_sha256"] == _sha256(VIDEO_PATH)
    assert metadata["demo_manifest_sha256"] == _sha256(MANIFEST_PATH)
    assert metadata["video_size_bytes"] == VIDEO_PATH.stat().st_size
    assert VIDEO_PATH.stat().st_size > 100_000


def test_demo_video_records_a_truthful_two_minute_static_walkthrough() -> None:
    metadata = _metadata()
    probe = metadata["probe"]
    assertions = metadata["capture_assertions"]

    assert MINIMUM_DURATION_SECONDS <= probe["duration_seconds"] <= MAXIMUM_DURATION_SECONDS
    assert probe["width"] == 1280
    assert probe["height"] == 720
    assert probe["video_stream_count"] == 1
    assert probe["audio_stream_count"] == 0
    assert assertions["all_six_stages_displayed"] is True
    assert assertions["authenticated_requests"] == 0
    assert assertions["non_get_requests"] == 0
    assert assertions["cookies_after_walkthrough"] == 0
    assert assertions["mutable_storage_entries_after_walkthrough"] == 0
    assert assertions["visitor_specific_server_state"] is False
    assert re.fullmatch(r"[0-9a-f]{40}", metadata["capture_source_commit"])
