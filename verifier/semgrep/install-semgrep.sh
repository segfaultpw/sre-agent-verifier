#!/usr/bin/env bash
# Installs semgrep, the static analysis tool the runner's scan mode and a repository profile's
# `analysis` commands use, into a verifier image. Run once per image as root, after the
# packages and before harden-writable.sh.
#
#   install-semgrep.sh --python VERSION+TAG --semgrep VERSION
#
#   --python    the python-build-standalone CPython to run semgrep with, as VERSION+RELEASE
#               (3.13.16+20261013): its install_only_stripped build for this machine, whose
#               sha256 is in python-standalone.lock
#   --semgrep   the semgrep release; semgrep.lock must pin exactly it
#
# What it does, and what it never does:
#
#   * The interpreter is installed under /opt/semgrep/python, away from the image's own Python
#     (five of the six images have none), so a repository's commands see no new `python3`.
#   * Everything fetched is pinned and checked: the interpreter by the sha256 in
#     python-standalone.lock (verified before anything is unpacked, and its listing checked),
#     every Python package by the hashes in semgrep.lock (pip --require-hashes --only-binary
#     :all: --no-deps, so nothing is resolved here and no build step runs). The set that ended
#     up installed is then compared with the lock (check-installed.py).
#   * pip is removed afterwards, with the standard library's test suite and the interpreter's
#     headers: the image keeps no package installer and nothing it would be scanned for.
#   * Everything is owned by root and closed to group and other. /usr/local/bin/semgrep is a
#     root-owned wrapper that runs semgrep's own entry point under `python -I` (isolated mode: no
#     PYTHON* variable, no user site directory and no current directory or script directory on
#     the import path, so a repository's working tree cannot supply a module; the same holds for
#     the `pysemgrep` it starts, through PYTHONNOUSERSITE and PYTHONSAFEPATH) with metrics and the
#     version check switched off. Nothing is fetched when semgrep runs.
#   * semgrep parses the files it is pointed at and never runs them. The runner still starts it
#     as the unprivileged user, in the scan's own session, with no network.
#
# When /opt/semgrep already holds this exact install, nothing is fetched or changed.
#
# Test knobs (never set by an image build): SEMGREP_PREFIX (default /opt/semgrep),
# SEMGREP_BIN_DIR (default /usr/local/bin) and SEMGREP_FORCE_ARCH (x86_64 or aarch64).

set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
prefix=${SEMGREP_PREFIX:-/opt/semgrep}
bin_dir=${SEMGREP_BIN_DIR:-/usr/local/bin}
release_base=https://github.com/astral-sh/python-build-standalone/releases/download

fail() {
  echo "$*" >&2
  exit 1
}

python=""
semgrep=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --python)
      [[ "${2:-}" =~ ^3\.[0-9]+\.[0-9]+\+[0-9]{8}$ ]] || {
        echo "--python takes VERSION+RELEASE like 3.13.16+20261013, got '${2:-}'" >&2
        exit 2
      }
      python=$2
      shift 2
      ;;
    --semgrep)
      [[ "${2:-}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
        echo "--semgrep takes a release like 1.183.0, got '${2:-}'" >&2
        exit 2
      }
      semgrep=$2
      shift 2
      ;;
    *)
      echo "usage: $0 --python VERSION+RELEASE --semgrep VERSION" >&2
      exit 2
      ;;
  esac
done
if [ -z "$python" ] || [ -z "$semgrep" ]; then
  echo "usage: $0 --python VERSION+RELEASE --semgrep VERSION" >&2
  exit 2
fi

awk -v pin="semgrep==$semgrep" '$1 == pin {found = 1} END {exit !found}' "$here/semgrep.lock" ||
  fail "semgrep.lock does not pin semgrep $semgrep: regenerate it, see 'Bumping semgrep' in verifier/contract.md"

arch=${SEMGREP_FORCE_ARCH:-$(uname -m)}
case "$arch" in
  x86_64 | amd64) arch=x86_64 ;;
  aarch64 | arm64) arch=aarch64 ;;
  *) fail "no standalone Python is pinned for the architecture '$arch'" ;;
esac
file="cpython-$python-$arch-unknown-linux-gnu-install_only_stripped.tar.gz"
want=$(awk -v f="$file" '$1 ~ /^[0-9a-f]+$/ && $2 == f {print $1}' "$here/python-standalone.lock")
[[ "$want" =~ ^[0-9a-f]{64}$ ]] ||
  fail "python-standalone.lock has no sha256 for $file: add it from the release's SHA256SUMS, see 'Bumping semgrep' in verifier/contract.md"

pins="python=$python semgrep=$semgrep"
if [ -f "$prefix/.pins" ] && [ "$(cat "$prefix/.pins")" = "$pins" ] && [ -x "$bin_dir/semgrep" ]; then
  echo "semgrep $semgrep on Python $python is already installed; nothing to change"
  exit 0
fi
[ ! -e "$prefix" ] || fail "$prefix exists and is not the install of $pins; remove it first"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir "$work/tmp" "$work/home"
export TMPDIR="$work/tmp" HOME="$work/home"

tag=${python#*+}
curl --proto '=https' --tlsv1.2 -fsSL --retry 3 -o "$work/python.tar.gz" "$release_base/$tag/${file//+/%2B}"
got=$(sha256sum "$work/python.tar.gz" | cut -d' ' -f1)
[ "$got" = "$want" ] || fail "$file does not have the pinned sha256: expected $want, got $got"

while IFS= read -r entry; do
  case "$entry" in
    python | python/*) ;;
    *) fail "$file holds '$entry', outside python/" ;;
  esac
  case "/$entry/" in
    */../*) fail "$file holds '$entry', which climbs out of python/" ;;
  esac
done < <(tar -tzf "$work/python.tar.gz")

# Unpacked in place, so the console scripts pip writes carry the final interpreter path.
install -d -m 0755 "$prefix"
tar -xzf "$work/python.tar.gz" -C "$prefix" --no-same-owner --no-same-permissions
py="$prefix/python/bin/python3"
[ -x "$py" ] || fail "$file has no python/bin/python3"
have=$("$py" -I -c 'import platform; print(platform.python_version())')
[ "$have" = "${python%%+*}" ] || fail "the unpacked Python is $have, not ${python%%+*}"

"$py" -I -m pip --isolated --disable-pip-version-check --no-input --no-cache-dir install \
  --require-hashes --only-binary :all: --no-deps --no-warn-script-location --root-user-action ignore \
  -r "$here/semgrep.lock"
"$py" -I -m pip --isolated --disable-pip-version-check check

site=$("$py" -I -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
"$py" -I "$here/check-installed.py" "$here/semgrep.lock" "$site" --ignore pip ||
  fail "what was installed is not what semgrep.lock pins (above)"

"$py" -I -m pip --isolated --disable-pip-version-check uninstall --yes pip
stdlib=$("$py" -I -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')
rm -rf "$stdlib/test" "$stdlib/idlelib" "$stdlib/turtledemo" "$stdlib/ensurepip/_bundled" \
  "$prefix/python/include" "$prefix/python/share"
rm -f "$prefix/python/bin"/pip "$prefix/python/bin"/pip3 "$prefix/python/bin"/pip3.* \
  "$prefix/python/bin"/idle3 "$prefix/python/bin"/idle3.* "$prefix/python/bin"/pydoc3 "$prefix/python/bin"/pydoc3.*
[ ! -e "$site/pip" ] || fail "pip is still in $site after its removal"

printf '%s\n' "$pins" >"$prefix/.pins"
chmod -R go-w "$prefix"
if [ "$(id -u)" = 0 ]; then chown -R 0:0 "$prefix"; fi

cat >"$bin_dir/semgrep" <<WRAPPER
#!/bin/sh
SEMGREP_SEND_METRICS=off
SEMGREP_ENABLE_VERSION_CHECK=0
PYTHONNOUSERSITE=1
PYTHONSAFEPATH=1
PYTHONDONTWRITEBYTECODE=1
export SEMGREP_SEND_METRICS SEMGREP_ENABLE_VERSION_CHECK PYTHONNOUSERSITE PYTHONSAFEPATH PYTHONDONTWRITEBYTECODE
exec $py -I $prefix/python/bin/semgrep "\$@"
WRAPPER
chmod 0755 "$bin_dir/semgrep"
if [ "$(id -u)" = 0 ]; then chown 0:0 "$bin_dir/semgrep"; fi

# It starts, it is the pinned release, and it finds what a rule says in a file, with nothing
# fetched and nothing run (a rule file and one source file of this step's own).
[ "$("$bin_dir/semgrep" --version | tail -n 1)" = "$semgrep" ] || fail "semgrep reports another version than $semgrep"
mkdir "$work/smoke"
cat >"$work/smoke/rule.yml" <<'RULE'
rules:
  - id: sre-install-smoke
    languages: [python]
    severity: WARNING
    message: install smoke test
    pattern: install_smoke_target($X)
RULE
printf 'install_smoke_target(1)\n' >"$work/smoke/a.py"
(
  cd "$work/smoke"
  "$bin_dir/semgrep" scan --config rule.yml --json --quiet --metrics=off --disable-version-check \
    --oss-only --no-rewrite-rule-ids . >"$work/smoke.json"
)
jq -e '(.results | length) == 1 and .results[0].check_id == "sre-install-smoke"' "$work/smoke.json" >/dev/null ||
  fail "semgrep did not find the rule's match in the smoke file"
echo "installed semgrep $semgrep on Python $python under $prefix"
