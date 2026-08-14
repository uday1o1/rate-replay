FROM caddy@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648

ARG RATEREPLAY_SOURCE_COMMIT=unknown
LABEL org.opencontainers.image.revision="${RATEREPLAY_SOURCE_COMMIT}" \
      org.opencontainers.image.source="https://github.com/uday1o1/rate-replay"
RUN addgroup -S -g 10001 ratereplay \
    && adduser -S -D -H -u 10001 -G ratereplay ratereplay \
    && setcap -r /usr/bin/caddy \
    && chown -R 10001:10001 /config /data
COPY --chown=10001:10001 ops/caddy/proxy.Caddyfile /etc/caddy/Caddyfile
USER 10001:10001
