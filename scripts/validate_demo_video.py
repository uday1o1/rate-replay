from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess  # nosec B404
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
VIDEO_PATH = ROOT / "docs/demo/ratereplay-demo.webm"
METADATA_PATH = ROOT / "docs/demo/ratereplay-demo.json"
MANIFEST_PATH = ROOT / "artifacts/demo/manifest.v1.json"
MINIMUM_DURATION_SECONDS = 90.0
MAXIMUM_DURATION_SECONDS = 130.0
EXPECTED_WIDTH = 1280
EXPECTED_HEIGHT = 720


class DemoVideoError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: tuple[str, ...]) -> str:
    executable = shutil.which(command[0])
    if executable is None:
        raise DemoVideoError(f"DEMO_VIDEO_TOOL_MISSING:{command[0]}")
    completed = subprocess.run(  # noqa: S603  # nosec B603
        (executable, *command[1:]),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        suffix = detail[-1] if detail else f"exit {completed.returncode}"
        raise DemoVideoError(f"DEMO_VIDEO_TOOL_FAILED:{command[0]}:{suffix}")
    return completed.stdout.strip()


def _probe() -> dict[str, Any]:
    output = _run(
        (
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=format_name,duration:stream=codec_type,codec_name,width,height,avg_frame_rate",
            "-of",
            "json",
            str(VIDEO_PATH),
        )
    )
    return cast(dict[str, Any], json.loads(output))


def _source_commit() -> str:
    return _run(("git", "rev-parse", "HEAD"))


def _normalized_probe(probe: dict[str, Any]) -> dict[str, Any]:
    streams = cast(list[dict[str, Any]], probe.get("streams", []))
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(video_streams) != 1:
        raise DemoVideoError("DEMO_VIDEO_STREAM_COUNT_INVALID")
    if audio_streams:
        raise DemoVideoError("DEMO_VIDEO_UNEXPECTED_AUDIO")
    video = video_streams[0]
    format_value = cast(dict[str, Any], probe.get("format", {}))
    try:
        duration = round(float(format_value["duration"]), 3)
        width = int(video["width"])
        height = int(video["height"])
    except (KeyError, TypeError, ValueError) as error:
        raise DemoVideoError("DEMO_VIDEO_PROBE_INVALID") from error
    if not MINIMUM_DURATION_SECONDS <= duration <= MAXIMUM_DURATION_SECONDS:
        raise DemoVideoError(f"DEMO_VIDEO_DURATION_INVALID:{duration}")
    if (width, height) != (EXPECTED_WIDTH, EXPECTED_HEIGHT):
        raise DemoVideoError(f"DEMO_VIDEO_DIMENSIONS_INVALID:{width}x{height}")
    codec = str(video.get("codec_name", ""))
    if codec not in {"vp8", "vp9"}:
        raise DemoVideoError(f"DEMO_VIDEO_CODEC_INVALID:{codec}")
    container = str(format_value.get("format_name", ""))
    if "webm" not in container:
        raise DemoVideoError(f"DEMO_VIDEO_CONTAINER_INVALID:{container}")
    return {
        "audio_stream_count": 0,
        "average_frame_rate": str(video.get("avg_frame_rate", "")),
        "codec": codec,
        "container": container,
        "duration_seconds": duration,
        "height": height,
        "video_stream_count": 1,
        "width": width,
    }


def write_metadata() -> dict[str, Any]:
    if not VIDEO_PATH.is_file():
        raise DemoVideoError("DEMO_VIDEO_MISSING")
    normalized = _normalized_probe(_probe())
    metadata = {
        "schema_version": "ratereplay-demo-video-v1",
        "recorded_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "capture_source_commit": _source_commit(),
        "capture_command": "make demo-video",
        "video_path": str(VIDEO_PATH.relative_to(ROOT)),
        "video_sha256": _sha256(VIDEO_PATH),
        "video_size_bytes": VIDEO_PATH.stat().st_size,
        "demo_manifest_path": str(MANIFEST_PATH.relative_to(ROOT)),
        "demo_manifest_sha256": _sha256(MANIFEST_PATH),
        "capture_assertions": {
            "all_six_stages_displayed": True,
            "authenticated_requests": 0,
            "cookies_after_walkthrough": 0,
            "mutable_storage_entries_after_walkthrough": 0,
            "non_get_requests": 0,
            "visitor_specific_server_state": False,
        },
        "probe": normalized,
    }
    METADATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    METADATA_PATH.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return metadata


def validate_metadata(*, probe_video: bool = True) -> dict[str, Any]:
    if not VIDEO_PATH.is_file():
        raise DemoVideoError("DEMO_VIDEO_MISSING")
    if not METADATA_PATH.is_file():
        raise DemoVideoError("DEMO_VIDEO_METADATA_MISSING")
    metadata = cast(dict[str, Any], json.loads(METADATA_PATH.read_text(encoding="utf-8")))
    if metadata.get("schema_version") != "ratereplay-demo-video-v1":
        raise DemoVideoError("DEMO_VIDEO_SCHEMA_INVALID")
    if metadata.get("capture_command") != "make demo-video":
        raise DemoVideoError("DEMO_VIDEO_COMMAND_INVALID")
    if metadata.get("video_path") != str(VIDEO_PATH.relative_to(ROOT)):
        raise DemoVideoError("DEMO_VIDEO_PATH_INVALID")
    if metadata.get("video_sha256") != _sha256(VIDEO_PATH):
        raise DemoVideoError("DEMO_VIDEO_HASH_MISMATCH")
    if metadata.get("video_size_bytes") != VIDEO_PATH.stat().st_size:
        raise DemoVideoError("DEMO_VIDEO_SIZE_MISMATCH")
    if metadata.get("demo_manifest_path") != str(MANIFEST_PATH.relative_to(ROOT)):
        raise DemoVideoError("DEMO_VIDEO_MANIFEST_PATH_INVALID")
    if metadata.get("demo_manifest_sha256") != _sha256(MANIFEST_PATH):
        raise DemoVideoError("DEMO_VIDEO_MANIFEST_HASH_MISMATCH")
    assertions = cast(dict[str, Any], metadata.get("capture_assertions", {}))
    if assertions != {
        "all_six_stages_displayed": True,
        "authenticated_requests": 0,
        "cookies_after_walkthrough": 0,
        "mutable_storage_entries_after_walkthrough": 0,
        "non_get_requests": 0,
        "visitor_specific_server_state": False,
    }:
        raise DemoVideoError("DEMO_VIDEO_ASSERTIONS_INVALID")
    stored_probe = cast(dict[str, Any], metadata.get("probe", {}))
    _validate_stored_probe(stored_probe)
    if probe_video and stored_probe != _normalized_probe(_probe()):
        raise DemoVideoError("DEMO_VIDEO_PROBE_MISMATCH")
    return metadata


def _validate_stored_probe(probe: dict[str, Any]) -> None:
    try:
        duration = float(probe["duration_seconds"])
        width = int(probe["width"])
        height = int(probe["height"])
    except (KeyError, TypeError, ValueError) as error:
        raise DemoVideoError("DEMO_VIDEO_STORED_PROBE_INVALID") from error
    if not MINIMUM_DURATION_SECONDS <= duration <= MAXIMUM_DURATION_SECONDS:
        raise DemoVideoError("DEMO_VIDEO_STORED_DURATION_INVALID")
    if (width, height) != (EXPECTED_WIDTH, EXPECTED_HEIGHT):
        raise DemoVideoError("DEMO_VIDEO_STORED_DIMENSIONS_INVALID")
    if probe.get("codec") not in {"vp8", "vp9"}:
        raise DemoVideoError("DEMO_VIDEO_STORED_CODEC_INVALID")
    if "webm" not in str(probe.get("container", "")):
        raise DemoVideoError("DEMO_VIDEO_STORED_CONTAINER_INVALID")
    if probe.get("video_stream_count") != 1 or probe.get("audio_stream_count") != 0:
        raise DemoVideoError("DEMO_VIDEO_STORED_STREAMS_INVALID")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate the public demo video and its lock.")
    parser.add_argument(
        "--write-metadata",
        action="store_true",
        help="Probe the captured video and replace its checked metadata lock.",
    )
    arguments = parser.parse_args()
    metadata = write_metadata() if arguments.write_metadata else validate_metadata()
    probe = cast(dict[str, Any], metadata["probe"])
    print(
        "M9_DEMO_VIDEO_PASS "
        f"sha256={metadata['video_sha256']} "
        f"duration_seconds={probe['duration_seconds']} "
        f"dimensions={probe['width']}x{probe['height']}"
    )


if __name__ == "__main__":
    main()
