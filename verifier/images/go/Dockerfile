# syntax=docker/dockerfile:1
# Verifier image: Go. Build with the verifier/ directory as the context:
#   docker build -f verifier/images/go/Dockerfile verifier/
# The base is pinned by the digest of the multi-arch index (amd64 and arm64), and the
# toolchain to Go 1.26.9: the release that fixes the Go standard library advisories the
# scan reported against the earlier 1.26 patch releases. Dependabot moves the patch version.
FROM golang:1.26.9-trixie@sha256:f06b7122f888680048afb247c4e5f49c0069eb6c4c1ccae0f3f4834527a8d22c

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends bash ca-certificates curl git jq util-linux coreutils build-essential \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 --shell /bin/bash sandbox \
 && mkdir -p /work /etc/verifier \
 && chmod 0711 /work

COPY run.sh /usr/local/bin/verifier-run
COPY images/go/step.env /etc/verifier/step.env
RUN chmod 0755 /usr/local/bin/verifier-run

ENV VERIFIER_RUNTIME=go
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/verifier-run"]
