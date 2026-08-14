SHELL := /bin/sh
UV_CACHE_DIR ?= /private/tmp/rate-replay-uv-cache
PNPM_STORE_DIR ?= /private/tmp/rate-replay-pnpm-store
COMPOSE ?= $(shell command -v docker-compose >/dev/null 2>&1 && echo docker-compose || echo docker compose)

.PHONY: bootstrap browser-bootstrap browser-test check format format-check lint typecheck test security dependency-audit web-build compose-config operations-config-check release-config-check clean-checkout-check integration-auth integration-backup integration-object-store integration-m1 integration-m2 integration-m3 integration-m4 integration-m5 benchmark-m1-recovery benchmark-m4-v2-failure benchmark-m4-optimization benchmark-m8-performance qualification-m2 qualification-m3-goldens qualification-m3 qualification-m4 qualification-m6-study qualification-m7-restore qualification-m7-deployment qualification-m8 qualification-m8-correctness qualification-m8-release m8-correctness-check m8-performance-check m8-manifest-check demo-artifacts demo-artifacts-check user-study-protocol-check

bootstrap:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv sync --frozen --all-groups
	corepack pnpm --store-dir $(PNPM_STORE_DIR) install --frozen-lockfile
	$(MAKE) browser-bootstrap

browser-bootstrap:
	corepack pnpm --filter @ratereplay/web exec playwright install chromium

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
	RATEREPLAY_POSTGRES_IMAGE=ratereplay-postgres:config \
	RATEREPLAY_APP_IMAGE=ratereplay-app:config \
	RATEREPLAY_OBJECT_STORE_IMAGE=ratereplay-object-store:config \
	RATEREPLAY_WEB_IMAGE=ratereplay-web:config \
	RATEREPLAY_PROXY_IMAGE=ratereplay-proxy:config \
	$(COMPOSE) -f compose.release.yaml config --quiet

check: format-check lint typecheck test browser-test security web-build compose-config operations-config-check release-config-check m8-manifest-check m8-correctness-check m8-performance-check demo-artifacts-check user-study-protocol-check
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python scripts/validate_evidence.py

operations-config-check:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python scripts/validate_operations.py

release-config-check:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python scripts/validate_release.py

browser-test:
	corepack pnpm test:e2e

demo-artifacts:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python scripts/generate_demo_artifacts.py

demo-artifacts-check:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python scripts/generate_demo_artifacts.py --check

user-study-protocol-check:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python scripts/user_comprehension_study.py protocol

qualification-m6-study:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python scripts/user_comprehension_study.py validate --results-dir evidence/user-study

qualification-m7-restore:
	UV_CACHE_DIR=$(UV_CACHE_DIR) ./scripts/qualify_m7_local.sh

qualification-m7-deployment:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python scripts/qualify_m7_deployment.py

m8-manifest-check:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python scripts/validate_m8_manifest.py

qualification-m8-correctness: m8-manifest-check demo-artifacts-check
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python -m scripts.qualify_m8_correctness

m8-correctness-check:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python -m scripts.qualify_m8_correctness --check

benchmark-m8-performance: m8-manifest-check
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python -m benchmarks.scripts.m8_performance run
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python -m benchmarks.scripts.m8_performance followup-variance

m8-performance-check:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python -m benchmarks.scripts.m8_performance check

qualification-m8-release: m8-manifest-check
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python -m scripts.qualify_m8_release

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
