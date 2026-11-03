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
# The Debian security archive publishes fixes (linux-libc-dev, CVE-2026-99083 and a run of kernel
# header advisories) before the base image is rebuilt, and the gate blocks every fixable HIGH, so
# the installed packages are brought up to the archive's current ones before the tools are added.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends bash ca-certificates curl git jq util-linux coreutils build-essential \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 --shell /bin/bash sandbox \
 && mkdir -p /work /etc/verifier \
 && chmod 0711 /work

# semgrep, for the runner's scan mode and a repository profile's `analysis` commands: a pinned,
# hash-checked install under /opt/semgrep with its own standalone Python (python-standalone.lock,
# semgrep.lock; see the script's header), `/usr/local/bin/semgrep` a root-owned wrapper. Nothing
# is fetched when it runs, and it parses files without running them.
RUN --mount=type=bind,source=semgrep,target=/tmp/semgrep-install \
    bash /tmp/semgrep-install/install-semgrep.sh --python 3.13.16+20261013 --semgrep 1.183.0

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
