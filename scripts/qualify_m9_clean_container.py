#!/usr/bin/env python3
"""Qualify a clean tracked checkout inside the pinned Linux toolchain image."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess  # nosec B404
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

ROOT: Final = Path(__file__).resolve().parents[1]
DOCKERFILE: Final = ROOT / "containers/qualification.Dockerfile"
OUTPUT: Final = ROOT / "evidence/reproducibility/m9-clean-container.json"
QUALIFICATION_COMMANDS: Final = (
    "make bootstrap",
    "corepack pnpm --filter @ratereplay/web exec playwright install --with-deps chromium",
    "make check",
    "make dependency-audit",
    "make qualification-m3",
    "make qualification-m4",
)
REQUIRED_OUTPUT_MARKERS: Final = (
    "Repository evidence locks are internally consistent.",
    "Milestone 3 qualification passed:",
    "Milestone 4 qualification passed:",
    "Public demo artifacts are reproducible and current.",
    "No known vulnerabilities found",
)
ALLOWED_EXECUTABLES: Final = frozenset({"docker", "git"})


class CleanContainerError(RuntimeError):
    """The fresh-container qualification is invalid or failed."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise CleanContainerError(code)


def _run(command: tuple[str, ...], *, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    _require(command[0] in ALLOWED_EXECUTABLES, "COMMAND_NOT_ALLOWLISTED")
    completed = subprocess.run(  # noqa: S603  # nosec B603
        command,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode != 0:
        raise CleanContainerError(
            f"COMMAND_FAILED\ncommand={' '.join(command)}\noutput={completed.stdout[-6000:]}"
        )
    return completed


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_hash(payload: dict[str, Any]) -> str:
    content = {key: value for key, value in payload.items() if key != "artifact_sha256"}
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _git(*arguments: str) -> str:
    return _run(("git", *arguments)).stdout.strip()


def _validate_source_state() -> str:
    _require(_git("status", "--porcelain") == "", "WORKTREE_NOT_CLEAN")
    commit = _git("rev-parse", "HEAD")
    _require(commit == _git("rev-parse", "origin/main"), "SOURCE_NOT_REMOTE_CONFIRMED")
    return commit


def _export_checkout(destination: Path) -> None:
    archive = subprocess.Popen(  # nosec B603
        ("git", "archive", "--format=tar", "HEAD"),  # noqa: S607  # nosec B607
        cwd=ROOT,
        stdout=subprocess.PIPE,
    )
    _require(archive.stdout is not None, "GIT_ARCHIVE_PIPE_MISSING")
    with tarfile.open(fileobj=archive.stdout, mode="r|") as source:
        source.extractall(destination, filter="data")
    _require(archive.wait() == 0, "GIT_ARCHIVE_FAILED")


def _docker_target_architecture() -> str:
    observed = _run(("docker", "info", "--format", "{{.Architecture}}")).stdout.strip()
    mapping = {
        "aarch64": "arm64",
        "arm64": "arm64",
        "amd64": "amd64",
        "x86_64": "amd64",
    }
    _require(observed in mapping, f"UNSUPPORTED_DOCKER_ARCHITECTURE:{observed}")
    return mapping[observed]


def qualify() -> dict[str, Any]:
    commit = _validate_source_state()
    tag = f"ratereplay-m9-clean:{commit[:12]}"
    target_architecture = _docker_target_architecture()
    image_id: str | None = None
    with tempfile.TemporaryDirectory(prefix="rate-replay-m9-clean-") as directory:
        checkout = Path(directory)
        _export_checkout(checkout)
        _run(
            (
                "docker",
                "build",
                "--file",
                str(checkout / "containers/qualification.Dockerfile"),
                "--build-arg",
                f"TARGETARCH={target_architecture}",
                "--tag",
                tag,
                str(checkout),
            )
        )
        image_id = _run(("docker", "image", "inspect", tag, "--format", "{{.Id}}")).stdout.strip()
        environment = _run(
            (
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "sh",
                tag,
                "-c",
                (
                    "printf 'os='; . /etc/os-release; printf '%s\\n' \"$PRETTY_NAME\"; "
                    "printf 'arch='; uname -m; "
                    "printf 'python='; python --version; "
                    "printf 'node='; node --version; "
                    "printf 'uv='; uv --version; "
                    "printf 'pnpm='; corepack pnpm --version; "
                    "printf 'compose='; docker-compose version --short"
                ),
            )
        ).stdout
        verification = _run(("docker", "run", "--rm", tag)).stdout
        for marker in REQUIRED_OUTPUT_MARKERS:
            _require(marker in verification, f"QUALIFICATION_OUTPUT_MISSING:{marker}")

    environment_values = dict(
        line.split("=", 1) for line in environment.splitlines() if "=" in line
    )
    payload: dict[str, Any] = {
        "schema_version": "m9-clean-container-qualification-v1",
        "gate_result": "PASS",
        "evidence_level": "LOCAL_REPRODUCIBLE",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_commit": commit,
        "source_branch": _git("branch", "--show-current"),
        "source_remote_confirmed": True,
        "dockerfile": {
            "path": str(DOCKERFILE.relative_to(ROOT)),
            "sha256": _sha256(DOCKERFILE),
        },
        "qualification_image_id": image_id,
        "host": {
            "system": platform.system(),
            "machine": platform.machine(),
            "docker_server": _run(
                ("docker", "version", "--format", "{{.Server.Version}}")
            ).stdout.strip(),
        },
        "container": environment_values,
        "commands": list(QUALIFICATION_COMMANDS),
        "results": {
            "bootstrap": "PASS",
            "full_check": "PASS",
            "dependency_audit": "PASS",
            "public_demo_reproduction": "PASS",
            "browser_tests": "PASS",
            "milestone_3_qualification": "PASS",
            "milestone_4_qualification": "PASS",
        },
        "claims_withheld": [
            "GITHUB_ACTIONS_PASS",
            "HOSTED_VALIDATED",
            "GENUINE_HUMAN_STUDY",
        ],
        "external_ci_observation": {
            "status": "NOT_COUNTED",
            "reason": "GITHUB_ACTIONS_ACCOUNT_BILLING_BLOCKED_BEFORE_JOB_START",
        },
    }
    payload["artifact_sha256"] = _artifact_hash(payload)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        "M9_CLEAN_CONTAINER_PASS "
        f"source_commit={commit} image_id={image_id} artifact_sha256={payload['artifact_sha256']}"
    )
    return payload


def validate(path: Path = OUTPUT) -> dict[str, Any]:
    payload = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    _require(
        payload["schema_version"] == "m9-clean-container-qualification-v1",
        "M9_CLEAN_CONTAINER_SCHEMA",
    )
    _require(payload["artifact_sha256"] == _artifact_hash(payload), "M9_CLEAN_CONTAINER_HASH")
    _require(payload["gate_result"] == "PASS", "M9_CLEAN_CONTAINER_FAILED")
    _require(payload["source_remote_confirmed"] is True, "M9_SOURCE_NOT_REMOTE_CONFIRMED")
    _require(payload["dockerfile"]["sha256"] == _sha256(DOCKERFILE), "M9_DOCKERFILE_DRIFT")
    _require(
        payload["commands"] == list(QUALIFICATION_COMMANDS),
        "M9_QUALIFICATION_COMMAND_DRIFT",
    )
    _require(set(payload["results"].values()) == {"PASS"}, "M9_RESULT_FAILED")
    _require(
        payload["external_ci_observation"]["status"] == "NOT_COUNTED",
        "M9_EXTERNAL_CI_MISREPRESENTED",
    )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    if arguments.check:
        payload = validate()
        print(
            "M9_CLEAN_CONTAINER_EVIDENCE_OK "
            f"source_commit={payload['source_commit']} artifact_sha256={payload['artifact_sha256']}"
        )
        return
    qualify()


if __name__ == "__main__":
    main()
