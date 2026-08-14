SHELL := /bin/sh
UV_CACHE_DIR ?= /private/tmp/rate-replay-uv-cache
PNPM_STORE_DIR ?= /private/tmp/rate-replay-pnpm-store
COMPOSE ?= $(shell command -v docker-compose >/dev/null 2>&1 && echo docker-compose || echo docker compose)

.PHONY: bootstrap check format format-check lint typecheck test security dependency-audit web-build compose-config clean-checkout-check integration-auth integration-backup integration-object-store integration-m1 integration-m2 integration-m3 integration-m4 integration-m5 benchmark-m1-recovery benchmark-m4-v2-failure benchmark-m4-optimization qualification-m2 qualification-m3-goldens qualification-m3 qualification-m4 demo-artifacts demo-artifacts-check

bootstrap:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv sync --frozen --all-groups
	corepack pnpm --store-dir $(PNPM_STORE_DIR) install --frozen-lockfile

format:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run ruff format .
	corepack pnpm format

format-check:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run ruff format --check .
	corepack pnpm format:check

lint:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run ruff check .
	corepack pnpm lint

typecheck:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run mypy apps packages scripts benchmarks/scripts
	corepack pnpm typecheck

test:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run pytest
	corepack pnpm test

security:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run bandit -c pyproject.toml -r apps packages scripts
	./scripts/scan-secrets.sh

dependency-audit:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv export --quiet --frozen --all-groups --no-emit-project --format requirements.txt --output-file /private/tmp/rate-replay-audit-requirements.txt
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run pip-audit --strict --disable-pip --require-hashes --requirement /private/tmp/rate-replay-audit-requirements.txt
	corepack pnpm audit --audit-level high

web-build:
	corepack pnpm build

compose-config:
	$(COMPOSE) -f compose.yaml config --quiet

check: format-check lint typecheck test security web-build compose-config demo-artifacts-check
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python scripts/validate_evidence.py

demo-artifacts:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python scripts/generate_demo_artifacts.py

demo-artifacts-check:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python scripts/generate_demo_artifacts.py --check

clean-checkout-check:
	./scripts/clean-checkout-check.sh

integration-auth:
	@test -n "$(RATEREPLAY_TEST_DATABASE_URL)"
	@RATEREPLAY_DATABASE_URL="$(RATEREPLAY_TEST_DATABASE_URL)" UV_CACHE_DIR=$(UV_CACHE_DIR) uv run alembic upgrade head
	@RATEREPLAY_TEST_DATABASE_URL="$(RATEREPLAY_TEST_DATABASE_URL)" UV_CACHE_DIR=$(UV_CACHE_DIR) uv run pytest --no-cov -m postgres tests/integration/test_auth_postgres.py

integration-object-store:
	@test -n "$(RATEREPLAY_TEST_MINIO_ENDPOINT)"
	@test -n "$(RATEREPLAY_TEST_MINIO_ACCESS_KEY_FILE)"
	@test -n "$(RATEREPLAY_TEST_MINIO_SECRET_KEY_FILE)"
	@RATEREPLAY_TEST_MINIO_ENDPOINT="$(RATEREPLAY_TEST_MINIO_ENDPOINT)" RATEREPLAY_TEST_MINIO_ACCESS_KEY_FILE="$(RATEREPLAY_TEST_MINIO_ACCESS_KEY_FILE)" RATEREPLAY_TEST_MINIO_SECRET_KEY_FILE="$(RATEREPLAY_TEST_MINIO_SECRET_KEY_FILE)" UV_CACHE_DIR=$(UV_CACHE_DIR) uv run pytest --no-cov -m object_store tests/integration/test_object_store_minio.py

integration-backup:
	@test -n "$(RATEREPLAY_TEST_MINIO_ENDPOINT)"
	@test -n "$(RATEREPLAY_TEST_MINIO_ACCESS_KEY_FILE)"
	@test -n "$(RATEREPLAY_TEST_MINIO_SECRET_KEY_FILE)"
	@test -n "$(RATEREPLAY_TEST_BACKUP_MINIO_ENDPOINT)"
	@test -n "$(RATEREPLAY_TEST_BACKUP_MINIO_ACCESS_KEY_FILE)"
	@test -n "$(RATEREPLAY_TEST_BACKUP_MINIO_SECRET_KEY_FILE)"
	@test -n "$(RATEREPLAY_TEST_BACKUP_PGDUMP_COMMAND_JSON)"
	@test -n "$(RATEREPLAY_TEST_BACKUP_PGDUMP_VERSION_COMMAND_JSON)"
	@test -n "$(RATEREPLAY_TEST_BACKUP_PGRESTORE_COMMAND_JSON)"
	@RATEREPLAY_TEST_MINIO_ENDPOINT="$(RATEREPLAY_TEST_MINIO_ENDPOINT)" RATEREPLAY_TEST_MINIO_ACCESS_KEY_FILE="$(RATEREPLAY_TEST_MINIO_ACCESS_KEY_FILE)" RATEREPLAY_TEST_MINIO_SECRET_KEY_FILE="$(RATEREPLAY_TEST_MINIO_SECRET_KEY_FILE)" RATEREPLAY_TEST_BACKUP_MINIO_ENDPOINT="$(RATEREPLAY_TEST_BACKUP_MINIO_ENDPOINT)" RATEREPLAY_TEST_BACKUP_MINIO_ACCESS_KEY_FILE="$(RATEREPLAY_TEST_BACKUP_MINIO_ACCESS_KEY_FILE)" RATEREPLAY_TEST_BACKUP_MINIO_SECRET_KEY_FILE="$(RATEREPLAY_TEST_BACKUP_MINIO_SECRET_KEY_FILE)" RATEREPLAY_TEST_BACKUP_PGDUMP_COMMAND_JSON='$(RATEREPLAY_TEST_BACKUP_PGDUMP_COMMAND_JSON)' RATEREPLAY_TEST_BACKUP_PGDUMP_VERSION_COMMAND_JSON='$(RATEREPLAY_TEST_BACKUP_PGDUMP_VERSION_COMMAND_JSON)' RATEREPLAY_TEST_BACKUP_PGRESTORE_COMMAND_JSON='$(RATEREPLAY_TEST_BACKUP_PGRESTORE_COMMAND_JSON)' UV_CACHE_DIR=$(UV_CACHE_DIR) uv run pytest --no-cov -m backup tests/integration/test_backup_minio_postgres.py

integration-m1:
	@test -n "$(RATEREPLAY_TEST_DATABASE_URL)"
	@RATEREPLAY_DATABASE_URL="$(RATEREPLAY_TEST_DATABASE_URL)" UV_CACHE_DIR=$(UV_CACHE_DIR) uv run alembic upgrade head
	@RATEREPLAY_DATABASE_URL="$(RATEREPLAY_TEST_DATABASE_URL)" UV_CACHE_DIR=$(UV_CACHE_DIR) uv run alembic check
	@RATEREPLAY_TEST_DATABASE_URL="$(RATEREPLAY_TEST_DATABASE_URL)" UV_CACHE_DIR=$(UV_CACHE_DIR) uv run pytest --no-cov -m postgres tests/integration/test_auth_postgres.py tests/integration/test_import_postgres.py

integration-m2:
	@test -n "$(RATEREPLAY_TEST_DATABASE_URL)"
	@RATEREPLAY_DATABASE_URL="$(RATEREPLAY_TEST_DATABASE_URL)" UV_CACHE_DIR=$(UV_CACHE_DIR) uv run alembic upgrade head
	@RATEREPLAY_DATABASE_URL="$(RATEREPLAY_TEST_DATABASE_URL)" UV_CACHE_DIR=$(UV_CACHE_DIR) uv run alembic check
	@RATEREPLAY_TEST_DATABASE_URL="$(RATEREPLAY_TEST_DATABASE_URL)" UV_CACHE_DIR=$(UV_CACHE_DIR) uv run pytest --no-cov -m postgres tests/integration/test_auth_postgres.py tests/integration/test_import_postgres.py tests/integration/test_replay_postgres.py

integration-m3:
	@test -n "$(RATEREPLAY_TEST_DATABASE_URL)"
	@RATEREPLAY_DATABASE_URL="$(RATEREPLAY_TEST_DATABASE_URL)" UV_CACHE_DIR=$(UV_CACHE_DIR) uv run alembic upgrade head
	@RATEREPLAY_DATABASE_URL="$(RATEREPLAY_TEST_DATABASE_URL)" UV_CACHE_DIR=$(UV_CACHE_DIR) uv run alembic check
	@RATEREPLAY_TEST_DATABASE_URL="$(RATEREPLAY_TEST_DATABASE_URL)" UV_CACHE_DIR=$(UV_CACHE_DIR) uv run pytest --no-cov -m postgres tests/integration/test_auth_postgres.py tests/integration/test_import_postgres.py tests/integration/test_replay_postgres.py tests/integration/test_comparison_postgres.py

integration-m4:
	@test -n "$(RATEREPLAY_TEST_DATABASE_URL)"
	@RATEREPLAY_DATABASE_URL="$(RATEREPLAY_TEST_DATABASE_URL)" UV_CACHE_DIR=$(UV_CACHE_DIR) uv run alembic upgrade head
	@RATEREPLAY_DATABASE_URL="$(RATEREPLAY_TEST_DATABASE_URL)" UV_CACHE_DIR=$(UV_CACHE_DIR) uv run alembic check
	@RATEREPLAY_TEST_DATABASE_URL="$(RATEREPLAY_TEST_DATABASE_URL)" UV_CACHE_DIR=$(UV_CACHE_DIR) uv run pytest --no-cov -m postgres tests/integration/test_auth_postgres.py tests/integration/test_import_postgres.py tests/integration/test_replay_postgres.py tests/integration/test_comparison_postgres.py tests/integration/test_scenario_postgres.py

integration-m5:
	@test -n "$(RATEREPLAY_TEST_DATABASE_URL)"
	@RATEREPLAY_DATABASE_URL="$(RATEREPLAY_TEST_DATABASE_URL)" UV_CACHE_DIR=$(UV_CACHE_DIR) uv run alembic upgrade head
	@RATEREPLAY_DATABASE_URL="$(RATEREPLAY_TEST_DATABASE_URL)" UV_CACHE_DIR=$(UV_CACHE_DIR) uv run alembic check
	@RATEREPLAY_TEST_DATABASE_URL="$(RATEREPLAY_TEST_DATABASE_URL)" UV_CACHE_DIR=$(UV_CACHE_DIR) uv run pytest --no-cov -m postgres tests/integration/test_auth_postgres.py tests/integration/test_import_postgres.py tests/integration/test_replay_postgres.py tests/integration/test_comparison_postgres.py tests/integration/test_scenario_postgres.py tests/integration/test_report_postgres.py tests/integration/test_jobs_postgres.py tests/integration/test_deletion_postgres.py tests/integration/test_restore_postgres.py tests/integration/test_retention_postgres.py

benchmark-m1-recovery:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python benchmarks/scripts/m1_recovery.py

benchmark-m4-v2-failure:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python -m benchmarks.scripts.m4_performance record-v2-failure

benchmark-m4-optimization:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python -m benchmarks.scripts.m4_performance run-v3

qualification-m2:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python scripts/qualify_m2.py
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run pytest --no-cov packages/tariffs/tests/test_compiler.py packages/tariffs/tests/test_billing.py packages/tariffs/tests/test_e1_mutations.py packages/tariffs/tests/test_tariff_cli.py apps/api/tests/test_replay_api.py

qualification-m3-goldens:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python scripts/validate_m3_goldens.py

qualification-m3:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python -m scripts.qualify_m3 --check
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run pytest --no-cov packages/tariffs/tests/test_comparison.py packages/tariffs/tests/test_etouc.py packages/tariffs/tests/test_etoud.py packages/tariffs/tests/test_eelec.py packages/tariffs/tests/test_ev2a.py packages/tariffs/tests/test_tariff_cli.py apps/api/tests/test_comparison_api.py apps/api/tests/test_replay_api.py
	corepack pnpm test

qualification-m4:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python -m scripts.qualify_m4
	corepack pnpm exec prettier --write evidence/correctness/m4-optimizer-qualification.json
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run pytest --no-cov packages/optimizer/tests/test_scenario.py packages/optimizer/tests/test_solver.py packages/optimizer/tests/test_verification.py apps/api/tests/test_scenario_api.py apps/api/tests/test_portfolio_core_api.py
	corepack pnpm test
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python scripts/validate_evidence.py
