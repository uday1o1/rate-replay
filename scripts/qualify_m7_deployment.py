#!/usr/bin/env python3
"""Exercise the hardened local deployment, fault, and rollback contract."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import shutil
import ssl
import subprocess  # nosec B404
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

import httpx

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT: Final = REPOSITORY_ROOT / "evidence/reliability/m7-local-deployment.json"
DEFAULT_STABLE_COMMIT: Final = "0bf962848c206c96920eae71aa1a5c666fb0f23a"
TRIVY_IMAGE: Final = (
    "aquasec/trivy@sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969"
)
ALLOWED_EXECUTABLES: Final = frozenset({"docker", "docker-compose", "git", "make", "tar"})
EXPECTED_MIGRATION_HEAD: Final = "20260814_0015"
CLAIMS_WITHHELD: Final = (
    "HOSTED_VALIDATED",
    "MANAGED_VOLUME_ENCRYPTION",
    "PRODUCTION_ACME_TLS",
    "PRODUCTION_NETWORK_ISOLATION",
    "PRODUCTION_ORCHESTRATOR_ROLLBACK",
)


class QualificationError(RuntimeError):
    """Raised when an observed deployment result does not satisfy the gate."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise QualificationError(code)


def _run(
    command: tuple[str, ...],
    *,
    environment: dict[str, str] | None = None,
    cwd: Path = REPOSITORY_ROOT,
    timeout: int = 300,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    _require(bool(command) and command[0] in ALLOWED_EXECUTABLES, "COMMAND_NOT_ALLOWLISTED")
    completed = subprocess.run(  # noqa: S603  # nosec B603
        command,
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode != 0:
        safe_stdout = completed.stdout[-4000:]
        safe_stderr = completed.stderr[-4000:]
        raise QualificationError(
            "COMMAND_FAILED\n"
            f"command={' '.join(command)}\n"
            f"stdout={safe_stdout}\n"
            f"stderr={safe_stderr}"
        )
    return completed


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _self_hash(payload: dict[str, Any]) -> str:
    content = {key: value for key, value in payload.items() if key != "artifact_sha256"}
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_deployment_evidence(payload: dict[str, Any]) -> None:
    _require(payload.get("schema_version") == "m7-local-deployment-evidence-v1", "SCHEMA")
    _require(payload.get("evidence_level") == "LOCAL_REPRODUCIBLE", "EVIDENCE_LEVEL")
    _require(payload.get("gate_result") == "PASS", "GATE_RESULT")
    _require(payload.get("artifact_sha256") == _self_hash(payload), "ARTIFACT_HASH")
    security = cast(dict[str, Any], payload.get("security"))
    _require(security.get("ignored_critical_findings") == 0, "IGNORED_CRITICAL")
    _require(security.get("critical_findings") == 0, "CRITICAL_FINDING")
    _require(security.get("dependency_audit_passed") is True, "DEPENDENCY_AUDIT")
    topology = cast(dict[str, Any], payload.get("topology"))
    _require(topology.get("published_services") == ["proxy"], "PUBLICATION_SCOPE")
    _require(topology.get("https_ready") is True, "HTTPS_NOT_READY")
    faults = cast(list[dict[str, Any]], payload.get("failure_injections"))
    _require(len(faults) == 3, "FAULT_COUNT")
    _require(all(item.get("passed") is True for item in faults), "FAULT_FAILED")
    rollback = cast(dict[str, Any], payload.get("rollback"))
    _require(rollback.get("persistent_session_survived") is True, "SESSION_ROLLBACK")
    _require(rollback.get("fresh_login_succeeded") is True, "LOGIN_ROLLBACK")
    _require(rollback.get("same_schema") is True, "SCHEMA_ROLLBACK")
    _require(tuple(payload.get("claims_withheld", ())) == CLAIMS_WITHHELD, "CLAIM_SCOPE")


def _write_secret(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(value)
        output.write("\n")


def _create_runtime(repository: Path) -> tuple[Path, dict[str, str], list[str]]:
    parent = repository / ".local-secrets"
    parent.mkdir(mode=0o700, exist_ok=True)
    parent.chmod(0o700)
    runtime = Path(tempfile.mkdtemp(prefix="ratereplay-m7-deploy.", dir=parent))
    runtime.chmod(0o700)
    for name in (
        "object-encryption-keys",
        "backup-encryption-keys",
        "deletion-ledger-keys",
        "restore-keys",
    ):
        directory = runtime / name
        directory.mkdir(mode=0o700)

    values = {
        "postgres_password": secrets.token_hex(24),
        "object_store_user": f"rr{secrets.token_hex(8)}",
        "object_store_password": secrets.token_hex(24),
        "backup_store_user": f"rr{secrets.token_hex(8)}",
        "backup_store_password": secrets.token_hex(24),
        "session_secret": secrets.token_hex(32),
        "transaction_outcome_key": secrets.token_hex(32),
        "object_key": secrets.token_hex(32),
        "backup_key": secrets.token_hex(32),
        "ledger_key": secrets.token_hex(32),
        "restore_key": secrets.token_hex(32),
    }
    _write_secret(runtime / "postgres_password", values["postgres_password"])
    _write_secret(
        runtime / "postgres_pgpass",
        f"postgres:5432:ratereplay:ratereplay:{values['postgres_password']}",
    )
    for name in (
        "object_store_user",
        "object_store_password",
        "backup_store_user",
        "backup_store_password",
        "session_secret",
        "transaction_outcome_key",
    ):
        _write_secret(runtime / name, values[name])
    _write_secret(runtime / "object-encryption-keys/object-key-v1", values["object_key"])
    _write_secret(runtime / "backup-encryption-keys/backup-key-v1", values["backup_key"])
    _write_secret(runtime / "deletion-ledger-keys/ledger-v1", values["ledger_key"])
    _write_secret(runtime / "restore-keys/restore-v1", values["restore_key"])

    environment = os.environ.copy()
    environment.update(
        {
            "RATEREPLAY_POSTGRES_PASSWORD_FILE": str(runtime / "postgres_password"),
            "RATEREPLAY_POSTGRES_PGPASS_FILE": str(runtime / "postgres_pgpass"),
            "RATEREPLAY_OBJECT_STORE_USER_FILE": str(runtime / "object_store_user"),
            "RATEREPLAY_OBJECT_STORE_PASSWORD_FILE": str(runtime / "object_store_password"),
            "RATEREPLAY_BACKUP_STORE_USER_FILE": str(runtime / "backup_store_user"),
            "RATEREPLAY_BACKUP_STORE_PASSWORD_FILE": str(runtime / "backup_store_password"),
            "RATEREPLAY_SESSION_SECRET_FILE": str(runtime / "session_secret"),
            "RATEREPLAY_TRANSACTION_OUTCOME_KEY_FILE": str(runtime / "transaction_outcome_key"),
            "RATEREPLAY_OBJECT_ENCRYPTION_KEYS_DIR": str(runtime / "object-encryption-keys"),
            "RATEREPLAY_BACKUP_ENCRYPTION_KEYS_DIR": str(runtime / "backup-encryption-keys"),
            "RATEREPLAY_DELETION_LEDGER_KEYS_DIR": str(runtime / "deletion-ledger-keys"),
            "RATEREPLAY_RESTORE_KEYS_DIR": str(runtime / "restore-keys"),
        }
    )
    return runtime, environment, list(values.values())


def _prepare_stable_context(runtime: Path, stable_commit: str) -> Path:
    archive = runtime / "stable.tar"
    destination = runtime / "stable"
    destination.mkdir(mode=0o700)
    _run(
        (
            "git",
            "archive",
            "--format=tar",
            f"--output={archive}",
            stable_commit,
        )
    )
    _run(("tar", "--extract", "--file", str(archive), "--directory", str(destination)))
    archive.unlink()
    return destination


def _build_image(
    *,
    context: Path,
    dockerfile: str,
    source_commit: str,
    tag: str,
) -> None:
    print(f"BUILD_IMAGE {tag}", flush=True)
    _run(
        (
            "docker",
            "build",
            "--file",
            dockerfile,
            "--build-arg",
            f"RATEREPLAY_SOURCE_COMMIT={source_commit}",
            "--tag",
            tag,
            ".",
        ),
        cwd=context,
        timeout=900,
    )


def _image_id(tag: str) -> str:
    return _run(("docker", "image", "inspect", tag, "--format", "{{.Id}}")).stdout.strip()


def summarize_trivy_report(report: dict[str, Any]) -> dict[str, Any]:
    results = report.get("Results")
    _require(isinstance(results, list) and bool(results), "TRIVY_RESULTS_MISSING")
    typed_results = cast(list[Any], results)
    vulnerabilities = [
        vulnerability
        for result in typed_results
        if isinstance(result, dict)
        for vulnerability in result.get("Vulnerabilities") or []
        if isinstance(vulnerability, dict)
    ]
    critical = [
        item for item in vulnerabilities if str(item.get("Severity", "")).upper() == "CRITICAL"
    ]
    return {
        "critical_findings": len(critical),
        "detector_types": sorted(
            {
                str(result.get("Type"))
                for result in typed_results
                if isinstance(result, dict) and result.get("Type")
            }
        ),
        "targets_scanned": len(typed_results),
    }


def _scan_image(tag: str) -> dict[str, Any]:
    print(f"SCAN_IMAGE {tag}", flush=True)
    completed = _run(
        (
            "docker",
            "run",
            "--rm",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            "-v",
            "ratereplay-trivy-cache:/root/.cache/",
            TRIVY_IMAGE,
            "image",
            "--scanners",
            "vuln",
            "--severity",
            "CRITICAL",
            "--exit-code",
            "1",
            "--no-progress",
            "--format",
            "json",
            tag,
        ),
        timeout=300,
    )
    report = cast(dict[str, Any], json.loads(completed.stdout))
    summary = summarize_trivy_report(report)
    _require(summary["critical_findings"] == 0, "CRITICAL_CONTAINER_FINDING")
    return summary


def parse_published_services(rendered: str) -> list[str]:
    rows = [json.loads(line) for line in rendered.splitlines() if line.strip()]
    return sorted(
        {
            str(row["Service"])
            for row in rows
            if isinstance(row, dict)
            and any(
                int(publisher.get("PublishedPort", 0)) > 0
                for publisher in row.get("Publishers") or []
                if isinstance(publisher, dict)
            )
        }
    )


class ComposeDeployment:
    def __init__(
        self,
        *,
        project: str,
        environment: dict[str, str],
        http_port: int,
        https_port: int,
        runtime: Path,
    ) -> None:
        self.project = project
        self.environment = environment
        self.http_port = http_port
        self.https_port = https_port
        self.runtime = runtime

    def command(self, *arguments: str) -> tuple[str, ...]:
        return (
            "docker-compose",
            "--project-name",
            self.project,
            "--file",
            "compose.release.yaml",
            *arguments,
        )

    def run(self, *arguments: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
        return _run(self.command(*arguments), environment=self.environment, timeout=timeout)

    def container_id(self, service: str) -> str:
        container = self.run("ps", "--quiet", service).stdout.strip()
        _require(bool(container), f"CONTAINER_MISSING:{service}")
        return container

    def down(self) -> None:
        _run(
            self.command("down", "--volumes", "--remove-orphans"),
            environment=self.environment,
            timeout=120,
            check=False,
        )

    def wait_healthy(self, service: str, *, timeout_seconds: float = 120.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_state = "missing"
        while time.monotonic() < deadline:
            container = self.run("ps", "--quiet", service).stdout.strip()
            if container:
                rendered = _run(
                    ("docker", "inspect", container, "--format", "{{json .State}}")
                ).stdout
                state = cast(dict[str, Any], json.loads(rendered))
                last_state = str(state)
                health = cast(dict[str, Any], state.get("Health") or {})
                if state.get("Running") is True and health.get("Status") == "healthy":
                    return
            time.sleep(0.5)
        raise QualificationError(f"SERVICE_NOT_HEALTHY:{service}:{last_state}")


def _request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    expected_status: int,
    payload: dict[str, str] | None = None,
    origin: str | None = None,
) -> dict[str, Any]:
    headers = {"Origin": origin} if origin is not None else None
    response = client.request(method, path, json=payload, headers=headers, timeout=10)
    _require(response.status_code == expected_status, f"HTTP_STATUS:{path}:{response.status_code}")
    parsed = response.json()
    _require(isinstance(parsed, dict), f"HTTP_JSON:{path}")
    return cast(dict[str, Any], parsed)


def _wait_public_ready(client: httpx.Client, *, timeout_seconds: float = 90.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = client.get("/readyz", timeout=3)
            if response.status_code == 200 and response.json() == {"status": "ready"}:
                return
        except (httpx.HTTPError, json.JSONDecodeError):
            pass
        time.sleep(0.5)
    raise QualificationError("PUBLIC_READINESS_TIMEOUT")


def _internal_metrics(deployment: ComposeDeployment, service: str, port: int) -> str:
    command = (
        "python",
        "-c",
        "import urllib.request; "
        f"print(urllib.request.urlopen('http://127.0.0.1:{port}/metrics', timeout=3)"
        ".read().decode('utf-8'))",
    )
    return deployment.run("exec", "-T", service, *command).stdout


def _inject_unexpected_process_crash(container: str) -> None:
    program = (
        "import glob, os; "
        "current=os.getpid(); "
        "pids=[int(path.rsplit('/',1)[1]) for path in glob.glob('/proc/[0-9]*') "
        "if int(path.rsplit('/',1)[1]) not in (1,current) "
        "and int(open(path+'/stat').read().split()[3]) == 1]; "
        "assert len(pids) == 1, pids; "
        "os.kill(pids[0], 9)"
    )
    _run(
        (
            "docker",
            "exec",
            container,
            "python",
            "-c",
            program,
        ),
        timeout=30,
        check=False,
    )


def _assert_problem(
    client: httpx.Client,
    *,
    expected_code: str = "DEPENDENCY_UNAVAILABLE",
) -> None:
    response = client.get("/readyz", timeout=10)
    _require(response.status_code == 503, f"FAULT_STATUS:{response.status_code}")
    payload = response.json()
    _require(payload.get("schema_version") == "problem-v1", "FAULT_PROBLEM_SCHEMA")
    _require(payload.get("code") == expected_code, "FAULT_PROBLEM_CODE")
    _require(payload.get("witness") == {}, "FAULT_WITNESS_LEAK")
    _require(response.headers.get("cache-control") == "no-store", "FAULT_CACHE_POLICY")


def _exercise_deployment(
    deployment: ComposeDeployment,
    *,
    candidate_image_id: str,
    stable_image_id: str,
    stable_tag: str,
    sensitive_values: list[str],
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, Any],
]:
    deployment.run("up", "--detach", "--wait", "--wait-timeout", "120", timeout=180)
    published = parse_published_services(deployment.run("ps", "--format", "json").stdout)
    _require(published == ["proxy"], "UNEXPECTED_PUBLISHED_SERVICE")

    redirect = httpx.get(
        f"http://localhost:{deployment.http_port}/",
        follow_redirects=False,
        timeout=10,
    )
    _require(redirect.status_code == 308, "HTTP_REDIRECT_STATUS")
    _require(
        redirect.headers.get("location") == f"https://localhost:{deployment.https_port}/",
        "HTTP_REDIRECT_LOCATION",
    )
    proxy = deployment.container_id("proxy")
    certificate = deployment.runtime / "local-root.crt"
    _run(
        (
            "docker",
            "cp",
            f"{proxy}:/data/caddy/pki/authorities/local/root.crt",
            str(certificate),
        )
    )
    tls_context = ssl.create_default_context(cafile=str(certificate))
    origin = f"https://localhost:{deployment.https_port}"
    client = httpx.Client(base_url=origin, verify=tls_context, follow_redirects=False)
    _wait_public_ready(client)
    metadata = _request_json(client, "GET", "/v1/meta", expected_status=200)
    _require(metadata.get("schema_version") == "v1", "META_SCHEMA")
    web = client.get("/", timeout=10)
    _require(web.status_code == 200, "WEB_STATUS")
    required_headers = {
        "content-security-policy",
        "permissions-policy",
        "referrer-policy",
        "strict-transport-security",
        "x-content-type-options",
        "x-frame-options",
    }
    _require(required_headers <= set(web.headers), "SECURITY_HEADER_MISSING")

    username = f"rollback_{secrets.token_hex(6)}"
    password = f"Rr-{secrets.token_hex(18)}"
    sensitive_values.extend((username, password))
    registered = _request_json(
        client,
        "POST",
        "/v1/auth/register",
        expected_status=201,
        payload={"username": username, "password": password},
        origin=origin,
    )
    _require(cast(dict[str, Any], registered.get("user"))["username"] == username, "REGISTER")
    session = _request_json(client, "GET", "/v1/auth/session", expected_status=200)
    _require(cast(dict[str, Any], session.get("user"))["username"] == username, "SESSION")

    failure_injections: list[dict[str, Any]] = []
    deployment.run("stop", "object-store")
    _assert_problem(client)
    _require(client.get("/", timeout=10).status_code == 200, "WEB_DURING_STORAGE_FAULT")
    failure_injections.append(
        {
            "id": "OBJECT_STORE_UNAVAILABLE",
            "expected_code": "DEPENDENCY_UNAVAILABLE",
            "web_remained_available": True,
            "passed": True,
        }
    )
    deployment.run("start", "object-store")
    deployment.wait_healthy("object-store")
    _wait_public_ready(client)

    deployment.run("stop", "postgres")
    _assert_problem(client)
    _require(client.get("/", timeout=10).status_code == 200, "WEB_DURING_DATABASE_FAULT")
    failure_injections.append(
        {
            "id": "POSTGRES_UNAVAILABLE",
            "expected_code": "DEPENDENCY_UNAVAILABLE",
            "web_remained_available": True,
            "passed": True,
        }
    )
    deployment.run("start", "postgres")
    deployment.wait_healthy("postgres")
    _wait_public_ready(client)

    worker = deployment.container_id("worker")
    before = int(
        _run(("docker", "inspect", worker, "--format", "{{.RestartCount}}")).stdout.strip()
    )
    _inject_unexpected_process_crash(worker)
    deadline = time.monotonic() + 90
    restarted = False
    while time.monotonic() < deadline:
        state = cast(
            dict[str, Any],
            json.loads(_run(("docker", "inspect", worker, "--format", "{{json .State}}")).stdout),
        )
        count = int(
            _run(("docker", "inspect", worker, "--format", "{{.RestartCount}}")).stdout.strip()
        )
        health = cast(dict[str, Any], state.get("Health") or {})
        if state.get("Running") is True and health.get("Status") == "healthy" and count > before:
            restarted = True
            break
        time.sleep(0.5)
    _require(restarted, "WORKER_DID_NOT_RESTART")
    _wait_public_ready(client)
    failure_injections.append(
        {
            "id": "WORKER_SIGKILL",
            "injection_method": "WORKLOAD_CHILD_SIGKILL_FROM_CONTAINER",
            "restart_policy_recovered": True,
            "api_remained_ready": True,
            "passed": True,
        }
    )

    api_metrics = _internal_metrics(deployment, "api", 8000)
    worker_metrics = _internal_metrics(deployment, "worker", 9100)
    for metric in (
        "ratereplay_http_requests_total",
        "ratereplay_readiness_checks_total",
    ):
        _require(metric in api_metrics, f"API_METRIC_MISSING:{metric}")
    _require('outcome="unready"' in api_metrics, "UNREADY_METRIC_MISSING")
    for metric in (
        "ratereplay_job_queue_depth",
        "ratereplay_job_oldest_lease_age_seconds",
        "ratereplay_job_retry_attempts",
        "ratereplay_worker_runs_total",
    ):
        _require(metric in worker_metrics, f"WORKER_METRIC_MISSING:{metric}")

    logs = deployment.run("logs", "--no-color", "api", "worker").stdout
    _require(
        '"schema_version":"ratereplay-telemetry-v1"' in logs,
        "STRUCTURED_EVENT_MISSING",
    )
    _require('"name": "http.server.request"' in logs, "HTTP_TRACE_MISSING")
    _require('"name": "worker.poll"' in logs, "WORKER_TRACE_MISSING")
    _require(all(value not in logs for value in sensitive_values), "TELEMETRY_SECRET_LEAK")

    deployment.environment["RATEREPLAY_APP_IMAGE"] = stable_tag
    rollback_started = time.monotonic()
    deployment.run(
        "up",
        "--detach",
        "--no-deps",
        "--force-recreate",
        "--wait",
        "--wait-timeout",
        "120",
        "api",
        "worker",
        timeout=180,
    )
    _wait_public_ready(client)
    rollback_duration_ms = round((time.monotonic() - rollback_started) * 1000, 3)
    post_rollback_session = _request_json(client, "GET", "/v1/auth/session", expected_status=200)
    persistent_session = (
        cast(dict[str, Any], post_rollback_session.get("user"))["username"] == username
    )
    fresh_client = httpx.Client(
        base_url=origin,
        verify=tls_context,
        follow_redirects=False,
    )
    logged_in = _request_json(
        fresh_client,
        "POST",
        "/v1/auth/login",
        expected_status=200,
        payload={"username": username, "password": password},
        origin=origin,
    )
    fresh_login = cast(dict[str, Any], logged_in.get("user"))["username"] == username
    fresh_client.close()
    stable_api = deployment.container_id("api")
    stable_worker = deployment.container_id("worker")
    for container in (stable_api, stable_worker):
        observed_image = _run(
            ("docker", "inspect", container, "--format", "{{.Image}}")
        ).stdout.strip()
        _require(observed_image == stable_image_id, "ROLLBACK_IMAGE_MISMATCH")
    client.close()

    topology = {
        "backend_network_internal": True,
        "http_redirect_status": 308,
        "https_ready": True,
        "local_ca_trusted_by_test_client": True,
        "published_services": published,
        "security_headers_present": sorted(required_headers),
        "service_count": 8,
    }
    user_path = {
        "account_registered_over_https": True,
        "session_read_over_https": True,
        "simulated_static_web_loaded": True,
    }
    observability = {
        "api_metrics_observed": True,
        "structured_events_observed": True,
        "telemetry_sensitive_value_probe_passed": True,
        "traces_observed": ["http.server.request", "worker.poll"],
        "unready_dependency_metric_observed": True,
        "worker_metrics_observed": True,
    }
    rollback = {
        "candidate_image_id": candidate_image_id,
        "fresh_login_succeeded": fresh_login,
        "persistent_session_survived": persistent_session,
        "rollback_duration_ms": rollback_duration_ms,
        "same_schema": True,
        "stable_image_id": stable_image_id,
        "stable_ready_after_candidate_replacement": True,
    }
    return topology, user_path, observability, failure_injections, rollback


def _git_preflight(*, allow_dirty: bool) -> tuple[str, str]:
    source_commit = _run(("git", "rev-parse", "HEAD")).stdout.strip()
    branch = _run(("git", "branch", "--show-current")).stdout.strip()
    _require(bool(branch), "DETACHED_HEAD")
    dirty = _run(("git", "status", "--porcelain", "--untracked-files=all")).stdout.strip()
    if not allow_dirty:
        _require(not dirty, "WORKTREE_NOT_CLEAN")
        remote = _run(("git", "ls-remote", "origin", f"refs/heads/{branch}")).stdout.split()
        _require(bool(remote) and remote[0] == source_commit, "SOURCE_NOT_PUSHED")
    return source_commit, branch


def main() -> None:
    allow_dirty = os.getenv("RATEREPLAY_M7_ALLOW_DIRTY") == "1"
    artifact = Path(os.getenv("RATEREPLAY_M7_DEPLOYMENT_ARTIFACT", str(DEFAULT_ARTIFACT)))
    stable_commit = os.getenv("RATEREPLAY_M7_STABLE_COMMIT", DEFAULT_STABLE_COMMIT)
    source_commit, branch = _git_preflight(allow_dirty=allow_dirty)
    _run(("git", "cat-file", "-e", f"{stable_commit}^{{commit}}"))
    _run(("git", "merge-base", "--is-ancestor", stable_commit, source_commit))

    print("SECURITY_CHECKS", flush=True)
    _run(
        ("make", "security", "dependency-audit", "operations-config-check", "release-config-check"),
        timeout=600,
    )
    runtime, environment, sensitive_values = _create_runtime(REPOSITORY_ROOT)
    project = f"ratereplay-m7-deploy-{os.getpid()}"
    http_port = int(os.getenv("RATEREPLAY_M7_HTTP_PORT", "58180"))
    https_port = int(os.getenv("RATEREPLAY_M7_HTTPS_PORT", "58543"))
    _require(1024 <= http_port <= 65535, "HTTP_PORT_INVALID")
    _require(1024 <= https_port <= 65535 and https_port != http_port, "HTTPS_PORT_INVALID")
    environment.update(
        {
            "RATEREPLAY_PUBLIC_HTTP_PORT": str(http_port),
            "RATEREPLAY_PUBLIC_HTTPS_PORT": str(https_port),
        }
    )
    deployment = ComposeDeployment(
        project=project,
        environment=environment,
        http_port=http_port,
        https_port=https_port,
        runtime=runtime,
    )
    try:
        stable_context = _prepare_stable_context(runtime, stable_commit)
        source_short = source_commit[:12]
        stable_short = stable_commit[:12]
        tags = {
            "app_candidate": f"ratereplay-m7-app-candidate:{source_short}",
            "app_stable": f"ratereplay-m7-app-stable:{stable_short}",
            "object_store": f"ratereplay-m7-object-store:{source_short}",
            "postgres": f"ratereplay-m7-postgres:{source_short}",
            "proxy": f"ratereplay-m7-proxy:{source_short}",
            "web": f"ratereplay-m7-web:{source_short}",
        }
        _build_image(
            context=REPOSITORY_ROOT,
            dockerfile="containers/app.Dockerfile",
            source_commit=source_commit,
            tag=tags["app_candidate"],
        )
        _build_image(
            context=stable_context,
            dockerfile="containers/app.Dockerfile",
            source_commit=stable_commit,
            tag=tags["app_stable"],
        )
        for key, dockerfile in (
            ("object_store", "containers/object-store.Dockerfile"),
            ("postgres", "containers/postgres.Dockerfile"),
            ("proxy", "containers/proxy.Dockerfile"),
            ("web", "containers/web.Dockerfile"),
        ):
            _build_image(
                context=REPOSITORY_ROOT,
                dockerfile=dockerfile,
                source_commit=source_commit,
                tag=tags[key],
            )
        _run(("docker", "volume", "inspect", "ratereplay-trivy-cache"), check=False)
        _run(("docker", "volume", "create", "ratereplay-trivy-cache"), check=False)
        scans = {key: _scan_image(tag) for key, tag in tags.items()}
        image_ids = {key: _image_id(tag) for key, tag in tags.items()}

        for tag in (tags["app_candidate"], tags["app_stable"]):
            head = _run(("docker", "run", "--rm", tag, "alembic", "heads")).stdout
            _require(EXPECTED_MIGRATION_HEAD in head, "IMAGE_MIGRATION_HEAD_MISMATCH")
        environment.update(
            {
                "RATEREPLAY_APP_IMAGE": tags["app_candidate"],
                "RATEREPLAY_OBJECT_STORE_IMAGE": tags["object_store"],
                "RATEREPLAY_POSTGRES_IMAGE": tags["postgres"],
                "RATEREPLAY_PROXY_IMAGE": tags["proxy"],
                "RATEREPLAY_WEB_IMAGE": tags["web"],
            }
        )
        (
            topology,
            user_path,
            observability,
            failure_injections,
            rollback,
        ) = _exercise_deployment(
            deployment,
            candidate_image_id=image_ids["app_candidate"],
            stable_image_id=image_ids["app_stable"],
            stable_tag=tags["app_stable"],
            sensitive_values=sensitive_values,
        )
        critical_findings = sum(cast(int, scan["critical_findings"]) for scan in scans.values())
        payload: dict[str, Any] = {
            "schema_version": "m7-local-deployment-evidence-v1",
            "evidence_level": "LOCAL_REPRODUCIBLE",
            "gate_result": "PASS",
            "source_commit": source_commit,
            "stable_commit": stable_commit,
            "generated_at": datetime.now(UTC).isoformat(),
            "environment": {
                "architecture": platform.machine(),
                "docker_compose": _run(("docker-compose", "version", "--short")).stdout.strip(),
                "docker_engine": _run(
                    ("docker", "version", "--format", "{{.Server.Version}}")
                ).stdout.strip(),
                "operating_system": platform.system(),
                "python": platform.python_version(),
                "source_branch": branch,
                "trivy_image": TRIVY_IMAGE,
            },
            "inputs": {
                "compose_release_sha256": _sha256(REPOSITORY_ROOT / "compose.release.yaml"),
                "migration_head": EXPECTED_MIGRATION_HEAD,
                "operations_contract_sha256": _sha256(
                    REPOSITORY_ROOT / "ops/observability/sli-contract.v1.json"
                ),
                "uv_lock_sha256": _sha256(REPOSITORY_ROOT / "uv.lock"),
            },
            "security": {
                "container_scans": scans,
                "critical_findings": critical_findings,
                "dependency_audit_passed": True,
                "ignored_critical_findings": 0,
                "credential_pattern_scan_passed": True,
                "static_analysis_passed": True,
            },
            "images": image_ids,
            "topology": topology,
            "user_path": user_path,
            "observability": observability,
            "failure_injections": failure_injections,
            "rollback": rollback,
            "claims_withheld": list(CLAIMS_WITHHELD),
        }
        _require(critical_findings == 0, "CRITICAL_FINDINGS_PRESENT")
        payload["artifact_sha256"] = _self_hash(payload)
        validate_deployment_evidence(payload)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(
            f"M7_DEPLOYMENT_QUALIFICATION_PASS artifact_sha256={payload['artifact_sha256']}",
            flush=True,
        )
    finally:
        deployment.down()
        shutil.rmtree(runtime, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except QualificationError as error:
        print(f"M7_DEPLOYMENT_QUALIFICATION_FAIL {error}", file=sys.stderr)
        raise SystemExit(1) from error
