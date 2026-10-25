# syntax=docker/dockerfile:1
# Verifier image: Node, for JavaScript and TypeScript repositories. Build with the
# verifier/ directory as the context:
#   docker build -f verifier/images/node/Dockerfile verifier/
# The base is pinned by the digest of the multi-arch index (amd64 and arm64).
FROM node:26-trixie-slim@sha256:36793bcbb160a6a16cede1e62b93f84e26b78a972a8a4c0b879c1c12f22761ec

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends bash ca-certificates curl git jq util-linux coreutils build-essential python3 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 --shell /bin/bash sandbox \
 && mkdir -p /work /etc/verifier \
 && chmod 0711 /work \
 && corepack enable

# The base's npm bundles libraries an image scan reports (fixable HIGH findings), and no
# npm release carries the fixed ones yet. Floors: the first release of each major line
# that fixes every advisory open against it, each with the sha512 its tarball must have (the
# registry's own integrity for that release, which the script checks against the download).
# Once the base's npm is newer this does nothing.
RUN --mount=type=bind,source=images/node/fix-npm-bundled.sh,target=/tmp/fix-npm-bundled.sh \
    bash /tmp/fix-npm-bundled.sh \
      --min brace-expansion@5.0.11=sha512-awigjhi6cLTh90bdw6+QJ9CtmJmyYhEIi70iCbc8Rozn04Fw9FeQIBjv/E22FFGuCGx1bLJyUfB64x/szUSXUg== \
      --min ip-address@10.3.1=sha512-1e9d3kb97NHJTIJDZW9rKqW2h6+dFa50Dy0fpPSMQp2ADje5gvKsXmdiK6dwY5t76TaTt5+P5N1Y/LoToIxP6g== \
      --min tar@7.5.21=sha512-XdhtCvlMywwxpCW8YEq3lOXBJpUPTR2OHHcwLPO3HwsJqOHa2Ok/oJ7ruGzp+JrKoRPVCzJwAdEjqLW/vNRPHA== \
      --min undici@6.28.1=sha512-zWpdTVD54H48CIybL0rWQ3ukpb9d23wM7eH5RtfdmeP70cWHNjtfo7P4vZX+5CoDcO53J4Pu5uXp7lNfjc6DRA==

# No directory the unprivileged user can write may be one root executes from or has on its PATH
# (the runner is root, the repository's commands are not): the base's own world-writable
# directories lose their write bits. verifier/test/image_audit.sh proves it in the built image.
RUN --mount=type=bind,source=harden-writable.sh,target=/tmp/harden-writable.sh \
    bash /tmp/harden-writable.sh sandbox

COPY run.sh /usr/local/bin/verifier-run
COPY images/node/step.env /etc/verifier/step.env
RUN chmod 0755 /usr/local/bin/verifier-run

ENV VERIFIER_RUNTIME=node
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/verifier-run"]
