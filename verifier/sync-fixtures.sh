#!/usr/bin/env bash
# Keeps this repository's copy of the platform's static analysis corpus the same as the
# platform's, so the semgrep rules and the Elixir matchers that implement the same check cannot
# drift apart unnoticed.
#
#   sync-fixtures.sh [--check] [--dest DIR] PLATFORM_REPOSITORY
#
# The platform defines each built-in static class twice: a semgrep rule file and an Elixir
# matcher. Both are held to one corpus of small source files, a `positive` set the class must
# flag and a `negative` set it must not. The platform's tests run its matcher over every file
# of the corpus; this repository's contract test (verifier/test/contract.sh, corpus_cases) runs
# semgrep with the same rule over the same files, inside every image. The corpus lives in the
# platform repository, at
#
#     apps/sre_core/priv/code_analysis/rules/<class>.yml
#     apps/sre_core/priv/code_analysis/fixtures/<class>/positive/<file>
#     apps/sre_core/priv/code_analysis/fixtures/<class>/negative/<file>
#
# and this copy, committed to this repository, is what the contract test reads:
#
#     verifier/test/corpus/rules/<class>.yml
#     verifier/test/corpus/fixtures/<class>/{positive,negative}/<file>
#
# Without --check the script replaces the copy with the platform's. With --check it changes
# nothing and fails when the two differ (a file added, removed or changed on either side).
# Either way the platform's corpus must be well formed: every class has a rule file and both a
# positive and a negative fixture, names are plain, there are no links, no file is over 200 KB.
# The corpus is public, since this repository is: nothing in it may name a customer, an
# organization or an account.
#
# A platform repository that has no corpus yet is not an error: the script says so and, with
# --check, accepts only a local copy that is absent too. A PLATFORM_REPOSITORY that is not a
# directory is a usage error (exit 2). Exit 1 means drift or a corpus that is not well formed.
#
# --dest is for tests; the default is test/corpus next to this script.

set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
dest=$here/test/corpus
check=no
platform=""

usage() {
  echo "usage: $0 [--check] [--dest DIR] PLATFORM_REPOSITORY" >&2
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --check)
      check=yes
      shift
      ;;
    --dest)
      [ -n "${2:-}" ] || usage
      dest=$2
      shift 2
      ;;
    -*) usage ;;
    *)
      [ -z "$platform" ] || usage
      platform=$1
      shift
      ;;
  esac
done
[ -n "$platform" ] || usage
[ -d "$platform" ] || {
  echo "$platform is not a directory: give the path of the platform repository checkout" >&2
  exit 2
}

source_dir=$platform/apps/sre_core/priv/code_analysis
class_re='^[a-z][a-z0-9-]{0,60}$'

problems=()
problem() { problems+=("$*"); }

# well_formed ROOT: the corpus under ROOT (rules/ and fixtures/) is what the script accepts.
well_formed() {
  local root=$1 rule class dir set file
  [ -z "$(find "$root" -type l -print -quit)" ] || problem "$root holds a symbolic link: $(find "$root" -type l -print -quit)"
  [ -z "$(find "$root" -type f -size +200k -print -quit)" ] || problem "$root holds a file over 200 KB: $(find "$root" -type f -size +200k -print -quit)"
  for rule in "$root"/rules/* "$root"/rules/.[!.]*; do
    [ -e "$rule" ] || continue
    class=$(basename "$rule")
    if [ ! -f "$rule" ] || [[ "$class" != *.yml ]] || ! [[ "${class%.yml}" =~ $class_re ]]; then
      problem "$root/rules holds '$class', which is not <class>.yml with a plain class name"
      continue
    fi
    class=${class%.yml}
    for set in positive negative; do
      if [ -z "$(find "$root/fixtures/$class/$set" -type f -print -quit 2>/dev/null)" ]; then
        problem "class $class has a rule and no $set fixture ($root/fixtures/$class/$set)"
      fi
    done
  done
  for dir in "$root"/fixtures/* "$root"/fixtures/.[!.]*; do
    [ -e "$dir" ] || continue
    class=$(basename "$dir")
    if [ ! -d "$dir" ] || ! [[ "$class" =~ $class_re ]]; then
      problem "$root/fixtures holds '$class', which is not a class directory with a plain name"
      continue
    fi
    [ -f "$root/rules/$class.yml" ] || problem "fixtures for class $class have no rule file ($root/rules/$class.yml)"
    for file in "$dir"/* "$dir"/.[!.]*; do
      [ -e "$file" ] || continue
      case "$(basename "$file")" in
        positive | negative) [ -d "$file" ] || problem "$file is not a directory" ;;
        *) problem "$dir holds '$(basename "$file")': only positive/ and negative/ belong in a class" ;;
      esac
    done
  done
}

finish_with_problems() {
  printf '%s\n' "${problems[@]}" >&2
  exit 1
}

has_corpus() {
  [ -d "$1/rules" ] && [ -d "$1/fixtures" ]
}

if ! has_corpus "$source_dir"; then
  echo "the platform repository has no shared fixture corpus yet ($source_dir/rules and $source_dir/fixtures): nothing to copy" >&2
  if [ "$check" = yes ] && has_corpus "$dest"; then
    echo "but $dest holds a copy of one: the platform's corpus was removed or moved" >&2
    exit 1
  fi
  exit 0
fi

well_formed "$source_dir"
[ "${#problems[@]}" -eq 0 ] || finish_with_problems

if [ "$check" = yes ]; then
  has_corpus "$dest" || {
    echo "$dest has no copy of the corpus: run verifier/sync-fixtures.sh $platform" >&2
    exit 1
  }
  differences=$({
    diff -rq "$source_dir/rules" "$dest/rules" || true
    diff -rq "$source_dir/fixtures" "$dest/fixtures" || true
  } 2>&1)
  if [ -n "$differences" ]; then
    echo "the corpus in $dest differs from the platform's ($source_dir):" >&2
    printf '%s\n' "$differences" >&2
    echo "run verifier/sync-fixtures.sh $platform and commit the result" >&2
    exit 1
  fi
  echo "the corpus in $dest is the platform's"
  exit 0
fi

staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
cp -R "$source_dir/rules" "$staging/rules"
cp -R "$source_dir/fixtures" "$staging/fixtures"
mkdir -p "$dest"
rm -rf "$dest/rules" "$dest/fixtures"
mv "$staging/rules" "$dest/rules"
mv "$staging/fixtures" "$dest/fixtures"
echo "copied the platform's corpus ($(find "$dest/rules" -type f | wc -l) rule files, $(find "$dest/fixtures" -type f | wc -l) fixtures) to $dest"
