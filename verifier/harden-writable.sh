#!/usr/bin/env bash
# Run once per image, as root, after every package and toolchain step and before the runner is
# copied in. The unprivileged user runs every command of the repository under test, and the
# runner is root: a directory the unprivileged user can write that root's PATH names, or that
# root executes from, lets a command plant a binary root then runs. The base images ship a few
# such directories (the Go image's GOPATH bin is mode 1777, the PHP image's web root is 1777,
# /run/lock is 1777 everywhere), and a base that is refreshed weekly can add another, so this
# removes the write bits instead of listing the ones known today.
#
# What stays writable on purpose: the sandbox user's home, the work directory's own children
# (made at run time), /tmp and /var/tmp (the restore between jobs puts them back) and the
# mounts the container runtime adds (/dev/shm, /dev/mqueue), which the restore also covers.
# verifier/test/image_audit.sh proves the result in the built image.
set -euo pipefail

user=${1:-sandbox}
uid=$(id -u "$user")
gid=$(id -g "$user")

find / -xdev \( -path /proc -o -path /sys -o -path /dev -o -path /tmp -o -path /var/tmp \
  -o -path "/home/$user" -o -path /work \) -prune -o \
  -type d \( -perm -0002 -o -uid "$uid" -o \( -gid "$gid" -perm -0020 \) \) -print0 |
  while IFS= read -r -d '' dir; do
    if [ "$(stat -c %u "$dir")" = "$uid" ]; then chown root:root "$dir"; fi
    if [ "$(stat -c %g "$dir")" = "$gid" ]; then chgrp root "$dir"; fi
    chmod go-w,-t "$dir"
  done

# Files the sandbox user owns outside its home (a build step that ran as that user) are root's.
find / -xdev \( -path /proc -o -path /sys -o -path /dev -o -path /tmp -o -path /var/tmp \
  -o -path "/home/$user" -o -path /work \) -prune -o \
  \( -uid "$uid" -o -gid "$gid" \) ! -type l -print0 |
  while IFS= read -r -d '' path; do
    chown -h root:root "$path"
    chmod go-w "$path"
  done
