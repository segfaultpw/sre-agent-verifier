# syntax=docker/dockerfile:1
# Verifier image: Python. Build with the verifier/ directory as the context:
#   docker build -f verifier/images/python/Dockerfile verifier/
# The base is pinned by the digest of the multi-arch index (amd64 and arm64).
FROM python:3.14-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends bash ca-certificates curl git jq util-linux coreutils build-essential \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 --shell /bin/bash sandbox \
 && mkdir -p /work /etc/verifier \
 && chmod 0711 /work

# pip bundles libraries (pip/_vendor) that an image scan reports as installed packages
# (fixable HIGH findings), and no pip release carries the fixed ones yet. Floors: the first
# release that fixes every advisory open against it. Nothing this step fetches is trusted by
# name: the pip source is pinned by commit, the build tools and the bundled libraries by the
# hashes in the two .lock files next to the script (see the script's header). The one finding
# this cannot fix is setuptools 70.3.0, which is pip's pkg_resources (pip-tools and other
# tools import it, so it stays): a scoped exception for exactly that advisory on exactly that
# package is the OpenVEX document in this directory, which the workflow hands to the scan.
RUN --mount=type=bind,source=images/python,target=/tmp/pip-fix \
    bash /tmp/pip-fix/fix-pip-vendored.sh \
      --pip-source 26.2.1=634a6ec1a5d9dcc2433571cdb2f4c58a4bb29caf \
      --min urllib3==2.8.0 \
      --min msgpack==1.2.1 \
      --build-from-sdist msgpack

COPY run.sh /usr/local/bin/verifier-run
COPY images/python/step.env /etc/verifier/step.env
RUN chmod 0755 /usr/local/bin/verifier-run

ENV VERIFIER_RUNTIME=python
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/verifier-run"]
