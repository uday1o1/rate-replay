#!/usr/bin/env python3
"""Qualify a clean tracked checkout in a pinned, disposable Linux environment."""

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
OUTPUT: Final = ROOT / "evidence/reproducibility/m9-clean-container.json"
CONTAINER_IMAGE: Final = (
    "mcr.microsoft.com/playwright@sha256:"
    "dcc5531e97840b9b5e794f2814476b21571c5124a3fca2267d73041f56e7580e"
)
CONTAINER_PLATFORM: Final = "linux/amd64"
UV_WHEEL_URL: Final = (
    "https://files.pythonhosted.org/packages/19/ff/"
    "764e1c21ba988589d2b505d2b06876b5f06ffe7cc6858dff6cc3faf7cb14/"
    "uv-0.11.23-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
)
UV_WHEEL_SHA256: Final = "7a85330de0a7eb0d5c6cf03c80edfb86facad19df367a0b52fc906db1ab15ce9"
NODE_ARCHIVE_URL: Final = "https://nodejs.org/dist/v24.16.0/node-v24.16.0-linux-x64.tar.gz"
NODE_ARCHIVE_SHA256: Final = "2faf6a387e9b62b888e21c54f01249fb27537ffecf1842f29f4c919d0a59a0ff"
COMPOSE_URL: Final = (
    "https://github.com/docker/compose/releases/download/v5.4.0/docker-compose-linux-x86_64"
)
COMPOSE_SHA256: Final = "837fd1d35bf6a494f41b5b5988269a7be79de337cf1a1a6ff0e45ab51bb4e9be"
MAKE_PACKAGE_URL: Final = (
    "https://archive.ubuntu.com/ubuntu/pool/main/m/make-dfsg/make_4.3-4.1build2_amd64.deb"
)
MAKE_PACKAGE_SHA256: Final = "1fe6a815b56c7b6e9ce4086a363f09444bbd0a0d30e230c453d0b78e44b57a99"
RIPGREP_PACKAGE_URL: Final = (
    "https://archive.ubuntu.com/ubuntu/pool/universe/r/rust-ripgrep/ripgrep_14.1.0-1_amd64.deb"
)
RIPGREP_PACKAGE_SHA256: Final = "c5ae63c7bee915b1cfc9f0bd07c55b4ce7f2bcd1133cba4da56719aac26101a4"
PYTHON_VERSION: Final = "3.12.13"
QUALIFICATION_COMMANDS: Final = (
    "make bootstrap",
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
ENVIRONMENT_PREFIXES: Final = (
    "os=",
    "arch=",
    "python=",
    "node=",
    "uv=",
    "pnpm=",
    "compose=",
    "make=",
    "ripgrep=",
)
ALLOWED_EXECUTABLES: Final = frozenset({"docker", "git"})


class CleanContainerError(RuntimeError):
    """The clean Linux qualification is invalid or failed."""


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


def _container_script() -> str:
    commands = "\n".join(QUALIFICATION_COMMANDS)
    return f"""
set -eu
cd /qualification/source
tools=/qualification/tools
package_root="$tools/packages/root"
mkdir -p "$tools" "$package_root" "$tools/node" "$tools/python" "$tools/uv-cache" \
  "$tools/pnpm-store" "$tools/playwright"

curl --fail --location --silent --show-error '{UV_WHEEL_URL}' --output "$tools/uv.whl"
printf '%s  %s\n' '{UV_WHEEL_SHA256}' "$tools/uv.whl" | sha256sum --check --strict
python3 -m zipfile -e "$tools/uv.whl" "$tools/uv-wheel"
uv="$tools/uv-wheel/uv-0.11.23.data/scripts/uv"
chmod 0755 "$uv"

curl --fail --location --silent --show-error '{NODE_ARCHIVE_URL}' --output "$tools/node.tar.gz"
printf '%s  %s\n' '{NODE_ARCHIVE_SHA256}' "$tools/node.tar.gz" | sha256sum --check --strict
tar --extract --gzip --file "$tools/node.tar.gz" --directory "$tools/node" \
  --strip-components=1 --exclude='*/bin/npm' --exclude='*/bin/npx' --exclude='*/bin/corepack'
ln -s ../lib/node_modules/npm/bin/npm-cli.js "$tools/node/bin/npm"
ln -s ../lib/node_modules/npm/bin/npx-cli.js "$tools/node/bin/npx"
ln -s ../lib/node_modules/corepack/dist/corepack.js "$tools/node/bin/corepack"

curl --fail --location --silent --show-error '{COMPOSE_URL}' --output "$tools/docker-compose"
printf '%s  %s\n' '{COMPOSE_SHA256}' "$tools/docker-compose" | sha256sum --check --strict
chmod 0755 "$tools/docker-compose"

curl --fail --location --silent --show-error '{MAKE_PACKAGE_URL}' --output "$tools/make.deb"
printf '%s  %s\n' '{MAKE_PACKAGE_SHA256}' "$tools/make.deb" | sha256sum --check --strict
dpkg-deb --extract "$tools/make.deb" "$package_root"

curl --fail --location --silent --show-error '{RIPGREP_PACKAGE_URL}' --output "$tools/ripgrep.deb"
printf '%s  %s\n' '{RIPGREP_PACKAGE_SHA256}' "$tools/ripgrep.deb" | sha256sum --check --strict
dpkg-deb --extract "$tools/ripgrep.deb" "$package_root"

export PATH="$package_root/usr/bin:$tools/node/bin:$tools:$PATH"
export UV_CACHE_DIR="$tools/uv-cache"
export UV_PYTHON_INSTALL_DIR="$tools/python"
export UV_PYTHON='{PYTHON_VERSION}'
export PNPM_STORE_DIR="$tools/pnpm-store"
export PLAYWRIGHT_BROWSERS_PATH="$tools/playwright"
export CI=1

"$uv" python install '{PYTHON_VERSION}'
ln -s "$uv" "$tools/uv"
corepack enable --install-directory "$tools/node/bin"

printf 'os='
. /etc/os-release
printf '%s\n' "$PRETTY_NAME"
printf 'arch='
uname -m
printf 'python='
"$uv" run --no-project --python '{PYTHON_VERSION}' python --version
printf 'node='
node --version
printf 'uv='
"$uv" --version
printf 'pnpm='
corepack pnpm --version
printf 'compose='
docker-compose version --short
printf 'make='
make --version | head -n 1
printf 'ripgrep='
rg --version | head -n 1

{commands}
""".strip()


def qualify() -> dict[str, Any]:
    commit = _validate_source_state()
    with tempfile.TemporaryDirectory(prefix=".qualification-work-", dir=ROOT) as directory:
        qualification_root = Path(directory)
        checkout = qualification_root / "source"
        checkout.mkdir()
        _export_checkout(checkout)
        verification = _run(
            (
                "docker",
                "run",
                "--rm",
                "--platform",
                CONTAINER_PLATFORM,
                "--volume",
                f"{qualification_root}:/qualification",
                "--workdir",
                "/qualification/source",
                CONTAINER_IMAGE,
                "bash",
                "-lc",
                _container_script(),
            )
        ).stdout
        for marker in REQUIRED_OUTPUT_MARKERS:
            _require(marker in verification, f"QUALIFICATION_OUTPUT_MISSING:{marker}")

    environment_values = dict(
        line.split("=", 1)
        for line in verification.splitlines()
        if line.startswith(ENVIRONMENT_PREFIXES)
    )
    _require(
        set(environment_values) == {prefix.removesuffix("=") for prefix in ENVIRONMENT_PREFIXES},
        "QUALIFICATION_ENVIRONMENT_INCOMPLETE",
    )
    payload: dict[str, Any] = {
        "schema_version": "m9-clean-container-qualification-v1",
        "gate_result": "PASS",
        "evidence_level": "LOCAL_REPRODUCIBLE",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_commit": commit,
        "source_branch": _git("branch", "--show-current"),
        "source_remote_confirmed": True,
        "qualification_container": {
            "image": CONTAINER_IMAGE,
            "platform": CONTAINER_PLATFORM,
            "python_version": PYTHON_VERSION,
            "uv_wheel_sha256": UV_WHEEL_SHA256,
            "node_archive_sha256": NODE_ARCHIVE_SHA256,
            "compose_binary_sha256": COMPOSE_SHA256,
            "make_package_sha256": MAKE_PACKAGE_SHA256,
            "ripgrep_package_sha256": RIPGREP_PACKAGE_SHA256,
        },
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
        f"source_commit={commit} artifact_sha256={payload['artifact_sha256']}"
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
    expected_container = {
        "image": CONTAINER_IMAGE,
        "platform": CONTAINER_PLATFORM,
        "python_version": PYTHON_VERSION,
        "uv_wheel_sha256": UV_WHEEL_SHA256,
        "node_archive_sha256": NODE_ARCHIVE_SHA256,
        "compose_binary_sha256": COMPOSE_SHA256,
        "make_package_sha256": MAKE_PACKAGE_SHA256,
        "ripgrep_package_sha256": RIPGREP_PACKAGE_SHA256,
    }
    _require(
        payload["qualification_container"] == expected_container,
        "M9_QUALIFICATION_CONTAINER_DRIFT",
    )
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
