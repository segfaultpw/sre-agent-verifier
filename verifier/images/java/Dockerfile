# syntax=docker/dockerfile:1
# Verifier image: Java (Temurin 21 LTS with Maven; Gradle repositories use their
# own wrapper). Build with the verifier/ directory as the context:
#   docker build -f verifier/images/java/Dockerfile verifier/
# The base is pinned by the digest of the multi-arch index (amd64 and arm64).
FROM maven:3-eclipse-temurin-25@sha256:9b5f82b7166db6112eb123ba310a9e010ec3fe95022ac0728f491121563dfbab

ENV DEBIAN_FRONTEND=noninteractive
# The Ubuntu base ships Canonical's pebble service manager at /usr/bin/pebble, a Go binary no
# package owns and nothing here runs; it lags its Go releases and failed the Trivy gate
# (stdlib 1.26.7, CVE-2026-78667), so it is removed rather than carried.
RUN apt-get update \
 && apt-get install -y --no-install-recommends bash ca-certificates curl git jq util-linux coreutils build-essential \
 && rm -rf /var/lib/apt/lists/* \
 && rm -rf /usr/bin/pebble /var/lib/pebble \
 && useradd --create-home --uid 10001 --shell /bin/bash sandbox \
 && mkdir -p /work /etc/verifier \
 && chmod 0711 /work

# No directory the unprivileged user can write may be one root executes from or has on its PATH
# (the runner is root, the repository's commands are not): the base's own world-writable
# directories lose their write bits. verifier/test/image_audit.sh proves it in the built image.
RUN --mount=type=bind,source=harden-writable.sh,target=/tmp/harden-writable.sh \
    bash /tmp/harden-writable.sh sandbox

COPY run.sh /usr/local/bin/verifier-run
COPY images/java/step.env /etc/verifier/step.env
RUN chmod 0755 /usr/local/bin/verifier-run

ENV VERIFIER_RUNTIME=java
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/verifier-run"]
