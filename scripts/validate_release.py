"""Validate the hardened local release topology without starting containers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import yaml  # type: ignore[import-untyped]

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
COMPOSE_PATH: Final = REPOSITORY_ROOT / "compose.release.yaml"
REQUIRED_SERVICES: Final = frozenset(
    {
        "api",
        "backup-store",
        "migrate",
        "object-store",
        "postgres",
        "proxy",
        "web",
        "worker",
    }
)
HARDENED_SERVICES: Final = frozenset(
    {"api", "backup-store", "migrate", "object-store", "proxy", "web", "worker"}
)
DOCKERFILES: Final = {
    "app": REPOSITORY_ROOT / "containers/app.Dockerfile",
    "object-store": REPOSITORY_ROOT / "containers/object-store.Dockerfile",
    "postgres": REPOSITORY_ROOT / "containers/postgres.Dockerfile",
    "proxy": REPOSITORY_ROOT / "containers/proxy.Dockerfile",
    "web": REPOSITORY_ROOT / "containers/web.Dockerfile",
}
PRIVATE_OBJECT_STORE_TMPFS: Final = f"{Path('/').joinpath('tmp')}:mode=0700,uid=1000,gid=1000"
PRIVATE_PGPASS: Final = str(Path("/").joinpath("tmp", "ratereplay-postgres.pgpass"))


class ReleaseContractError(RuntimeError):
    """Raised when the release topology violates its security contract."""


def _mapping(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ReleaseContractError(f"{context} must be a mapping")
    return value


def _load_compose() -> dict[str, Any]:
    try:
        payload = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ReleaseContractError("compose.release.yaml is invalid") from error
    return _mapping(payload, "release compose document")


def _service_networks(service: dict[str, Any]) -> set[str]:
    networks = service.get("networks", [])
    if isinstance(networks, list):
        return {network for network in networks if isinstance(network, str)}
    return set(_mapping(networks, "service networks"))


def _validate_images(services: dict[str, Any]) -> None:
    required_images = {
        "api": "RATEREPLAY_APP_IMAGE",
        "backup-store": "RATEREPLAY_OBJECT_STORE_IMAGE",
        "migrate": "RATEREPLAY_APP_IMAGE",
        "object-store": "RATEREPLAY_OBJECT_STORE_IMAGE",
        "postgres": "RATEREPLAY_POSTGRES_IMAGE",
        "proxy": "RATEREPLAY_PROXY_IMAGE",
        "web": "RATEREPLAY_WEB_IMAGE",
        "worker": "RATEREPLAY_APP_IMAGE",
    }
    for service_name, variable in required_images.items():
        image = services[service_name].get("image")
        if image != f"${{{variable}:?{variable} is required}}":
            raise ReleaseContractError(f"{service_name} must require {variable}")
    for name, path in DOCKERFILES.items():
        source = path.read_text(encoding="utf-8")
        from_lines = [line for line in source.splitlines() if line.startswith("FROM ")]
        if not from_lines or any("@sha256:" not in line for line in from_lines):
            raise ReleaseContractError(f"{name} Dockerfile must pin every base image by digest")
        if not any(line.startswith("USER ") and ":" in line for line in source.splitlines()):
            raise ReleaseContractError(f"{name} Dockerfile must declare a numeric non-root user")


def _validate_isolation(document: dict[str, Any], services: dict[str, Any]) -> None:
    networks = _mapping(document.get("networks"), "release networks")
    backend = _mapping(networks.get("backend"), "backend network")
    if backend.get("internal") is not True:
        raise ReleaseContractError("backend network must be internal")
    for name, service in services.items():
        ports = service.get("ports", [])
        if name != "proxy" and ports:
            raise ReleaseContractError(f"{name} must not publish host ports")
    proxy_ports = services["proxy"].get("ports")
    if not isinstance(proxy_ports, list) or len(proxy_ports) != 2:
        raise ReleaseContractError("proxy must publish exactly HTTP and HTTPS")
    if any(not isinstance(port, str) or not port.startswith("127.0.0.1:") for port in proxy_ports):
        raise ReleaseContractError("local proxy ports must bind only to loopback")
    expected_networks = {
        "api": {"backend", "edge"},
        "backup-store": {"backend"},
        "migrate": {"backend"},
        "object-store": {"backend"},
        "postgres": {"backend"},
        "proxy": {"edge"},
        "web": {"edge"},
        "worker": {"backend"},
    }
    for name, expected in expected_networks.items():
        if _service_networks(services[name]) != expected:
            raise ReleaseContractError(f"{name} has an unexpected network attachment")


def _validate_hardening(services: dict[str, Any]) -> None:
    for name in HARDENED_SERVICES:
        service = services[name]
        if service.get("read_only") is not True:
            raise ReleaseContractError(f"{name} root filesystem must be read-only")
        if service.get("cap_drop") != ["ALL"]:
            raise ReleaseContractError(f"{name} must drop every Linux capability")
        options = service.get("security_opt")
        if not isinstance(options, list) or "no-new-privileges:true" not in options:
            raise ReleaseContractError(f"{name} must forbid privilege escalation")
    for name in ("object-store", "backup-store"):
        service = services[name]
        if service.get("tmpfs") != [PRIVATE_OBJECT_STORE_TMPFS]:
            raise ReleaseContractError(f"{name} must use a private ephemeral config directory")
        environment = _mapping(service.get("environment"), f"{name} environment")
        if not all(
            str(environment.get(key, "")).startswith("/run/secrets/")
            for key in (
                "RATEREPLAY_S3_ACCESS_KEY_FILE",
                "RATEREPLAY_S3_SECRET_KEY_FILE",
            )
        ):
            raise ReleaseContractError(f"{name} credentials must use secret files")
    for name in ("api", "migrate", "worker"):
        environment = _mapping(services[name].get("environment"), f"{name} environment")
        if environment.get("RATEREPLAY_POSTGRES_PGPASS_SOURCE_FILE") != (
            "/run/secrets/postgres_pgpass"
        ):
            raise ReleaseContractError(f"{name} must stage pgpass from its secret file")
        if environment.get("PGPASSFILE") != PRIVATE_PGPASS:
            raise ReleaseContractError(f"{name} must use its private pgpass copy")


def _validate_proxy_contract() -> None:
    proxy = (REPOSITORY_ROOT / "ops/caddy/proxy.Caddyfile").read_text(encoding="utf-8")
    required_fragments = (
        "tls internal",
        "Content-Security-Policy",
        "Strict-Transport-Security",
        "X-Forwarded-For {remote_host}",
        "X-Forwarded-Host {host}",
        "X-Forwarded-Proto https",
    )
    missing = [fragment for fragment in required_fragments if fragment not in proxy]
    if missing:
        raise ReleaseContractError(f"proxy policy is missing: {', '.join(missing)}")


def validate_release_assets() -> dict[str, int]:
    document = _load_compose()
    services = _mapping(document.get("services"), "release services")
    if set(services) != REQUIRED_SERVICES:
        raise ReleaseContractError("release compose service inventory changed")
    _validate_images(services)
    _validate_isolation(document, services)
    _validate_hardening(services)
    _validate_proxy_contract()
    return {
        "dockerfiles": len(DOCKERFILES),
        "hardened_services": len(HARDENED_SERVICES),
        "services": len(services),
    }


def main() -> None:
    result = validate_release_assets()
    print(
        "RELEASE_CONFIG_OK "
        f"services={result['services']} "
        f"hardened_services={result['hardened_services']} "
        f"dockerfiles={result['dockerfiles']}"
    )


if __name__ == "__main__":
    main()
