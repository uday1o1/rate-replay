FROM postgres@sha256:44c4ee9810eff91f7eab4d822642e01115b1a9eccce4bcbdde7604752d68eac6

ARG RATEREPLAY_SOURCE_COMMIT=unknown
LABEL org.opencontainers.image.revision="${RATEREPLAY_SOURCE_COMMIT}" \
      org.opencontainers.image.source="https://github.com/uday1o1/rate-replay"
RUN rm /usr/local/bin/gosu
USER 70:70
