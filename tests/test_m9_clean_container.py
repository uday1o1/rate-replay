from __future__ import annotations

from pathlib import Path

from scripts.qualify_m9_clean_container import (
    COMPOSE_SHA256,
    CONTAINER_IMAGE,
    CONTAINER_PLATFORM,
    MAKE_PACKAGE_SHA256,
    NODE_ARCHIVE_SHA256,
    PYTHON_VERSION,
    QUALIFICATION_COMMANDS,
    REQUIRED_OUTPUT_MARKERS,
    RIPGREP_PACKAGE_SHA256,
    UV_WHEEL_SHA256,
    _container_script,
)


def test_qualification_environment_pins_image_and_tool_downloads() -> None:
    assert CONTAINER_IMAGE == (
        "mcr.microsoft.com/playwright@sha256:"
        "dcc5531e97840b9b5e794f2814476b21571c5124a3fca2267d73041f56e7580e"
    )
    assert CONTAINER_PLATFORM == "linux/amd64"
    assert PYTHON_VERSION == "3.12.13"
    assert UV_WHEEL_SHA256 == ("7a85330de0a7eb0d5c6cf03c80edfb86facad19df367a0b52fc906db1ab15ce9")
    assert NODE_ARCHIVE_SHA256 == (
        "2faf6a387e9b62b888e21c54f01249fb27537ffecf1842f29f4c919d0a59a0ff"
    )
    assert COMPOSE_SHA256 == ("837fd1d35bf6a494f41b5b5988269a7be79de337cf1a1a6ff0e45ab51bb4e9be")
    assert MAKE_PACKAGE_SHA256 == (
        "1fe6a815b56c7b6e9ce4086a363f09444bbd0a0d30e230c453d0b78e44b57a99"
    )
    assert RIPGREP_PACKAGE_SHA256 == (
        "c5ae63c7bee915b1cfc9f0bd07c55b4ce7f2bcd1133cba4da56719aac26101a4"
    )


def test_container_script_keeps_downloads_and_package_state_outside_source() -> None:
    script = _container_script()
    assert script.startswith("set -eu\ncd /qualification/source\n")
    assert "tools=/qualification/tools" in script
    assert "apt-get" not in script
    assert "make_4.3-4.1build2_amd64.deb" in script
    assert "ripgrep_14.1.0-1_amd64.deb" in script
    assert "--exclude='*/bin/corepack'" in script
    assert (
        'ln -s ../lib/node_modules/corepack/dist/corepack.js "$tools/node/bin/corepack"' in script
    )
    assert "playwright install --with-deps" not in script
    assert f"uv python install '{PYTHON_VERSION}'" not in script
    assert f"\"$uv\" python install '{PYTHON_VERSION}'" in script
    for checksum in (
        UV_WHEEL_SHA256,
        NODE_ARCHIVE_SHA256,
        COMPOSE_SHA256,
        MAKE_PACKAGE_SHA256,
        RIPGREP_PACKAGE_SHA256,
    ):
        assert checksum in script


def test_qualification_commands_cover_public_and_private_verification() -> None:
    assert QUALIFICATION_COMMANDS == (
        "make bootstrap",
        "make check",
        "make dependency-audit",
        "make qualification-m3",
        "make qualification-m4",
    )
    assert REQUIRED_OUTPUT_MARKERS == (
        "Repository evidence locks are internally consistent.",
        "Milestone 3 qualification passed:",
        "Milestone 4 qualification passed:",
        "Public demo artifacts are reproducible and current.",
        "No known vulnerabilities found",
    )


def test_docker_context_retains_release_evidence_docs_and_tests() -> None:
    root = Path(__file__).resolve().parents[1]
    ignored = set((root / ".dockerignore").read_text(encoding="utf-8").splitlines())
    assert {"docs", "evidence", "tests"}.isdisjoint(ignored)
    assert {".qualification-tools", ".qualification-work-*"}.issubset(ignored)
    git_ignored = set((root / ".gitignore").read_text(encoding="utf-8").splitlines())
    assert {".qualification-tools/", ".qualification-work-*/"}.issubset(git_ignored)
