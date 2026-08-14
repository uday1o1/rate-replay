from __future__ import annotations

from pathlib import Path

from scripts.qualify_m9_clean_container import (
    DOCKERFILE,
    QUALIFICATION_COMMANDS,
    _docker_target_architecture,
)


def test_qualification_image_pins_toolchain_and_compose_checksums() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "FROM node@sha256:" in dockerfile
    assert "FROM python@sha256:" in dockerfile
    assert "uv==0.11.23" in dockerfile
    assert "v5.4.0/docker-compose-linux-${compose_arch}" in dockerfile
    assert "fc5d1371f1ec7987e703da94ede49af3fbfb240b83f22991a98511de7bc4b93b" in dockerfile
    assert "837fd1d35bf6a494f41b5b5988269a7be79de337cf1a1a6ff0e45ab51bb4e9be" in dockerfile


def test_qualification_commands_cover_public_and_private_verification() -> None:
    assert QUALIFICATION_COMMANDS == (
        "make bootstrap",
        "corepack pnpm --filter @ratereplay/web exec playwright install --with-deps chromium",
        "make check",
        "make dependency-audit",
        "make qualification-m3",
        "make qualification-m4",
    )


def test_qualification_dockerfile_is_repository_relative() -> None:
    expected = Path(__file__).resolve().parents[1] / "containers/qualification.Dockerfile"
    assert expected == DOCKERFILE


def test_docker_context_retains_release_evidence_docs_and_tests() -> None:
    root = Path(__file__).resolve().parents[1]
    ignored = set((root / ".dockerignore").read_text(encoding="utf-8").splitlines())
    assert {"docs", "evidence", "tests"}.isdisjoint(ignored)


def test_docker_architecture_is_normalized_for_legacy_builder(monkeypatch) -> None:
    class Result:
        stdout = "aarch64\n"

    monkeypatch.setattr("scripts.qualify_m9_clean_container._run", lambda command: Result())
    assert _docker_target_architecture() == "arm64"
