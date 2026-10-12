#!/usr/bin/env bash
# The runner contract, inside a real image. Usage:
#
#   verifier/test/contract.sh RUNTIME [RUNTIME ...]      (default: node)
#
# Builds verifier/images/RUNTIME, then runs it as root on the host network against
# a local fixture repository and a loopback platform. It checks the result shape and
# what a host-side test of run.sh cannot, because it needs the real uid separation:
#
#   * a command sees none of the runner's variables, neither in its own environment
#     nor through /proc/<runner pid>/environ, and cannot open the runner's directory;
#   * a daemon a command starts in its own session (so it leaves the process group)
#     is dead when the step ends, and so is anything else the unprivileged user owns;
#   * a daemon left behind cannot read the bootstrap or result URL from any command
#     line, because curl reads them from files;
#   * the patch is written by the unprivileged user (root never writes into the tree);
#   * the runner's own time cap holds when a command ignores it, and when the
#     repository's own commands plant what the runner's git runs (a hook, a config
#     entry, a filter) or block a file the patch is written to;
#   * git hooks and untrusted git configuration the commands plant never run;
#   * a sandbox user that is root is refused.
#
# Needs docker, git, jq and python3. No network beyond the image build.

set -euo pipefail

here=$(cd "$(dirname "$0")/.." && pwd)
runtimes=("$@")
if [ "${#runtimes[@]}" -eq 0 ]; then runtimes=(node); fi

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# run_case TAG TMP PORT BOOTSTRAP_JSON [DOCKER_ARG ...] [-- CONTAINER_ARG ...]
# Starts the loopback platform on PORT, runs the image once and leaves the result in
# $TMP/result.json. Prints the seconds the container took.
run_case() {
  local tag=$1 tmp=$2 port=$3 bootstrap=$4
  shift 4
  local docker_args=() container_args=()
  while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
    docker_args+=("$1")
    shift
  done
  if [ "${1:-}" = "--" ]; then shift; fi
  container_args=("$@")
  rm -f "$tmp/result.json"
  python3 "$here/test/fake_platform.py" "$port" "$bootstrap" "$tmp/result.json" &
  server_pid=$!
  sleep 1
  local started
  started=$(date +%s)
  docker run --rm --network host \
    -v "$tmp/origin:/origin:ro" \
    -e "BOOTSTRAP_URL=http://127.0.0.1:$port/bootstrap" \
    -e "AWS_SECRET_ACCESS_KEY=leak-aws" \
    "${docker_args[@]}" \
    "$tag" "${container_args[@]}" || fail "the runner exited non-zero"
  echo $(($(date +%s) - started))
  kill "$server_pid" 2>/dev/null || true
}

for runtime in "${runtimes[@]}"; do
  echo "== $runtime"
  tag="verifier-contract-$runtime:local"
  # VERIFIER_BUILD_ARGS: extra `docker build` arguments, e.g. --no-cache for the weekly
  # rebuild, which must not reuse a layer built weeks ago.
  read -r -a build_args <<<"${VERIFIER_BUILD_ARGS:-}"
  # The build's output is kept and shown when it fails: a quiet build prints nothing of what a
  # failing RUN wrote, and the image's fix steps explain a refusal (an unpinned pip, a
  # mismatched integrity) on stderr.
  build_log=$(mktemp)
  if ! docker build "${build_args[@]}" --progress=plain -f "$here/images/$runtime/Dockerfile" -t "$tag" "$here" >"$build_log" 2>&1; then
    cat "$build_log" >&2
    rm -f "$build_log"
    fail "building the $runtime image failed (its output is above)"
  fi
  rm -f "$build_log"

  tmp=$(mktemp -d)
  trap 'kill "${server_pid:-0}" 2>/dev/null || true; docker run --rm -v "$tmp:/t" busybox sh -c "rm -rf /t/* /t/.[!.]*" >/dev/null 2>&1 || true; rmdir "$tmp" 2>/dev/null || true' EXIT

  origin="$tmp/origin"
  mkdir -p "$origin"
  printf 'echo 5\n' >"$origin/calc.sh"
  printf 'test -f calc.sh\n' >"$origin/test_old.sh"
  # The repository's own attributes: the filter named here is defined (or not) by what a
  # command writes into .git/config, which is how a blocking git operation is planted.
  printf 'test_old.sh filter=slow\n' >"$origin/.gitattributes"
  git -C "$origin" init -q -b main
  git -C "$origin" add -A
  git -C "$origin" -c user.email=t@t -c user.name=t commit -q -m base
  sha=$(git -C "$origin" rev-parse HEAD)
  # Owned by root, like the repository the runner (root) clones: git refuses a
  # local clone of a repository owned by somebody else.
  docker run --rm -v "$origin:/o" busybox chown -R 0:0 /o >/dev/null

  port=$((20000 + RANDOM % 20000))

  # Steps. `index` is the command's position in the run's list.
  probe='test -z "${BOOTSTRAP_URL:-}${REPO_TOKEN:-}${RESULT_URL:-}${PATCH_URL:-}${STEPS_JSON:-}${REPO_URL:-}${BASE_SHA:-}${AWS_SECRET_ACCESS_KEY:-}" && test -z "$(env | grep -E "leak|ghs_|127.0.0.1|bootstrap")"'
  proc_probe='for p in /proc/[0-9]*; do if cat "$p/environ" 2>/dev/null | tr "\000" "\n" | grep -qE "BOOTSTRAP_URL=|REPO_TOKEN=|leak"; then exit 1; fi; done; exit 0'
  dir_probe='! ls /work/out >/dev/null 2>&1 && ! test -r /work/out/result.curl && ! test -r /work/out/bootstrap.curl'
  # A daemon with its own session (it is not in the step's process group), and one
  # that scans every command line for a URL or the token and records what it finds in
  # the sandbox user's home.
  daemon='setsid sh -c "while true; do echo alive >>\"\$HOME/alive\"; sleep 0.2; done" >/dev/null 2>&1 &
setsid sh -c "a=127.0.0; b=.1:; c=ghs_; d=contract; while true; do for f in /proc/[0-9]*/cmdline; do if tr \"\\000\" \" \" <\$f 2>/dev/null | grep -q -e \"\$a\$b\" -e \"\$c\$d\"; then echo found >>\"\$HOME/found\"; fi; done; sleep 0.05; done" >/dev/null 2>&1 &
sleep 0.5'
  # Run by a LATER step: nothing of the unprivileged user may be left from the one
  # before (this step itself is its only process).
  leftover_probe='me=$$; n=0; for p in /proc/[0-9]*; do pid=${p#/proc/}; [ "$pid" = "$me" ] && continue; uid=$(awk "/^Uid:/ {print \$2}" "$p/status" 2>/dev/null); st=$(awk "/^State:/ {print \$2}" "$p/status" 2>/dev/null); if [ "$uid" = "$(id -u)" ] && [ "$st" != Z ]; then case $(tr "\000" " " <"$p/cmdline" 2>/dev/null) in *leftover_probe*|*"awk "*|*"tr "*) ;; *) n=$((n+1)); echo "left: $pid $(tr "\000" " " <"$p/cmdline" 2>/dev/null)" >&2;; esac; fi; done; test ! -e "$HOME/found" || echo "found marker" >&2; test "$n" = 0 && test ! -e "$HOME/found"'
  patch_owner='test "$(stat -c %u calc.sh)" = "$(id -u)" && test "$(stat -c %u test_new.sh)" = "$(id -u)"'

  jq -n \
    --arg port "$port" --arg sha "$sha" --arg probe "$probe" --arg proc "$proc_probe" \
    --arg dirp "$dir_probe" --arg daemon "$daemon" --arg leftover "$leftover_probe" \
    --arg owner "$patch_owner" '{
      repo_url: "file:///origin",
      repo_token: "ghs_contract_read_only_token",
      base_sha: $sha,
      result_url: ("http://127.0.0.1:" + $port + "/result"),
      wall_clock_seconds: 300,
      max_output_bytes: 100000,
      steps: [
        {index: 0, name: "setup_step", kind: "setup", cmd: "echo installing"},
        {index: 1, name: "daemon", kind: "setup", cmd: $daemon},
        {index: 2, name: "leftover_probe", kind: "setup", cmd: $leftover},
        {index: 3, name: "tests", kind: "test", cmd: "sh test_old.sh"},
        {index: 4, name: "lint", kind: "check", cmd: "echo lint"},
        {index: 5, name: "env_probe", kind: "check", cmd: $probe},
        {index: 6, name: "proc_probe", kind: "check", cmd: $proc},
        {index: 7, name: "dir_probe", kind: "check", cmd: $dirp},
        {index: 8, name: "patch_owner", kind: "check", cmd: $owner},
        {index: 9, name: "new_test", kind: "new_test", cmd: "sh test_new.sh"}
      ],
      files: [
        {path: "calc.sh", content: "echo 4\n", test: false},
        {path: "test_new.sh", content: "test \"$(sh calc.sh)\" = 4\n", test: true}
      ]
    }' >"$tmp/bootstrap.json"

  run_case "$tag" "$tmp" "$port" "$tmp/bootstrap.json" >/dev/null

  [ -s "$tmp/result.json" ] || fail "no result was uploaded"
  jq -e '.schema == 1 and .baseline_green == true' "$tmp/result.json" >/dev/null || {
    jq . "$tmp/result.json" >&2
    fail "baseline was not green"
  }

  for name in clone baseline new_test_fails_before lint tests new_test_passes_after env_probe proc_probe dir_probe leftover_probe patch_owner daemon; do
    status=$(jq -r --arg n "$name" '[.steps[] | select(.name == $n)][0].status' "$tmp/result.json")
    [ "$status" = pass ] || {
      jq . "$tmp/result.json" >&2
      fail "step $name was '$status', not pass"
    }
  done

  # a step is reported by index, never by text
  if jq -e '[.steps[] | has("command")] | any' "$tmp/result.json" >/dev/null; then fail "a step carries command text"; fi
  jq -e '[.steps[] | select(.name == "tests")][0].index == 3' "$tmp/result.json" >/dev/null || fail "the tests step lost its index"

  if grep -q 'ghs_contract_read_only_token\|leak-aws\|127.0.0.1' "$tmp/result.json"; then fail "a secret leaked into the result"; fi
  [ "$(wc -c <"$tmp/result.json")" -lt 64000 ] || fail "the result is over the platform's 64 KB bound"

  # The time cap holds when a command ignores TERM and starts its own session.
  jq -n --arg port "$port" --arg sha "$sha" '{
      repo_url: "file:///origin", repo_token: "ghs_contract_read_only_token", base_sha: $sha,
      result_url: ("http://127.0.0.1:" + $port + "/result"),
      wall_clock_seconds: 40, max_output_bytes: 100000,
      steps: [
        {index: 0, name: "tests", kind: "test", cmd: "true"},
        {index: 1, name: "stubborn", kind: "check",
         cmd: "trap \"\" TERM; setsid sh -c \"trap \\\"\\\" TERM; sleep 300\" >/dev/null 2>&1 & while true; do sleep 1; done"}
      ],
      files: [{path: "calc.sh", content: "echo 4\n", test: false}]
    }' >"$tmp/bootstrap2.json"
  seconds=$(run_case "$tag" "$tmp" "$port" "$tmp/bootstrap2.json")
  [ "$(jq -r '[.steps[] | select(.name == "stubborn")][0].status' "$tmp/result.json")" = timeout ] || {
    jq . "$tmp/result.json" >&2
    fail "the stubborn step was not a timeout"
  }
  [ "$seconds" -lt 40 ] || fail "the runner outlived its own cap ($seconds s)"

  # What the repository's own commands plant for git to run: a hook in .git/hooks, a
  # hooksPath and a file system monitor in .git/config. test_old.sh is tracked, so
  # taking the tests-only patch back is a `git checkout` of it, the command a
  # post-checkout hook fires on. None of the three may run, and the proof is a step of
  # the profile that looks for what they would have left in the sandbox user's home.
  plant='mkdir -p .git/hooks "$HOME/hooks2" && for f in .git/hooks/post-checkout "$HOME/hooks2/post-checkout"; do printf "#!/bin/sh\ntouch \"$HOME/ran-%s\"\n" "$(basename "$(dirname "$f")")" >"$f"; chmod +x "$f"; done && printf "#!/bin/sh\ntouch \"$HOME/ran-fsmonitor\"\n" >"$HOME/fsm.sh" && chmod +x "$HOME/fsm.sh" && git config core.hooksPath "$HOME/hooks2" && git config core.fsmonitor "$HOME/fsm.sh"'
  ran_probe='ls "$HOME" | grep -q "^ran-" && { ls "$HOME" >&2; exit 1; }; exit 0'

  jq -n --arg port "$port" --arg sha "$sha" --arg plant "$plant" --arg ran "$ran_probe" '{
      repo_url: "file:///origin", repo_token: "ghs_contract_read_only_token", base_sha: $sha,
      result_url: ("http://127.0.0.1:" + $port + "/result"),
      wall_clock_seconds: 300, max_output_bytes: 100000,
      steps: [
        {index: 0, name: "plant", kind: "setup", cmd: $plant},
        {index: 1, name: "tests", kind: "test", cmd: "sh test_old.sh"},
        {index: 2, name: "ran_probe", kind: "check", cmd: $ran},
        {index: 3, name: "new_test", kind: "new_test", cmd: "sh test_new.sh"}
      ],
      files: [
        {path: "calc.sh", content: "echo 4\n", test: false},
        {path: "test_old.sh", content: "test -f calc.sh\n# reworked\n", test: true},
        {path: "test_new.sh", content: "test \"$(sh calc.sh)\" = 4\n", test: true}
      ]
    }' >"$tmp/bootstrap3.json"
  run_case "$tag" "$tmp" "$port" "$tmp/bootstrap3.json" >/dev/null
  for name in plant baseline new_test_fails_before ran_probe tests new_test_passes_after; do
    status=$(jq -r --arg n "$name" '[.steps[] | select(.name == $n)][0].status' "$tmp/result.json")
    [ "$status" = pass ] || {
      jq . "$tmp/result.json" >&2
      fail "step $name was '$status' with a planted hook, hooksPath and fsmonitor, not pass"
    }
  done

  # The runner's own git is held to the clock. A filter planted in .git/config makes
  # `git checkout` of the tracked test file block for five minutes; a FIFO where the
  # patch is written makes the write block. The result is still uploaded inside the
  # runner's own cap (wall_clock_seconds minus the 30 s kept for the upload).
  jq -n --arg port "$port" --arg sha "$sha" '{
      repo_url: "file:///origin", repo_token: "ghs_contract_read_only_token", base_sha: $sha,
      result_url: ("http://127.0.0.1:" + $port + "/result"),
      wall_clock_seconds: 45, max_output_bytes: 100000,
      steps: [
        {index: 0, name: "plant", kind: "setup", cmd: "git config filter.slow.smudge \"sleep 300; cat\""},
        {index: 1, name: "tests", kind: "test", cmd: "sh test_old.sh"},
        {index: 2, name: "new_test", kind: "new_test", cmd: "sh test_new.sh"}
      ],
      files: [
        {path: "calc.sh", content: "echo 4\n", test: false},
        {path: "test_old.sh", content: "test -f calc.sh\n# reworked\n", test: true},
        {path: "test_new.sh", content: "test \"$(sh calc.sh)\" = 4\n", test: true}
      ]
    }' >"$tmp/bootstrap4.json"
  seconds=$(run_case "$tag" "$tmp" "$port" "$tmp/bootstrap4.json")
  [ "$(jq -r '[.steps[] | select(.name == "patch")][0].status' "$tmp/result.json")" = timeout ] || {
    jq . "$tmp/result.json" >&2
    fail "a blocking git filter did not end as a timeout of the patch step"
  }
  [ "$seconds" -lt 45 ] || fail "a planted git filter held the runner past its cap ($seconds s)"

  jq -n --arg port "$port" --arg sha "$sha" '{
      repo_url: "file:///origin", repo_token: "ghs_contract_read_only_token", base_sha: $sha,
      result_url: ("http://127.0.0.1:" + $port + "/result"),
      wall_clock_seconds: 45, max_output_bytes: 100000,
      steps: [
        {index: 0, name: "tests", kind: "test", cmd: "sh test_old.sh && rm -f calc.sh && mkfifo calc.sh"}
      ],
      files: [{path: "calc.sh", content: "echo 4\n", test: false}]
    }' >"$tmp/bootstrap5.json"
  seconds=$(run_case "$tag" "$tmp" "$port" "$tmp/bootstrap5.json")
  [ "$(jq -r '[.steps[] | select(.name == "patch")][0].status' "$tmp/result.json")" = timeout ] || {
    jq . "$tmp/result.json" >&2
    fail "a FIFO where the patch is written did not end as a timeout of the patch step"
  }
  [ "$seconds" -lt 45 ] || fail "a FIFO where the patch is written held the runner past its cap ($seconds s)"

  # Static analysis: the platform's rule pack is written by root into a directory of its
  # own that the unprivileged user can read and cannot write to, an analysis command reads
  # it as {rules_dir}, and a tool the image does not carry is skipped, not a failure.
  jq -n --arg port "$port" --arg sha "$sha" '{
      repo_url: "file:///origin", repo_token: "ghs_contract_read_only_token", base_sha: $sha,
      result_url: ("http://127.0.0.1:" + $port + "/result"),
      wall_clock_seconds: 300, max_output_bytes: 100000,
      steps: [
        {index: 0, name: "tests", kind: "test", cmd: "sh test_old.sh"},
        {index: 1, name: "rules_read", kind: "analysis", cmd: "grep -q sre-x {rules_dir}/a.yml"},
        {index: 2, name: "rules_readonly", kind: "check",
         cmd: "! touch /work/rules/evil 2>/dev/null && ! test -w /work/rules && ! test -w /work/rules/a.yml && test \"$(stat -c %u /work/rules/a.yml)\" = 0"},
        {index: 3, name: "scan", kind: "analysis", cmd: "semgrep-not-in-this-image --config {rules_dir}"}
      ],
      files: [{path: "calc.sh", content: "echo 4\n", test: false}],
      analysis: {rule_pack: "v1-contract", files: {"a.yml": "rules:\n  - id: sre-x\n"}}
    }' >"$tmp/bootstrap6.json"
  run_case "$tag" "$tmp" "$port" "$tmp/bootstrap6.json" >/dev/null
  for name in rules_read rules_readonly; do
    status=$(jq -r --arg n "$name" '[.steps[] | select(.name == $n)][0].status' "$tmp/result.json")
    [ "$status" = pass ] || {
      jq . "$tmp/result.json" >&2
      fail "step $name was '$status', not pass: the rule pack is not readable or not read-only"
    }
  done
  [ "$(jq -r '[.steps[] | select(.name == "scan")][0].status' "$tmp/result.json")" = skipped ] || {
    jq . "$tmp/result.json" >&2
    fail "a tool the image does not carry was not skipped"
  }

  # A sandbox user that is root would run every command as root and let the uid-wide
  # kill end the runner itself: refused, before any command runs. So is one whose
  # primary group is root's (a user created in the image with `-g 0`).
  run_case "$tag" "$tmp" "$port" "$tmp/bootstrap.json" -e VERIFIER_SANDBOX_USER=root >/dev/null
  jq -e '(.runner_error | test("sandbox user is root")) and ([.steps[] | select(.name == "baseline")] | length == 0)' "$tmp/result.json" >/dev/null || {
    jq . "$tmp/result.json" >&2
    fail "a sandbox user that is root was not refused"
  }

  run_case "$tag" "$tmp" "$port" "$tmp/bootstrap.json" \
    -e VERIFIER_SANDBOX_USER=gzero --entrypoint /bin/sh \
    -- -c 'useradd -M -u 10002 -g 0 -s /bin/sh gzero && exec /usr/local/bin/verifier-run' >/dev/null
  jq -e '(.runner_error | test("sandbox user is root")) and ([.steps[] | select(.name == "baseline")] | length == 0)' "$tmp/result.json" >/dev/null || {
    jq . "$tmp/result.json" >&2
    fail "a sandbox user whose primary group is 0 was not refused"
  }

  echo "ok: $runtime"
done
