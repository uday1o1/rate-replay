FROM postgres@sha256:cac8243312724d445e81848fef5614bccfaebee4bd53c99033378cb478db8d99 AS postgres-client

FROM python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS builder

ENV PATH="/app/.venv/bin:/usr/local/bin:${PATH}" \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
WORKDIR /app
RUN python -m pip install --no-cache-dir uv==0.11.23
COPY pyproject.toml uv.lock README.md LICENSE /app/
RUN uv sync --frozen --no-dev --no-editable --no-install-project \
    && rm -rf /tmp/uv-cache
COPY alembic.ini pnpm-lock.yaml /app/
COPY apps/api /app/apps/api
COPY apps/worker /app/apps/worker
COPY artifacts/demo /app/artifacts/demo
COPY data /app/data
COPY migrations /app/migrations
COPY packages /app/packages
COPY tariffs /app/tariffs
COPY third_party /app/third_party
RUN uv sync --frozen --no-dev --no-editable \
    && rm -rf /tmp/uv-cache

FROM python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS runtime

ARG RATEREPLAY_SOURCE_COMMIT=unknown
LABEL org.opencontainers.image.revision="${RATEREPLAY_SOURCE_COMMIT}" \
      org.opencontainers.image.source="https://github.com/uday1o1/rate-replay"
RUN apt-get update \
    && apt-get install --yes --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 ratereplay \
    && useradd --no-log-init --uid 10001 --gid 10001 --home-dir /nonexistent \
       --shell /usr/sbin/nologin ratereplay \
    && install -d -m 0700 -o ratereplay -g ratereplay \
       /var/lib/ratereplay \
       /var/lib/ratereplay/deletion-ledger \
    && dpkg --purge --force-depends --force-remove-essential perl-base
COPY --from=postgres-client /usr/lib/postgresql/16/bin/pg_dump /usr/local/bin/pg_dump
COPY --from=postgres-client /usr/lib/postgresql/16/bin/pg_restore /usr/local/bin/pg_restore
COPY --chown=10001:10001 ops/application/entrypoint.sh /usr/local/bin/ratereplay-entrypoint
RUN chmod 0555 /usr/local/bin/ratereplay-entrypoint
COPY --from=builder --chown=10001:10001 /app /app
ENV PATH="/app/.venv/bin:/usr/local/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RATEREPLAY_REPOSITORY_ROOT=/app
WORKDIR /app
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/ratereplay-entrypoint"]
