# syntax=docker/dockerfile:1
# Verifier image: Go. Build with the verifier/ directory as the context:
#   docker build -f verifier/images/go/Dockerfile verifier/
# The base is pinned by the digest of the multi-arch index (amd64 and arm64) of the floating
# 1.26 tag, which its publisher re-points at each patch release, so the weekly refresh keeps
# patches arriving. The toolchain must be Go 1.26.10 or later (the release that fixes the Go
# standard library advisories the scan reported against the earlier 1.26 patch releases);
# verifier/test/contract.sh fails the build of this image when `go version` is older.
FROM golang:1.27-trixie@sha256:c65ecfe613b539ac74c62a4c3b2c27e8f8a6076384c8f27e8a3c0f3be17cc385

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends bash ca-certificates curl git jq util-linux coreutils build-essential \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 --shell /bin/bash sandbox \
 && mkdir -p /work /etc/verifier \
 && chmod 0711 /work

# No directory the unprivileged user can write may be one root executes from or has on its PATH
# (the runner is root, the repository's commands are not): the base's own world-writable
# directories lose their write bits. verifier/test/image_audit.sh proves it in the built image.
RUN --mount=type=bind,source=harden-writable.sh,target=/tmp/harden-writable.sh \
    bash /tmp/harden-writable.sh sandbox

COPY run.sh /usr/local/bin/verifier-run
COPY images/go/step.env /etc/verifier/step.env
RUN chmod 0755 /usr/local/bin/verifier-run

ENV VERIFIER_RUNTIME=go
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/verifier-run"]
