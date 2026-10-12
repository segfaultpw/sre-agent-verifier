#!/usr/bin/env bash
# Raises the libraries npm bundles (npm/node_modules) in the node image. npm ships its
# dependencies inside its own package, so a base image carries whichever versions the npm
# release of the day was built with, and no npm release (the latest 11 and 12 included)
# has yet moved to the fixed release of every library an image scanner flags in it.
#
#   fix-npm-bundled.sh --min NAME@VERSION=INTEGRITY [--min NAME@VERSION=INTEGRITY]...
#
# Every copy of NAME under the global node_modules whose version is below VERSION, and
# on the same major line (a fix for 5.x says nothing about a nested 2.x), is replaced by
# exactly VERSION from the registry. INTEGRITY is the sha512 the release must have, in
# the form npm prints (sha512-<base64>): the downloaded tarball is hashed here and the
# build stops on a mismatch before that release replaces anything, so the registry alone
# is not trusted for the bytes (floors are processed in order, so the copies of an earlier
# floor are already replaced when a later one fails; the failed build's layer is discarded).
# Copies that are already new enough are left alone, and then
# nothing is downloaded: the step goes quiet once the base image's npm catches up. The tree
# is checked afterwards: every bundled library still has the dependencies it declares, and
# npm still packs and installs a package, one made inside the build, so no third-party code
# is fetched or run to prove it.

set -euo pipefail

floors=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --min)
      [[ "${2:-}" =~ ^(@?[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)?)@([0-9]+\.[0-9]+\.[0-9]+)=(sha512-[A-Za-z0-9+/]{86}==)$ ]] || {
        echo "--min takes NAME@VERSION=sha512-<base64 integrity>, got '${2:-}'" >&2
        exit 2
      }
      floors+=("$2")
      shift 2
      ;;
    *)
      echo "usage: $0 --min NAME@VERSION=INTEGRITY [--min NAME@VERSION=INTEGRITY]..." >&2
      exit 2
      ;;
  esac
done

if [ "${#floors[@]}" -eq 0 ]; then
  echo "nothing to do: give at least one --min" >&2
  exit 2
fi

export npm_config_update_notifier=false npm_config_fund=false npm_config_audit=false npm_config_ignore_scripts=true

# a < b, for the forms a floor takes (MAJOR.MINOR.PATCH): the release parts compare as
# numbers, and a pre-release of the floor (5.0.11-beta.1, 6.28.1-rc.0, 7.5.21-1) is below it,
# as semver orders them. Build metadata (+build.5) does not count.
version_lt() {
  local a=${1%%+*} b=${2%%+*}
  [[ "$a" =~ ^([0-9]+(\.[0-9]+)*)(.*)$ ]] || return 1
  local release_a=${BASH_REMATCH[1]} suffix_a=${BASH_REMATCH[3]}
  [[ "$b" =~ ^([0-9]+(\.[0-9]+)*)(.*)$ ]] || return 1
  local release_b=${BASH_REMATCH[1]} suffix_b=${BASH_REMATCH[3]}
  if [ "$release_a" != "$release_b" ]; then
    [ "$(printf '%s\n%s\n' "$release_a" "$release_b" | sort -V | head -n 1)" = "$release_a" ]
    return
  fi
  [ -n "$suffix_a" ] && [ -z "$suffix_b" ]
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
# npm keeps its cache and its debug logs under the cache directory, and Node its module
# compile cache under the temporary directory: in the step's own directory they go with it,
# instead of ending up in the image layer
export npm_config_cache="$work/npm-cache"
mkdir "$work/tmp"
export TMPDIR="$work/tmp"

global_root=$(npm root -g)
npm_dir="$global_root/npm"
[ -d "$npm_dir/node_modules" ] || {
  echo "no bundled npm under $global_root" >&2
  exit 1
}

changed=0
for floor in "${floors[@]}"; do
  integrity=${floor#*=}
  spec=${floor%%=*}
  name=${spec%@*}
  want=${spec##*@}
  major=${want%%.*}
  tarball=
  while IFS= read -r package_json; do
    read -r found have < <(jq -r '[.name // "-", .version // "-"] | @tsv' "$package_json")
    [ "$found" = "$name" ] || continue
    [ "${have%%.*}" = "$major" ] || continue
    version_lt "$have" "$want" || continue
    dir=$(dirname "$package_json")
    echo "raising ${dir#"$global_root"/} $have -> $want"
    if [ -z "$tarball" ]; then
      tarball="$work/$(npm pack --silent --json --pack-destination "$work" "$name@$want" | jq -r '.[0].filename')"
      got="sha512-$(openssl dgst -sha512 -binary "$tarball" | openssl base64 -A)"
      [ "$got" = "$integrity" ] || {
        echo "$name@$want does not have the pinned integrity: expected $integrity, got $got" >&2
        exit 1
      }
    fi
    # the copy's own node_modules stay: they are other libraries
    find "$dir" -mindepth 1 -maxdepth 1 ! -name node_modules -exec rm -rf {} +
    tar -xzf "$tarball" --strip-components=1 --no-same-owner -C "$dir"
    [ "$(jq -r .version "$dir/package.json")" = "$want" ] || {
      echo "$dir is not $want after the replacement" >&2
      exit 1
    }
    changed=1
  done < <(find "$global_root" -type f -path "*/node_modules/$name/package.json")
done

if [ "$changed" -eq 0 ]; then
  echo "npm $(npm --version) already carries every requested version; nothing to change"
  exit 0
fi

# a library whose declared dependencies the tree no longer satisfies fails here
(cd "$npm_dir" && npm ls --all --omit=dev >/dev/null)

npm --version

# npm packs and installs a package made right here: nothing from the registry, nothing
# third-party, and npm ls proves what was installed
mkdir -p "$work/smoke/pkg" "$work/smoke/app"
(
  cd "$work/smoke/pkg"
  npm init --yes >/dev/null
  echo 'module.exports = 1' >index.js
  npm pack --silent >/dev/null
  cd ../app
  npm init --yes >/dev/null
  npm install --silent ../pkg/pkg-1.0.0.tgz
  npm ls --silent
  test -f node_modules/pkg/index.js
)
