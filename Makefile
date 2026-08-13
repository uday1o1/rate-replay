SHELL := /bin/sh
UV_CACHE_DIR ?= /private/tmp/rate-replay-uv-cache
PNPM_STORE_DIR ?= /private/tmp/rate-replay-pnpm-store
COMPOSE ?= $(shell command -v docker-compose >/dev/null 2>&1 && echo docker-compose || echo docker compose)

.PHONY: bootstrap check format format-check lint typecheck test security dependency-audit web-build compose-config clean-checkout-check

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
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run mypy apps packages scripts
	corepack pnpm typecheck

test:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run pytest
	corepack pnpm test

security:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run bandit -c pyproject.toml -r apps packages scripts
	./scripts/scan-secrets.sh

dependency-audit:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run pip-audit --strict
	corepack pnpm audit --audit-level high

web-build:
	corepack pnpm build

compose-config:
	$(COMPOSE) -f compose.yaml config --quiet

check: format-check lint typecheck test security web-build compose-config

clean-checkout-check:
	./scripts/clean-checkout-check.sh
