# syntax=docker/dockerfile:1
# Verifier image: Python. Build with the verifier/ directory as the context:
#   docker build -f verifier/images/python/Dockerfile verifier/
# The base is pinned by the digest of the multi-arch index (amd64 and arm64).
FROM python:3.14-slim-trixie@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966

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
      --pip-source 26.2.2=b3b696fe2122f2339d782517559bfade2cb6b879 \
      --min urllib3==2.8.0 \
      --min msgpack==1.2.3 \
      --build-from-sdist msgpack

# No directory the unprivileged user can write may be one root executes from or has on its PATH
# (the runner is root, the repository's commands are not): the base's own world-writable
# directories lose their write bits. verifier/test/image_audit.sh proves it in the built image.
RUN --mount=type=bind,source=harden-writable.sh,target=/tmp/harden-writable.sh \
    bash /tmp/harden-writable.sh sandbox

COPY run.sh /usr/local/bin/verifier-run
COPY images/python/step.env /etc/verifier/step.env
RUN chmod 0755 /usr/local/bin/verifier-run

ENV VERIFIER_RUNTIME=python
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/verifier-run"]
