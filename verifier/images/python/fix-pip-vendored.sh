#!/usr/bin/env bash
# Raises the libraries pip bundles (pip/_vendor) in the python image. A pip release moves
# them only when its maintainers re-vendor, and an image scanner reads them out of pip's
# bom.cdx.json as if they were installed packages.
#
#   fix-pip-vendored.sh --pip-source VERSION=COMMIT --min NAME==VERSION [--min NAME==VERSION]...
#                       [--build-from-sdist NAME]...
#
#   --pip-source         the commit of pip's git repository that released VERSION; the installed
#                        pip's version must have one (a base image that moves pip fails here
#                        until somebody reviews the new commit and pins it)
#   --min                raise the bundled NAME to VERSION when the installed pip carries an older one
#   --build-from-sdist   NAME has no pure-Python wheel: build it from its hash-locked sdist
#
# It does what pip's own vendoring nox session does for an upgrade, on the pinned commit:
# edit src/pip/_vendor/vendor.txt, `vendoring sync` (pip's own tool, which applies pip's
# patches to the new copy and rewrites bom.cdx.json), then build the wheel the way pip
# builds a release (build-project/, hash-pinned build requirements). That wheel replaces the
# installed pip and the wheel `python -m venv` seeds a virtual environment from, so a
# repository's own venv carries the same fix. When the installed pip already carries every
# requested version nothing is fetched and nothing is changed.
#
# Everything the step fetches is pinned and checked, so a compromised index cannot put code
# into the image: the pip source by commit (verified after the fetch, and its vendor.txt must be
# the installed pip's), the vendoring tool and its dependencies from pip-fix-tools.lock and the
# bundled libraries from pip-vendored.lock (both with --require-hashes), pip's own build
# requirements by the hashes in its pylock.toml, and the final smoke download by hash. The sync
# runs with no index and a proxy that refuses everything, against a wheelhouse of the locked
# files, and the build stops if anything under src/pip/_vendor changed beyond the libraries
# asked for.

set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

declare -A floors=()
declare -A sources=()
sdists=()

fail() {
  echo "$*" >&2
  exit 1
}

normalize() {
  local name=${1,,}
  printf '%s' "${name//_/-}"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --min)
      [[ "${2:-}" =~ ^([A-Za-z0-9._-]+)==([0-9][A-Za-z0-9.]*)$ ]] || {
        echo "--min takes NAME==VERSION, got '${2:-}'" >&2
        exit 2
      }
      floors[$(normalize "${BASH_REMATCH[1]}")]=${BASH_REMATCH[2]}
      shift 2
      ;;
    --pip-source)
      [[ "${2:-}" =~ ^([0-9][A-Za-z0-9.]*)=([0-9a-f]{40})$ ]] || {
        echo "--pip-source takes VERSION=COMMIT (the full 40 character sha), got '${2:-}'" >&2
        exit 2
      }
      sources[${BASH_REMATCH[1]}]=${BASH_REMATCH[2]}
      shift 2
      ;;
    --build-from-sdist)
      [[ "${2:-}" =~ ^[A-Za-z0-9._-]+$ ]] || {
        echo "--build-from-sdist takes NAME, got '${2:-}'" >&2
        exit 2
      }
      sdists+=("$2")
      shift 2
      ;;
    *)
      echo "usage: $0 --pip-source VERSION=COMMIT --min NAME==VERSION [--min NAME==VERSION]... [--build-from-sdist NAME]..." >&2
      exit 2
      ;;
  esac
done

if [ "${#floors[@]}" -eq 0 ]; then
  echo "nothing to do: give at least one --min" >&2
  exit 2
fi

# msgpack's compiled extension is dropped from the vendored copy, so do not build it
export PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore MSGPACK_PUREPYTHON=1

# a < b, for the forms a floor takes (a plain release): the release parts compare as numbers,
# and a pre-release or dev release of the floor (2.8.0rc1, 2.8.0.dev0, 2.8.0a1) is below it,
# as PEP 440 orders them, while a post release (2.8.0.post1, 2.8.0-1) is above it. A local
# version (+build) does not count.
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
  [ -z "$suffix_b" ] || return 1
  [ -n "$suffix_a" ] || return 1
  [[ ! "$suffix_a" =~ ^([.-]?(post|rev)[0-9]*|[.-]?r[0-9]+|-[0-9]+) ]]
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
# whatever a tool writes to a temporary file goes away with the step
mkdir "$work/tmp"
export TMPDIR="$work/tmp"

pip_version=$(python -m pip --version | awk '{print $2}')
purelib=$(python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
installed_vendor_txt="$purelib/pip/_vendor/vendor.txt"

# What the installed pip carries decides, before anything is fetched.
changed=0
: >"$work/vendor.txt"
while IFS= read -r line || [ -n "$line" ]; do
  if [[ "$line" =~ ^([[:space:]]*)([A-Za-z0-9._-]+)==([^[:space:]#]+)(.*)$ ]]; then
    indent=${BASH_REMATCH[1]}
    name=${BASH_REMATCH[2]}
    have=${BASH_REMATCH[3]}
    rest=${BASH_REMATCH[4]}
    want=${floors[$(normalize "$name")]:-}
    if [ -n "$want" ] && version_lt "$have" "$want"; then
      echo "raising $name $have -> $want"
      line="${indent}${name}==${want}${rest}"
      changed=1
    fi
  fi
  printf '%s\n' "$line" >>"$work/vendor.txt"
done <"$installed_vendor_txt"

if [ "$changed" -eq 0 ]; then
  echo "pip $pip_version already carries every requested version; nothing to change"
  exit 0
fi

commit=${sources[$pip_version]:-}
[ -n "$commit" ] || fail "pip $pip_version has no pinned source: review its commit, then follow 'Bumping pip or Python in the python image' in verifier/contract.md (pin the commit, regenerate pip-vendored.lock, check pip-fix-tools.lock)"

git init --quiet "$work/pip"
cd "$work/pip"
git fetch --quiet --depth 1 https://github.com/pypa/pip "$commit"
git checkout --quiet --detach FETCH_HEAD
[ "$(git rev-parse HEAD)" = "$commit" ] || fail "the fetched commit is not $commit"
grep -q "^__version__ = \"$pip_version\"" src/pip/__init__.py || fail "commit $commit is not pip $pip_version"
cmp -s "$installed_vendor_txt" src/pip/_vendor/vendor.txt || fail "commit $commit does not carry the vendor.txt of the installed pip $pip_version"

vendor_txt=src/pip/_vendor/vendor.txt
cat "$work/vendor.txt" >"$vendor_txt"

# the vendoring tool, and what builds msgpack's sdist, from the hash-locked file
nox_spec=$(sed -nE 's/.*session\.install\("(vendoring[^"]*)"\).*/\1/p' noxfile.py | head -n 1)
[ -n "$nox_spec" ] || fail "no vendoring requirement found in the noxfile.py of pip $pip_version"
grep -qxF "# pip-noxfile: $nox_spec" "$here/pip-fix-tools.lock" ||
  fail "pip-fix-tools.lock was made for another vendoring requirement than $nox_spec: see 'Bumping pip or Python in the python image' in verifier/contract.md"
python -m venv "$work/venv"
"$work/venv/bin/python" -m pip install --quiet --require-hashes --only-binary :all: --no-deps -r "$here/pip-fix-tools.lock"

# the wheelhouse: exactly the libraries the edited vendor.txt names, by hash
pins() {
  local line
  while IFS= read -r line; do
    if [[ "$line" =~ ^[[:space:]]*([A-Za-z0-9._-]+)==([^[:space:]#\\]+) ]]; then
      printf '%s==%s\n' "$(normalize "${BASH_REMATCH[1]}")" "${BASH_REMATCH[2]}"
    fi
  done <"$1" | sort
}
diff <(pins "$vendor_txt") <(pins "$here/pip-vendored.lock") >&2 ||
  fail "pip-vendored.lock does not pin what the edited vendor.txt names (above: < vendor.txt, > lock); regenerate it, see 'Bumping pip or Python in the python image' in verifier/contract.md"

no_binary=()
for name in "${sdists[@]}"; do
  no_binary+=(--no-binary "$name")
done
wheelhouse="$work/wheelhouse"
"$work/venv/bin/python" -m pip download --quiet --require-hashes --no-deps --only-binary :all: "${no_binary[@]}" \
  --no-build-isolation --dest "$wheelhouse" -r "$here/pip-vendored.lock"
for name in "${sdists[@]}"; do
  for sdist in "$wheelhouse/$name"-*.tar.gz; do
    [ -e "$sdist" ] || fail "no sdist of $name was downloaded"
    "$work/venv/bin/python" -m pip wheel --quiet --no-deps --no-build-isolation --no-index --wheel-dir "$wheelhouse" "$sdist"
    rm -f "$sdist"
  done
  compgen -G "$wheelhouse/${name//-/_}-*-none-any.whl" >/dev/null || fail "$name did not build to a pure-Python wheel"
done

# The sync reaches no index (the wheelhouse is the only source) and no host (a proxy that
# refuses every connection, for anything the tool fetches itself).
env PIP_NO_INDEX=1 PIP_FIND_LINKS="$wheelhouse" \
  HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 http_proxy=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9 NO_PROXY= no_proxy= \
  "$work/venv/bin/vendoring" sync

# Nothing under the vendored tree may differ from the pinned commit except the libraries
# that were asked for, and the two files that describe them.
allowed=("$vendor_txt" "src/pip/_vendor/bom.cdx.json")
for key in "${!floors[@]}"; do
  dir="src/pip/_vendor/${key//-/_}"
  [ -d "$dir" ] || fail "$key has no directory $dir in pip's vendored tree"
  allowed+=("$dir/")
done
unexpected=""
while IFS= read -r path; do
  permitted=0
  for entry in "${allowed[@]}"; do
    case "$entry" in
      */) [[ "$path" == "$entry"* ]] && permitted=1 ;;
      *) [ "$path" = "$entry" ] && permitted=1 ;;
    esac
  done
  [ "$permitted" -eq 1 ] || unexpected+="$path"$'\n'
done < <(git status --porcelain --ignored --untracked-files=all | sed -E 's/^.{3}//')
[ -z "$unexpected" ] || fail "the vendoring sync changed more than was asked for:"$'\n'"$unexpected"

# pip's own reproducible build: hash-pinned build requirements, and a timestamp taken
# from the checkout's commit.
python build-project/build-project.py
wheel=dist/pip-$pip_version-py3-none-any.whl
[ -f "$wheel" ] || fail "the build did not produce $wheel"

# by name from the build directory: installing the wheel by path would record a direct URL
# (a file in a deleted temporary directory) in the installed pip's metadata
python -m pip install --quiet --no-index --no-deps --force-reinstall --find-links dist "pip==$pip_version"

bundled=$(python -c 'import ensurepip, pathlib; print(pathlib.Path(ensurepip.__file__).parent / "_bundled")')
cp -f "$wheel" "$bundled/pip-$pip_version-py3-none-any.whl"

# The result must still be a working pip carrying what was asked for, both as installed
# and in a virtual environment seeded from the bundled wheel.
python -m pip --version
debug=$(python -m pip debug)
printf '%s\n' "$debug" | sed -n '/vendored library versions/,/^$/p'
if printf '%s\n' "$debug" | grep -q CONFLICT; then
  fail "pip's vendored libraries do not match its vendor.txt"
fi

python -m venv "$work/seeded"
for interpreter in python "$work/seeded/bin/python"; do
  site=$("$interpreter" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
  while IFS= read -r line; do
    [[ "$line" =~ ^[[:space:]]*([A-Za-z0-9._-]+)==([^[:space:]#]+) ]] || continue
    name=${BASH_REMATCH[1]}
    have=${BASH_REMATCH[2]}
    key=$(normalize "$name")
    [ -n "${floors[$key]:-}" ] || continue
    if version_lt "$have" "${floors[$key]}"; then
      fail "$site/pip carries $name==$have, below ${floors[$key]}"
    fi
    grep -q "\"pkg:pypi/$name@$have\"" "$site/pip/_vendor/bom.cdx.json" ||
      fail "$site/pip/_vendor/bom.cdx.json does not list $name==$have"
  done <"$site/pip/_vendor/vendor.txt"
done
"$work/seeded/bin/pip" --version

# and one real index request through the new urllib3, for a file checked by hash
printf 'six==1.17.0 --hash=sha256:4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274\n' >"$work/smoke.txt"
python -m pip download --quiet --no-deps --require-hashes --only-binary :all: --dest "$work/smoke" -r "$work/smoke.txt"
