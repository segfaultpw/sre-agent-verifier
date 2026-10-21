# syntax=docker/dockerfile:1
# Verifier image: Elixir. Build with the verifier/ directory as the context:
#   docker build -f verifier/images/elixir/Dockerfile verifier/
# The base is pinned by the digest of the multi-arch index (amd64 and arm64), of a tag
# the publisher re-points (the official image is rebuilt on every Debian update), so
# the weekly refresh (verifier/refresh-bases.sh) and Dependabot can move it. The
# platform's own builder uses hexpm/elixir, whose tags name a dated build and never move;
# that is why this image does not. Same Elixir and OTP major as the platform builds with.
FROM elixir:1.20.4-otp-29-slim@sha256:5a0b4f07258208e101486b4bf6d55b7a194622fd10bf6c352602de7a37805637

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends bash ca-certificates curl git jq util-linux coreutils build-essential \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 --shell /bin/bash sandbox \
 && mkdir -p /work /etc/verifier \
 && chmod 0711 /work

COPY run.sh /usr/local/bin/verifier-run
COPY images/elixir/step.env /etc/verifier/step.env
RUN chmod 0755 /usr/local/bin/verifier-run

# Hex and rebar are installed for the unprivileged user, which is the one that
# runs every command of the repository under test.
USER sandbox
RUN mix local.hex --force && mix local.rebar --force
USER root

# No directory the unprivileged user can write may be one root executes from or has on its PATH
# (the runner is root, the repository's commands are not): the base's own world-writable
# directories lose their write bits. verifier/test/image_audit.sh proves it in the built image.
RUN --mount=type=bind,source=harden-writable.sh,target=/tmp/harden-writable.sh \
    bash /tmp/harden-writable.sh sandbox

ENV VERIFIER_RUNTIME=elixir
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/verifier-run"]
