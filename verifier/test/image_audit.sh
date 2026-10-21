#!/usr/bin/env bash
# What the image lets the unprivileged user write, checked inside the built image (verifier/test/
# contract.sh runs it as root with this file mounted read-only). The runner is root and the
# repository's commands are not, so a place the unprivileged user can write that root executes
# from or has on a PATH is a root code execution, and any other place it can write persists
# between the jobs of a session unless the restore covers it. Fails, naming each place, when:
#
#   * a directory on the runner's own PATH (SAFE_PATH in the runner), or on the image's PATH, or
#     any directory above one, can be written by the unprivileged user, or can be created by it
#     because its nearest existing parent can;
#   * the runner, its step environment or the work directory can be written by it;
#   * it can write any directory or regular file outside its home, /tmp, /var/tmp and the two
#     mounts the container runtime adds and the restore covers (/dev/shm, /dev/mqueue).
#
# "Can write" is asked of the kernel as that user (find -writable, test -w under setpriv), never
# read off a mode bit.
set -uo pipefail

user=${1:-sandbox}
uid=$(id -u "$user")
gid=$(id -g "$user")
runner=/usr/local/bin/verifier-run
failed=0

as_sandbox() { setpriv --reuid="$uid" --regid="$gid" --clear-groups "$@"; }
problem() {
  printf 'AUDIT: %s\n' "$*" >&2
  failed=1
}

safe_path=$(sed -n 's/^SAFE_PATH=//p' "$runner")
[ -n "$safe_path" ] || problem "the runner sets no SAFE_PATH"

# chain_writable PATH: the first of PATH and the places above it that the user can write (an
# existing one), or, for a PATH that does not exist, the first existing parent the user can write
# (it could create PATH). Prints it and answers 0, or answers 1.
chain_writable() {
  local walk
  walk=$(readlink -m -- "$1")
  while [ ! -e "$walk" ] && [ "$walk" != / ]; do
    walk=${walk%/*}
    [ -n "$walk" ] || walk=/
  done
  walk=$(readlink -f -- "$walk")
  while :; do
    if as_sandbox test -w "$walk"; then
      printf '%s\n' "$walk"
      return 0
    fi
    [ "$walk" != / ] || return 1
    walk=${walk%/*}
    [ -n "$walk" ] || walk=/
  done
}

IFS=: read -r -a runner_dirs <<<"$safe_path"
IFS=: read -r -a image_dirs <<<"$PATH"
for dir in "${runner_dirs[@]}"; do
  if hit=$(chain_writable "$dir"); then problem "$hit can be written by $user and the runner's PATH names $dir"; fi
done
for dir in "${image_dirs[@]}"; do
  case "$dir" in "/home/$user" | "/home/$user"/*) continue ;; esac
  if hit=$(chain_writable "$dir"); then problem "$hit can be written by $user and the image's PATH names $dir"; fi
done
for path in "$runner" /etc/verifier/step.env /etc/verifier /work; do
  if hit=$(chain_writable "$path"); then problem "$hit can be written by $user and the runner uses $path"; fi
done

excluded=(\( -path /proc -o -path /sys \) -prune -o)
allowed='^(/home/'"$user"'(/.*)?|/tmp(/.*)?|/var/tmp(/.*)?|/dev/shm(/.*)?|/dev/mqueue(/.*)?)$'
while IFS= read -r -d '' path; do
  [[ "$path" =~ $allowed ]] || problem "$path (a directory) can be written by $user and is none of the places the restore covers"
done < <(as_sandbox find / "${excluded[@]}" -type d -writable -print0 2>/dev/null)
while IFS= read -r -d '' path; do
  [[ "$path" =~ $allowed ]] || problem "$path (a file) can be written by $user and is none of the places the restore covers"
done < <(as_sandbox find / "${excluded[@]}" -type f -writable -print0 2>/dev/null)

if [ "$failed" -eq 0 ]; then echo "audit: ok"; fi
exit "$failed"
