#!/usr/bin/env bash
# Re-resolves the digest every FROM line of the given Dockerfiles pins, from the tag
# the line names, and rewrites the file in place:
#
#   FROM node:24-trixie-slim@sha256:<old>   ->   FROM node:24-trixie-slim@sha256:<today's>
#
# The images pin their bases by digest so a build is reproducible, which is also what
# stops a rebuild from picking up a fixed base. The weekly rebuild
# (.github/workflows/verifier-images.yml) runs this before it builds, on the runner's
# own checkout and without committing anything; Dependabot proposes the same change as
# a reviewed pull request. The digest is the one of the tag's multi-arch index, so the
# amd64 and arm64 builds resolve the same base.
#
# Usage: verifier/refresh-bases.sh DOCKERFILE [DOCKERFILE ...]
# Needs `docker buildx imagetools`, jq, sha256sum and network access to the registries.

set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "usage: $0 DOCKERFILE [DOCKERFILE ...]" >&2
  exit 2
fi

from_line='^(FROM[[:space:]]+)([^@[:space:]]+)@sha256:[0-9a-f]{64}(.*)$'

# resolve_digest REF: prints the digest of the manifest (or index) REF names today. The CI
# runner's buildx printed its default listing whatever --format asked for (the weekly rebuild
# of 2026-10-19 failed on every image), so when --format does not answer a digest the digest is
# computed the way the registry defines it, the sha256 of the manifest's own bytes, which --raw
# prints. Those bytes must be a manifest, or nothing is answered: hashing an error message would
# produce a well-formed digest of nothing.
resolve_digest() {
  local ref=$1 out raw
  out=$(docker buildx imagetools inspect "$ref" --format '{{.Manifest.Digest}}' 2>/dev/null) || out=""
  if [[ "$out" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    printf '%s' "$out"
    return 0
  fi
  raw=$(mktemp)
  if docker buildx imagetools inspect "$ref" --raw >"$raw" 2>/dev/null &&
    jq -e '.schemaVersion == 2 and (.manifests != null or .layers != null)' "$raw" >/dev/null 2>&1; then
    printf 'sha256:%s' "$(sha256sum "$raw" | cut -c1-64)"
  else
    printf '%s' "$out"
  fi
  rm -f "$raw"
}

for file in "$@"; do
  [ -f "$file" ] || {
    echo "no such file: $file" >&2
    exit 2
  }
  tmp=$(mktemp)
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ "$line" =~ $from_line ]]; then
      prefix=${BASH_REMATCH[1]}
      ref=${BASH_REMATCH[2]}
      rest=${BASH_REMATCH[3]}
      digest=$(resolve_digest "$ref")
      if ! [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        echo "could not resolve a digest for $ref (got '$digest')" >&2
        rm -f "$tmp"
        exit 1
      fi
      line="${prefix}${ref}@${digest}${rest}"
    fi
    printf '%s\n' "$line"
  done <"$file" >"$tmp"
  cat "$tmp" >"$file"
  rm -f "$tmp"
done
