# syntax=docker/dockerfile:1
# Verifier image: PHP with Composer. Build with the verifier/ directory as the
# context:
#   docker build -f verifier/images/php/Dockerfile verifier/
# Both bases are pinned by the digest of their multi-arch index (amd64 and arm64).
FROM composer:2@sha256:4ed71c38c6d788eb4e4e3747b5332f5b40d5b5a6fcadd5062a1a70986a118e0a AS composer

FROM php:8.5-cli-trixie@sha256:9236fd373d3bcda0659cf94838a744bd79791419292958963f8c6b34a0071554

ENV DEBIAN_FRONTEND=noninteractive
# The Debian security archive publishes fixes (linux-libc-dev, CVE-2026-99083 and a run of kernel
# header advisories) before the base image is rebuilt, and the gate blocks every fixable HIGH, so
# the installed packages are brought up to the archive's current ones before the tools are added.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends bash ca-certificates curl git jq util-linux coreutils build-essential \
    unzip libzip-dev libicu-dev libonig-dev libxml2-dev \
 && docker-php-ext-install -j"$(nproc)" intl zip bcmath pcntl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 --shell /bin/bash sandbox \
 && mkdir -p /work /etc/verifier \
 && chmod 0711 /work

# No directory the unprivileged user can write may be one root executes from or has on its PATH
# (the runner is root, the repository's commands are not): the base's own world-writable
# directories lose their write bits. verifier/test/image_audit.sh proves it in the built image.
RUN --mount=type=bind,source=harden-writable.sh,target=/tmp/harden-writable.sh \
    bash /tmp/harden-writable.sh sandbox

COPY --from=composer /usr/bin/composer /usr/local/bin/composer
COPY run.sh /usr/local/bin/verifier-run
COPY images/php/step.env /etc/verifier/step.env
RUN chmod 0755 /usr/local/bin/verifier-run

ENV VERIFIER_RUNTIME=php
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/verifier-run"]
