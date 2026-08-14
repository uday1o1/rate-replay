FROM node@sha256:2c87ef9bd3c6a3bd4b472b4bec2ce9d16354b0c574f736c476489d09f560a203 AS node

FROM python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

ARG TARGETARCH
ENV UV_CACHE_DIR=/tmp/rate-replay-uv-cache \
    PNPM_STORE_DIR=/tmp/rate-replay-pnpm-store \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
       ca-certificates \
       curl \
       git \
       make \
       ripgrep \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/corepack /usr/local/lib/node_modules/corepack
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
    && ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && corepack enable \
    && python -m pip install --no-cache-dir uv==0.11.23

RUN case "${TARGETARCH}" in \
      arm64) \
        compose_arch=aarch64; \
        compose_sha256=fc5d1371f1ec7987e703da94ede49af3fbfb240b83f22991a98511de7bc4b93b \
        ;; \
      amd64) \
        compose_arch=x86_64; \
        compose_sha256=837fd1d35bf6a494f41b5b5988269a7be79de337cf1a1a6ff0e45ab51bb4e9be \
        ;; \
      *) echo "Unsupported qualification architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl --fail --location --silent --show-error \
       "https://github.com/docker/compose/releases/download/v5.4.0/docker-compose-linux-${compose_arch}" \
       --output /usr/local/bin/docker-compose \
    && echo "${compose_sha256}  /usr/local/bin/docker-compose" | sha256sum --check --strict \
    && chmod 0755 /usr/local/bin/docker-compose

WORKDIR /workspace
COPY . /workspace

CMD ["sh", "-c", "make bootstrap && corepack pnpm --filter @ratereplay/web exec playwright install --with-deps chromium && make check && make dependency-audit && make qualification-m3 && make qualification-m4"]
