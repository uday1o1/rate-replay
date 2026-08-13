# Dependency and platform audit

Status: Accepted for the pinned Milestone 0 dependency set.

All direct Python, JavaScript, toolchain, and container versions are exact in `pyproject.toml`, `uv.lock`, `package.json`, `pnpm-lock.yaml`, and `compose.yaml`.
CI actions are pinned to commit SHA.
The local environment used Python 3.12.13, Node 24.16.0, pnpm 11.21.0 through Corepack, uv 0.11.23, Docker Engine 29.5.2, and Docker Compose 5.4.0.

The direct Python runtime license review found MIT for FastAPI, Pydantic, SQLAlchemy, Alembic, Argon2-cffi, and Typer; Apache-2.0 for asyncpg, OR-Tools, and OpenTelemetry; BSD-3-Clause for lxml, Uvicorn, and HTTPX; PSFL for defusedxml; LGPL-3.0-only for psycopg; and the declared dual license for prometheus-client.
Psycopg is consumed as a dynamically loaded Python package and is not copied into separately relicensed source.

The JavaScript lock license inventory contains MIT, MIT-0, Apache-2.0, BSD-2-Clause, BSD-3-Clause, ISC, MPL-2.0, BlueOak-1.0.0, and CC0-1.0 packages.
No direct application JavaScript dependency has a license incompatible with repository distribution.
MPL components are build dependencies and remain unmodified upstream packages.

The PostgreSQL 16.10 image and MinIO `RELEASE.2025-09-07T16-13-09Z` image both started on Linux arm64 under Docker.
PostgreSQL reached healthy status and returned its exact 16.10 version from a real SQL query.
MinIO reported `linux/arm64`, the pinned release, and ready status through `mc ready local`.
The qualification containers, named test volumes, and disposable ignored secrets were removed after verification.

The exact hashed Python lock export passed `pip-audit --strict --disable-pip --require-hashes` with no known vulnerabilities.
Disabling pip is safe here because the uv export contains the complete transitive dependency graph, exact versions, and hashes, and it prevents the audit from replacing lock verification with a fresh resolution.
The pnpm lock passed the package manager's high-severity audit with no known vulnerabilities.
