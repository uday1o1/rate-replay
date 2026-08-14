FROM chrislusf/seaweedfs@sha256:52194fba4fecd0083c842158b3a902ba6e04a63619b2b0efcd08007bdb6a4602

ARG RATEREPLAY_SOURCE_COMMIT=unknown
LABEL org.opencontainers.image.revision="${RATEREPLAY_SOURCE_COMMIT}" \
      org.opencontainers.image.source="https://github.com/uday1o1/rate-replay"
COPY ops/seaweed/entrypoint.sh /usr/local/bin/ratereplay-object-store
RUN chmod 0555 /usr/local/bin/ratereplay-object-store
USER 1000:1000
ENTRYPOINT ["/usr/local/bin/ratereplay-object-store"]
