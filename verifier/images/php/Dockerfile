# syntax=docker/dockerfile:1
# Verifier image: PHP with Composer. Build with the verifier/ directory as the
# context:
#   docker build -f verifier/images/php/Dockerfile verifier/
# Both bases are pinned by the digest of their multi-arch index (amd64 and arm64).
FROM composer:2@sha256:af98f42dfff7c68ba8d53c2164fd9fde1087b7d449514baa38c418b1f6bc4bac AS composer

FROM php:8.5-cli-trixie@sha256:01a109229f4465bc9ef042d9198f09a4d9da7775a825dbd8572dcdbd8756c4d3

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
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
