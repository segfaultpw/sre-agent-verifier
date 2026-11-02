#!/usr/bin/env bash
# Raises the libraries Maven bundles (lib/ under its home) in the java image. Maven ships
# its dependencies as jars next to itself, so a base image carries whichever versions the
# Maven release of the day was built with, and no Maven release has yet moved to the fixed
# release of every library an image scanner flags in it.
#
#   fix-maven-bundled.sh --min GROUP:ARTIFACT@VERSION=SHA256 [--min GROUP:ARTIFACT@VERSION=SHA256]...
#
# Every jar lib/ARTIFACT-<version>.jar whose version is below VERSION, and which is the same
# flavour of it (a -jre jar is never replaced by an -android one, nor the other way round), is
# replaced by exactly VERSION from Maven Central. SHA256 is the hex digest the jar must have,
# and it is checked twice before the jar replaces anything: against the checksum Maven Central
# publishes beside the artifact, and against the digest of the bytes that were downloaded, so
# neither the repository's checksum file nor its jar is trusted alone. A mismatch stops the
# build. A jar that is already new enough is left alone and then nothing is downloaded: the
# step goes quiet once the base image's Maven catches up. Afterwards the old jar is gone, the
# new one is the one in lib/, and Maven still starts and runs a build (a pom of this step's
# own, offline, so no third-party code is fetched or run to prove it).

set -euo pipefail

central=https://repo1.maven.org/maven2

floors=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --min)
      [[ "${2:-}" =~ ^([a-z0-9][a-z0-9._-]*(\.[a-z0-9][a-z0-9._-]*)*):([a-z0-9][a-z0-9._-]*)@([0-9]+(\.[0-9]+)+(-[A-Za-z0-9]+)?)=([0-9a-f]{64})$ ]] || {
        echo "--min takes GROUP:ARTIFACT@VERSION=<sha256 hex>, got '${2:-}'" >&2
        exit 2
      }
      floors+=("$2")
      shift 2
      ;;
    *)
      echo "usage: $0 --min GROUP:ARTIFACT@VERSION=SHA256 [--min GROUP:ARTIFACT@VERSION=SHA256]..." >&2
      exit 2
      ;;
  esac
done

if [ "${#floors[@]}" -eq 0 ]; then
  echo "nothing to do: give at least one --min" >&2
  exit 2
fi

# a < b for MAJOR.MINOR.PATCH release parts compared as numbers
release_lt() {
  [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n 1)" = "$1" ]
}

maven_home=$(mvn -v | sed -n 's/^Maven home: //p')
lib="$maven_home/lib"
[ -d "$lib" ] || {
  echo "no Maven lib directory at '$lib'" >&2
  exit 1
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

changed=0
for floor in "${floors[@]}"; do
  expected=${floor#*=}
  spec=${floor%%=*}
  coordinate=${spec%@*}
  want=${spec##*@}
  group=${coordinate%%:*}
  artifact=${coordinate#*:}
  [[ "$want" =~ ^([0-9]+(\.[0-9]+)*)(-.+)?$ ]]
  want_release=${BASH_REMATCH[1]}
  want_flavour=${BASH_REMATCH[3]}
  jar=
  for candidate in "$lib/$artifact"-*.jar; do
    [ -e "$candidate" ] || continue
    have=${candidate##*/}
    have=${have#"$artifact"-}
    have=${have%.jar}
    [[ "$have" =~ ^([0-9]+(\.[0-9]+)*)(-.+)?$ ]] || continue
    [ "${BASH_REMATCH[3]}" = "$want_flavour" ] || continue
    release_lt "${BASH_REMATCH[1]}" "$want_release" || continue
    echo "raising lib/${candidate##*/} -> $artifact-$want.jar"
    jar=$candidate
    if [ ! -f "$work/$artifact-$want.jar" ]; then
      url="$central/${group//.//}/$artifact/$want/$artifact-$want.jar"
      curl --proto '=https' --tlsv1.2 -fsSL --retry 3 -o "$work/$artifact-$want.jar" "$url"
      published=$(curl --proto '=https' --tlsv1.2 -fsSL --retry 3 "$url.sha256" | tr -d '[:space:]')
      got=$(sha256sum "$work/$artifact-$want.jar" | cut -d' ' -f1)
      [ "$published" = "$expected" ] || {
        echo "$artifact-$want.jar: Maven Central publishes $published, pinned $expected" >&2
        exit 1
      }
      [ "$got" = "$expected" ] || {
        echo "$artifact-$want.jar does not have the pinned sha256: expected $expected, got $got" >&2
        exit 1
      }
    fi
    install -m 0644 "$work/$artifact-$want.jar" "$lib/$artifact-$want.jar"
    rm -f "$candidate"
    [ ! -e "$candidate" ] || {
      echo "$candidate is still there after the replacement" >&2
      exit 1
    }
    changed=1
  done
  if [ -n "$jar" ]; then
    [ "$(sha256sum "$lib/$artifact-$want.jar" | cut -d' ' -f1)" = "$expected" ] || {
      echo "$lib/$artifact-$want.jar is not the pinned jar after the replacement" >&2
      exit 1
    }
  fi
done

if [ "$changed" -eq 0 ]; then
  echo "Maven $(mvn -v | sed -n '1s/^Apache Maven \([^ ]*\).*/\1/p') already carries every requested version; nothing to change"
  exit 0
fi

mvn -v

# Maven starts its dependency injection from these jars: a build with no plugin to resolve
# (an empty pom) runs through them offline, so a replacement that broke the class path fails here
mkdir -p "$work/smoke"
cat >"$work/smoke/pom.xml" <<'POM'
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>verifier.smoke</groupId>
  <artifactId>smoke</artifactId>
  <version>1</version>
  <packaging>pom</packaging>
</project>
POM
(cd "$work/smoke" && mvn --offline --batch-mode --quiet -Dmaven.repo.local="$work/repo" validate)
