FROM node@sha256:2c87ef9bd3c6a3bd4b472b4bec2ce9d16354b0c574f736c476489d09f560a203 AS builder

ENV PNPM_HOME=/pnpm
ENV PATH="${PNPM_HOME}:${PATH}"
WORKDIR /app
RUN corepack enable && corepack install --global pnpm@11.21.0
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml /app/
COPY apps/web /app/apps/web
COPY artifacts/demo /app/artifacts/demo
RUN pnpm install --frozen-lockfile \
    && pnpm --filter @ratereplay/web build

FROM caddy@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648 AS runtime

ARG RATEREPLAY_SOURCE_COMMIT=unknown
LABEL org.opencontainers.image.revision="${RATEREPLAY_SOURCE_COMMIT}" \
      org.opencontainers.image.source="https://github.com/uday1o1/rate-replay"
RUN addgroup -S -g 10001 ratereplay \
    && adduser -S -D -H -u 10001 -G ratereplay ratereplay \
    && setcap -r /usr/bin/caddy \
    && chown -R 10001:10001 /config /data
COPY --chown=10001:10001 ops/caddy/web.Caddyfile /etc/caddy/Caddyfile
COPY --from=builder --chown=10001:10001 /app/apps/web/dist /srv
USER 10001:10001
