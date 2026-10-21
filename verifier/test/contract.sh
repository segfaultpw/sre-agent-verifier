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
#   * a sandbox user that is root is refused;
#   * the runner never executes anything the unprivileged user can plant: the image is audited
#     for places that user can write (verifier/test/image_audit.sh: none on root's PATH, none
#     outside its home, /tmp, /var/tmp, /dev/shm and /dev/mqueue); a setup command plants an
#     executable named like every program the runner starts, in every directory it can write,
#     and none of them runs as root, with the image's own PATH and with a PATH that names a
#     directory the command made; and the runner refuses to start when a program it runs as root,
#     or a directory it looks it up in, can be changed by group or other.
#
# Session mode (runner version 3), with the same uid separation:
#
#   * a daemon a job starts is dead before the next job, and the next job's tree is the base
#     commit (a tracked file edited, an untracked file, a staged file, a planted git
#     configuration, hook and exclude file, a replace ref, an ignore rule the job wrote itself,
#     all gone; an ignored cache kept), restored by the unprivileged user; and so is what the
#     job left in the places outside the tree (its home, /tmp, /var/tmp, /dev/shm, /run/lock, a SysV shared
#     memory segment), while what setup left there unchanged stays;
#   * the job clock holds against a command that ignores SIGTERM and starts a daemon;
#   * `setup` never reruns, a job naming an index outside the snapshot, or carrying a command
#     text that is not the snapshot's, is refused (and the text never runs), the
#     runner exits on `410` and when idle, and no URL or secret is on any command line
#     (a scanner running as root for the whole run looks) or in a command's environment;
#   * the network rule, through a local proxy that admits only the secrets the platform
#     lists as live: a job command that opens a connection to an allowed host fails after
#     setup when the session says off and succeeds when on, the secret a setup command saved
#     is refused once setup is done (for the baseline, which runs right after setup, as
#     for every later command), and no secret is in a job command's environment.
#
# The go image also has a toolchain floor: Go 1.26.10 or later (the release that fixes the
# standard library advisories the scan reported), checked with `go version` in the image.
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

# Ends the servers a case started and removes the scratch directory (files the container
# created are root's, so a container removes them).
cleanup() {
  local pid
  for pid in "${server_pid:-}" "${proxy_pid:-}" "${allowed_pid:-}"; do
    [ -z "$pid" ] || kill "$pid" 2>/dev/null || true
  done
  docker run --rm -v "$tmp:/t" busybox sh -c "rm -rf /t/* /t/.[!.]*" >/dev/null 2>&1 || true
  rmdir "$tmp" 2>/dev/null || true
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

# A scanner that runs as root for the whole run, as the container's first process (the runner
# is exec'd after it): it looks at every command line on the machine, the runner's own curl,
# git and jq among them, for the pieces of the URLs and secrets of a session, and writes
# the matching lines to /scan/found, except the two bare words the run cases' own scanning daemon
# (see `daemon` below) looks for. Each pattern has a character class in it, so the scanner's own
# command line does not match what it looks for.
scan_script='eval "${VERIFIER_TEST_PRELUDE:-:}"
( while :; do cat /proc/[0-9]*/cmdline 2>/dev/null | tr "\000" "\n" | grep -a -e "127[.]0[.]0[.]1:" -e "ghs_contrac[t]" -e "SIGSECRE[T]" -e "RUNNERCRE[D]" -e "SETUPCRE[D]" -e "CHECKSCRE[D]" -e "job[s]/" -e "session[s]/" -e "afte[r]=" | grep -a -v -x -e "127[.]0[.]0[.]1:" -e "ghs_contrac[t]" >>/scan/found; sleep 0.01; done ) >/dev/null 2>&1 &
exec /usr/local/bin/verifier-run'

jq_defs='def step($i; $n; $k; $c): {index: $i, name: $n, kind: $k, cmd: $c};
  def job($seq; $steps; $files): {seq: $seq, files: $files, steps: $steps,
    result_url: ("http://127.0.0.1:@PORT@/jobs/\($seq)/result")};
  def session($end; $sha; $steps; $extra): {
    session: ({ready_url: "http://127.0.0.1:@PORT@/sessions/s1/ready",
               next_url: "http://127.0.0.1:@PORT@/sessions/s1/next",
               end_at: $end, idle_seconds: 120, job_seconds: 120, setup_seconds: 300} + ($extra.session // {})),
    repo_url: "file:///origin", repo_token: "ghs_contract_read_only_token", base_sha: $sha,
    steps: $steps, max_output_bytes: 100000} + ($extra.bootstrap // {});'

# run_session TAG TMP PORT SCENARIO RESULTS [DOCKER_ARG ...]
# Starts the loopback platform on PORT in scenario mode, runs the image once against a session
# bootstrap and leaves the platform's records in RESULTS (ready.json, result-<seq>.json,
# events.jsonl). The scanner above runs as root in the container; its findings are in
# RESULTS/scan/found. Leaves the seconds the container took in session_seconds.
session_seconds=0
run_session() {
  local tag=$1 tmp=$2 port=$3 scenario=$4 results=$5
  shift 5
  local docker_args=("$@")
  mkdir -p "$results/scan"
  : >"$results/events.jsonl"
  python3 "$here/test/fake_platform.py" --scenario "$scenario" --port "$port" --results "$results" --log "$results/events.jsonl" &
  server_pid=$!
  sleep 1
  local started
  started=$(date +%s)
  docker run --rm --network host \
    -v "$tmp/origin:/origin:ro" -v "$results/scan:/scan" \
    -e "BOOTSTRAP_URL=http://127.0.0.1:$port/webhooks/verifier/sessions/s1/SIGSECRET/bootstrap" \
    -e "AWS_SECRET_ACCESS_KEY=leak-aws" \
    --entrypoint /bin/sh \
    "${docker_args[@]}" \
    "$tag" -c "$scan_script" || fail "the runner exited non-zero"
  session_seconds=$(($(date +%s) - started))
  kill "$server_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
  server_pid=""
  if [ -s "$results/scan/found" ]; then
    cat "$results/scan/found" >&2
    fail "the scanner saw a URL or a secret of the session on a command line"
  fi
}

# expect_status FILE NAME STATUS WHAT: the first step named NAME in FILE has status STATUS.
expect_status() {
  local got
  got=$(jq -r --arg n "$2" '[.steps[] | select(.name == $n)][0].status' "$1")
  if [ "$got" != "$3" ]; then
    jq . "$1" >&2
    fail "$4: step $2 was '$got', not $3"
  fi
}

# expect_all_pass FILE WHAT
expect_all_pass() {
  if ! jq -e '[.steps[] | select(.status != "pass")] | length == 0' "$1" >/dev/null; then
    jq . "$1" >&2
    fail "$2: not every step passed"
  fi
}

# session_cases TAG TMP SHA: runner version 3's session mode inside the image, with the real
# uid separation, against the fixture repository in $TMP/origin.
session_cases() {
  local tag=$1 tmp=$2 sha=$3
  local end results scenario port
  end=$(date -u -d '+30 minutes' +%Y-%m-%dT%H:%M:%SZ)

  # Jobs 1 and 2 of the first session: job 1 edits a tracked file, leaves an untracked one, an
  # ignored cache file, a staged file, a planted git configuration, hook and exclude file, and
  # starts the daemons of the run cases; job 2 sees none of it, and the cache.
  local tamper restored sprobe
  tamper='echo tampered >README && echo stray >stray.txt && echo c >cache/job1-cache && echo y >staged.txt && git add staged.txt && rm calc.sh && echo "*" >>.git/info/exclude && echo h >hidden.txt && git config filter.slow.smudge "sleep 300; cat" && mkdir -p .git/hooks "$HOME/hooks2" && git config core.hooksPath "$HOME/hooks2" && printf "#!/bin/sh\ntouch \"$HOME/ran-hook\"\n" >.git/hooks/post-checkout && chmod +x .git/hooks/post-checkout'
  # The places outside the tree, an ignore rule of the job's own, and a replace ref that points the
  # base commit at a commit the job made.
  tamper+=' && mkdir -p planted /tmp/job1-dir && printf "*\\n" >planted/.gitignore && echo p >planted/calc.sh && echo e >"$HOME/.npmrc" && echo e >/tmp/job1-dir/f && echo e >/var/tmp/job1-var && echo e >/dev/shm/job1-shm && echo e >/run/lock/job1-lock && echo more >>/tmp/setup-mod && ipcmk --shmem 1024 >/dev/null && git -c user.name=t -c user.email=t@t commit -q -m evil && git replace '"$sha"' HEAD'
  restored='test "$(cat README)" = original || { echo "restored: README"; exit 1; }; test -f calc.sh || { echo "restored: calc.sh"; exit 1; }; ! test -e stray.txt || { echo "restored: stray"; exit 1; }; ! test -e staged.txt || { echo "restored: staged"; exit 1; }; ! test -e hidden.txt || { echo "restored: hidden"; exit 1; }; test -e cache/job1-cache || { echo "restored: cache"; exit 1; }; test -z "$(git status --porcelain --untracked-files=all | grep -v " test_new[.]sh$")" || { echo "restored: status: $(git status --porcelain --untracked-files=all | head -5)"; exit 1; }; ! git config --get filter.slow.smudge || { echo "restored: filter"; exit 1; }; ! git config --get core.hooksPath || { echo "restored: hooksPath"; exit 1; }; ! test -e "$HOME/ran-hook" || { echo "restored: hook ran"; exit 1; }; test "$(stat -c %u README)" = "$(id -u)" || { echo "restored: README owner"; exit 1; }; test "$(stat -c %u .git/config)" = "$(id -u)" || { echo "restored: config owner"; exit 1; }; ! test -e planted || { echo "restored: job-made ignore rule"; exit 1; }; test -z "$(git replace -l)" || { echo "restored: replace ref"; exit 1; }; ! test -e "$HOME/.npmrc" || { echo "restored: home"; exit 1; }; ! test -e /tmp/job1-dir || { echo "restored: /tmp"; exit 1; }; ! test -e /var/tmp/job1-var || { echo "restored: /var/tmp"; exit 1; }; ! test -e /dev/shm/job1-shm || { echo "restored: /dev/shm"; exit 1; }; ! test -e /run/lock/job1-lock || { echo "restored: /run/lock"; exit 1; }; test "$(ipcs -m | grep -c "^0x")" = 0 || { echo "restored: shared memory"; exit 1; }; test "$(cat /tmp/setup-keep)" = dep || { echo "restored: an unchanged setup file"; exit 1; }; ! test -e /tmp/setup-mod || { echo "restored: a changed setup file"; exit 1; }; test "$(cat "$HOME/.cache/dep")" = dep || { echo "restored: a setup home file"; exit 1; }; exit 0'
  sprobe='test -z "${BOOTSTRAP_URL:-}${REPO_TOKEN:-}${RESULT_URL:-}${NEXT_URL:-}${READY_URL:-}${AWS_SECRET_ACCESS_KEY:-}" && test -z "$(env | grep -E "leak|ghs_|127.0.0.1|bootstrap|SIGSECRE[T]|session[s]|job[s]/")"'

  results=$(mktemp -d -p "$tmp")
  scenario="$results/scenario.json"
  port=$((20000 + RANDOM % 20000))
  jq -n --arg end "$end" --arg sha "$sha" --arg daemon "$daemon" --arg tamper "$tamper" \
    --arg restored "$restored" --arg leftover "$leftover_probe" --arg probe "$sprobe" \
    --arg proc "$proc_probe" --arg dirp "$dir_probe" "$jq_defs"'
    [step(0; "deps"; "setup"; "mkdir -p cache \"$HOME/.cache\" && echo run >> cache/setup-runs && echo dep >/tmp/setup-keep && echo dep >/tmp/setup-mod && echo dep >\"$HOME/.cache/dep\""),
     step(1; "tests"; "test"; "sh test_old.sh"),
     step(2; "tamper"; "check"; $tamper),
     step(3; "daemon"; "check"; $daemon),
     step(4; "leftover"; "check"; $leftover),
     step(5; "restored"; "check"; $restored),
     step(6; "env_probe"; "check"; $probe),
     step(7; "proc_probe"; "check"; $proc),
     step(8; "dir_probe"; "check"; $dirp),
     step(9; "count"; "check"; "test \"$(grep -c run cache/setup-runs)\" = 1"),
     step(10; "owner"; "check"; "test \"$(stat -c %u test_new.sh)\" = \"$(id -u)\""),
     step(11; "noghost"; "check"; "! test -e ghost_from_text")] as $steps
    | {bootstrap: session($end; $sha; $steps; {}),
       next: [{status: 200, job: job(1; [$steps[2], $steps[3]]; [{path: "calc.sh", content: "echo 4\n", test: false}])},
              {status: 200, job: job(2; $steps[4:]; [{path: "test_new.sh", content: "true\n", test: true}])},
              {status: 200, job: job(3; [step(0; "deps"; "setup"; "echo run >> cache/setup-runs")]; [])},
              {status: 200, job: job(4; [$steps[9]]; [])},
              {status: 200, job: job(5; [step(9; "count"; "check"; "touch ghost_from_text")]; [])},
              {status: 200, job: job(6; [$steps[11]]; [])}]}' >"$scenario"
  # /run/lock is made writable for this case (the images make it unwritable, and a runtime may not).
  run_session "$tag" "$tmp" "$port" "$scenario" "$results" -e "VERIFIER_TEST_PRELUDE=chmod 1777 /run/lock"
  expect_all_pass "$results/ready.json" "the session boot"
  jq -e '.baseline_green == true and .runner.version == 3' "$results/ready.json" >/dev/null || fail "the ready report is not green, version 3"
  expect_all_pass "$results/result-1.json" "job 1"
  expect_all_pass "$results/result-2.json" "job 2 (a daemon from job 1 dead, the tree restored as the base commit)"
  expect_all_pass "$results/result-4.json" "job 4 (setup did not rerun)"
  jq -e '.steps == [] and (.runner_error | test("snapshot"))' "$results/result-5.json" >/dev/null || {
    jq . "$results/result-5.json" >&2
    fail "a job carrying a command text of its own was not refused"
  }
  expect_all_pass "$results/result-6.json" "job 6 (the text of job 5 never ran)"
  jq -e '.steps == [] and (.runner_error | test("snapshot"))' "$results/result-3.json" >/dev/null || {
    jq . "$results/result-3.json" >&2
    fail "a job naming a setup command was not refused"
  }
  jq -e '[.[] | select(.path != null and (.path | endswith("/next")))] | length == 7' <(jq -s . "$results/events.jsonl") >/dev/null || {
    jq -s . "$results/events.jsonl" >&2
    fail "the runner did not ask for the next job six times and stop at the 410"
  }
  if grep -q 'ghs_contract_read_only_token\|leak-aws\|127.0.0.1\|SIGSECRET' "$results/ready.json" "$results"/result-*.json; then
    fail "a secret or a URL leaked into a report"
  fi

  # The job clock holds against a command that ignores SIGTERM and starts a daemon that does
  # too: the job is a timeout, the session ends, and the runner is gone well inside the bound.
  results=$(mktemp -d -p "$tmp")
  scenario="$results/scenario.json"
  port=$((20000 + RANDOM % 20000))
  jq -n --arg end "$end" --arg sha "$sha" "$jq_defs"'
    [step(0; "tests"; "test"; "sh test_old.sh"),
     step(1; "stubborn"; "check"; "trap \"\" TERM; setsid sh -c \"trap \\\"\\\" TERM; sleep 300\" >/dev/null 2>&1 & while true; do sleep 1; done"),
     step(2; "lint"; "check"; "echo lint")] as $steps
    | {bootstrap: session($end; $sha; $steps; {session: {job_seconds: 6}}),
       next: [{status: 200, job: job(1; [$steps[1], $steps[2]]; [])}, {status: 200, job: job(2; [$steps[2]]; [])}]}' >"$scenario"
  run_session "$tag" "$tmp" "$port" "$scenario" "$results"
  expect_status "$results/result-1.json" stubborn timeout "the job clock"
  [ "$session_seconds" -lt 60 ] || fail "a command that ignores SIGTERM held the session past its job clock ($session_seconds s)"
  [ ! -e "$results/result-2.json" ] || fail "a session whose job timed out went on to another job"

  # Idle: no job for idle_seconds ends the runner, and the 204 answers are not hammered.
  results=$(mktemp -d -p "$tmp")
  scenario="$results/scenario.json"
  port=$((20000 + RANDOM % 20000))
  jq -n --arg end "$end" --arg sha "$sha" "$jq_defs"'
    [step(0; "tests"; "test"; "sh test_old.sh"), step(1; "lint"; "check"; "echo lint")] as $steps
    | {bootstrap: session($end; $sha; $steps; {session: {idle_seconds: 4}}),
       next: [{status: 200, job: job(1; [$steps[1]]; [])}], exhausted_status: 204}' >"$scenario"
  run_session "$tag" "$tmp" "$port" "$scenario" "$results"
  expect_all_pass "$results/result-1.json" "the job before the idle exit"
  [ "$session_seconds" -lt 40 ] || fail "an idle session was not ended by its idle limit ($session_seconds s)"
}

# network_cases TAG TMP SHA: the network rule through a local proxy that admits only what the
# fake platform lists as live (see fake_proxy.py), with the allowed host nameable only by it.
network_cases() {
  local tag=$1 tmp=$2 sha=$3
  local end network results scenario port pport aport reach tunnel saved dead env_probe platform_probe checks_on
  end=$(date -u -d '+30 minutes' +%Y-%m-%dT%H:%M:%SZ)

  for network in off on; do
    results=$(mktemp -d -p "$tmp")
    scenario="$results/scenario.json"
    port=$((20000 + RANDOM % 20000))
    pport=$((port + 1))
    aport=$((port + 2))
    mkdir -p "$results/allowed"
    printf 'allowed-ok' >"$results/allowed/index.html"
    python3 -m http.server "$aport" --bind 127.0.0.1 --directory "$results/allowed" >/dev/null 2>&1 &
    allowed_pid=$!
    python3 "$here/test/fake_proxy.py" --port "$pport" --authorize "http://127.0.0.1:$port/authorize" \
      --hosts "bootstrap=127.0.0.1:$port" --hosts "runner=127.0.0.1:$port" \
      --hosts "setup=allowed.test:$aport" --hosts "checks=allowed.test:$aport" \
      --map allowed.test=127.0.0.1 --log "$results/proxy.jsonl" &
    proxy_pid=$!

    # The proxy settings reach curl through the environment the runner built, never as an
    # argument, so the scanner above has nothing of the secrets to find on a command line.
    reach="curl -sS -f --max-time 10 http://allowed.test:$aport/ | grep -q allowed-ok"
    tunnel="curl -sS -f --max-time 10 --proxytunnel http://allowed.test:$aport/ | grep -q allowed-ok"
    saved="printf %s \"\$HTTPS_PROXY\" >\"\$HOME/cred\" && $reach"
    dead="test -s \"\$HOME/cred\" && ! http_proxy=\"\$(cat \"\$HOME/cred\")\" curl -sS -f --max-time 10 http://allowed.test:$aport/"
    # A baseline command (a `test`) runs right after setup, before the ready report: the secret the
    # setup command saved must already be dead for it.
    stolen="$dead"
    # The platform's address is built here and handed to curl on its standard input, so it is on no
    # command line either.
    platform_probe="h=127.0.0; ! printf 'url = \"http://%s.1:%s/%s/s1/next\"\\n' \"\$h\" $port sessions | curl -sS -f --max-time 10 --noproxy x.invalid -K -"
    if [ "$network" = on ]; then
      checks_on=true
      env_probe='test -n "$HTTPS_PROXY" && test -z "$(env | grep -E "SETUPCRE[D]|RUNNERCRE[D]|SIGSECRE[T]")" && env | grep -q CHECKSCRE[D]'
    else
      checks_on=false
      env_probe='test -z "$(env | grep -i -E "proxy|SETUPCRE[D]|CHECKSCRE[D]|RUNNERCRE[D]|SIGSECRE[T]")"'
    fi

    jq -n --arg end "$end" --arg sha "$sha" --arg saved "$saved" --arg reach "$reach" --arg tunnel "$tunnel" \
      --arg dead "$dead" --arg stolen "$stolen" --arg env_probe "$env_probe" --arg platform "$platform_probe" \
      --argjson on "$checks_on" "$jq_defs"'
      [step(0; "save"; "setup"; $saved),
       step(1; "tests"; "test"; "sh test_old.sh"),
       step(2; "reach"; "check"; $reach),
       step(3; "tunnel"; "check"; $tunnel),
       step(4; "dead"; "check"; $dead),
       step(5; "env_probe"; "check"; $env_probe),
       step(6; "platform"; "check"; $platform),
       step(7; "stolen"; "test"; $stolen)] as $steps
      | {bootstrap: session($end; $sha; $steps;
           {session: {setup_done_url: "http://127.0.0.1:@PORT@/sessions/s1/setup_done"},
            bootstrap: {network: $on,
                        proxy: {user: "session:s1", runner: "RUNNERCRED", setup: "SETUPCRED"}}}),
         credentials: ([{user: "session:s1", password: "SIGSECRET", kind: "bootstrap"},
                        {user: "session:s1", password: "RUNNERCRED", kind: "runner"},
                        {user: "session:s1", password: "SETUPCRED", kind: "setup", until: "setup_done"}]
                       + (if $on then [{user: "session:s1", password: "CHECKSCRED", kind: "checks", from: "setup_done"}] else [] end)),
         setup_done_answer: (if $on then {proxy: {checks: "CHECKSCRED"}} else {} end),
         next: [{status: 200, job: job(1; (if $on then $steps[2:7] else $steps[2:6] end); [])}]}' >"$scenario"
    run_session "$tag" "$tmp" "$port" "$scenario" "$results" -e "VERIFIER_PROXY_URL=http://127.0.0.1:$pport"
    kill "$proxy_pid" "$allowed_pid" 2>/dev/null || true
    proxy_pid=""
    allowed_pid=""

    expect_all_pass "$results/ready.json" "the boot with the network $network (the setup secret reaches the allowed host, and is dead for the baseline)"
    expect_status "$results/ready.json" baseline pass "the baseline, the setup secret saved by a setup command being dead for it, network $network"
    expect_status "$results/result-1.json" dead pass "the setup secret saved by a setup command, network $network"
    expect_status "$results/result-1.json" env_probe pass "the job environment, network $network"
    if [ "$network" = on ]; then
      expect_status "$results/result-1.json" reach pass "a job command reaching an allowed host, network on"
      expect_status "$results/result-1.json" tunnel pass "a job command tunnelling to an allowed host, network on"
      expect_status "$results/result-1.json" platform pass "a job command reaching the platform host, network on"
      jq -e -s '[.[] | select(.kind == "checks" and .allowed == true and .host == ("allowed.test:'"$aport"'"))] | length >= 2' "$results/proxy.jsonl" >/dev/null || {
        cat "$results/proxy.jsonl" >&2
        fail "the proxy did not admit the checks secret to the allowed host"
      }
    else
      expect_status "$results/result-1.json" reach fail "a job command reaching an allowed host, network off"
      expect_status "$results/result-1.json" tunnel fail "a job command tunnelling to an allowed host, network off"
      if jq -e -s '[.[] | select(.kind == "checks")] | length > 0' "$results/proxy.jsonl" >/dev/null; then
        cat "$results/proxy.jsonl" >&2
        fail "the proxy saw a checks secret with the network off"
      fi
    fi
    jq -e -s '[.[] | select(.kind == "setup" and .allowed == true)] | length >= 1' "$results/proxy.jsonl" >/dev/null || fail "the setup secret never reached the allowed host"
    jq -e -s '[.[] | select(.allowed == false and .host == ("allowed.test:'"$aport"'"))] | length >= 1' "$results/proxy.jsonl" >/dev/null || fail "the saved setup secret was not refused once setup was done"
    jq -e -s '[.[] | select(.kind == "bootstrap")] | length == 1' "$results/proxy.jsonl" >/dev/null || fail "the bootstrap did not go through the proxy on the URL's signature"
  done
}

# root_exec_cases TAG TMP SHA: the runner is root and a command is not, so nothing the command can
# write may ever be executed by the runner. A setup command (the unprivileged user) plants an
# executable named like every program the runner starts, in every directory it can write; each
# plant marks / (which only root can write) and then runs the real program, so a runner that
# executes one goes on working and is caught by the marker. Two runs: the image's own PATH, and a
# PATH that names a directory the command made first (as an image or a task definition could set
# it). Two jobs follow the boot, so the calls the restore, the report and the job make are
# covered as well as those of the boot.
root_exec_cases() {
  local tag=$1 tmp=$2 sha=$3
  local end tools plant image_path variant results scenario port extra
  end=$(date -u -d '+30 minutes' +%Y-%m-%dT%H:%M:%SZ)
  tools=$(sed -n '/^ROOT_TOOLS=(/,/^)/p' "$here/run.sh" | sed '1d;$d' | tr -s ' \n' ' ')
  [ -n "$tools" ] || fail "run.sh lists no ROOT_TOOLS"
  plant='mkdir -p /tmp/evilbin "$HOME/evilbin" && real="" && for t in '"$tools"'; do r=$(PATH=/usr/bin:/bin:/usr/sbin:/sbin command -v "$t") && real="$real $t=$r"; done && for d in $(find / \( -path /proc -o -path /sys \) -prune -o -type d -writable -print 2>/dev/null; find "$HOME" -type d -writable 2>/dev/null); do for pair in $real; do t=${pair%%=*}; r=${pair#*=}; printf "#!/bin/sh\n: >/pwned-root-%s 2>/dev/null\nexec %s \"\$@\"\n" "$t" "$r" >"$d/$t" 2>/dev/null && chmod +x "$d/$t" 2>/dev/null; done; done; test -n "$real"'
  image_path=$(docker image inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$tag" | sed -n 's/^PATH=//p')
  [ -n "$image_path" ] || fail "the image has no PATH"

  for variant in image_path made_dir_first; do
    extra=()
    if [ "$variant" = made_dir_first ]; then extra=(-e "PATH=/tmp/evilbin:$image_path"); fi
    results=$(mktemp -d -p "$tmp")
    scenario="$results/scenario.json"
    port=$((20000 + RANDOM % 20000))
    jq -n --arg end "$end" --arg sha "$sha" --arg plant "$plant" "$jq_defs"'
      [step(0; "plant"; "setup"; $plant),
       step(1; "tests"; "test"; "sh test_old.sh"),
       step(2; "planted"; "check"; "test -x /tmp/evilbin/jq && test -x \"$HOME/evilbin/wc\" && test -x /tmp/evilbin/find"),
       step(3; "clean"; "check"; "for f in /pwned-root-*; do if [ -e \"$f\" ]; then echo \"ran as root: $f\"; exit 1; fi; done; exit 0")] as $steps
      | {bootstrap: session($end; $sha; $steps; {}),
         next: [{status: 200, job: job(1; [$steps[2], $steps[3]]; [])},
                {status: 200, job: job(2; [$steps[3]]; [])}]}' >"$scenario"
    run_session "$tag" "$tmp" "$port" "$scenario" "$results" "${extra[@]}"
    expect_all_pass "$results/ready.json" "the boot with a binary planted in every writable directory ($variant)"
    expect_all_pass "$results/result-1.json" "job 1: the runner executed nothing the repository planted ($variant)"
    expect_all_pass "$results/result-2.json" "job 2: the runner executed nothing the repository planted ($variant)"
  done
}

# trust_cases TAG: the runner refuses to start when a program it runs as root, or a directory it
# looks the program up in, can be changed by group or other (an image defect: nothing of the
# repository has run when this is checked).
trust_cases() {
  local tag=$1 breakage out
  for breakage in \
    'chmod 0775 /usr/bin/jq' \
    'chmod 1777 /usr/local/bin' \
    "printf '#!/bin/sh\\n' >/tmp/planted && chmod 0755 /tmp/planted && ln -sf /tmp/planted /usr/local/bin/wc"; do
    out=$(docker run --rm --network host -e BOOTSTRAP_URL=http://127.0.0.1:1/bootstrap --entrypoint /bin/sh "$tag" \
      -c "$breakage; /usr/local/bin/verifier-run; echo status=\$?" 2>&1) || true
    # stdout and stderr reach `docker run` as two streams, so the refusal (stderr) and the
    # status line (stdout) can arrive in either order; each is looked for on its own.
    if [[ "$out" != *"refusing to run"* ]] || [[ "$out" != *"status=2"* ]]; then
      printf '%s\n' "$out" >&2
      fail "the runner started although: $breakage"
    fi
  done
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

  if [ "$runtime" = go ]; then
    go_version=$(docker run --rm --entrypoint go "$tag" version) || fail "go version failed in the go image"
    go_release=$(sed -n 's/^go version go\(1\.[0-9][0-9]*\(\.[0-9][0-9]*\)\{0,1\}\).*/\1/p' <<<"$go_version")
    [ -n "$go_release" ] || fail "could not read a Go release from: $go_version"
    go_minor=$(cut -d. -f2 <<<"$go_release")
    go_patch=$(cut -d. -f3 <<<"$go_release")
    if [ "$go_minor" -lt 26 ] || { [ "$go_minor" -eq 26 ] && [ "${go_patch:-0}" -lt 10 ]; }; then
      fail "the go image has go$go_release, below the go1.26.10 floor"
    fi
  fi

  docker run --rm -v "$here/test/image_audit.sh:/audit.sh:ro" --entrypoint bash "$tag" /audit.sh ||
    fail "the $runtime image lets the unprivileged user write a place the runner executes from (see above)"

  tmp=$(mktemp -d)
  trap cleanup EXIT

  origin="$tmp/origin"
  mkdir -p "$origin"
  printf 'echo 5\n' >"$origin/calc.sh"
  printf 'test -f calc.sh\n' >"$origin/test_old.sh"
  # The repository's own attributes: the filter named here is defined (or not) by what a
  # command writes into .git/config, which is how a blocking git operation is planted.
  printf 'test_old.sh filter=slow\n' >"$origin/.gitattributes"
  # A tracked file a session job can edit, and a cache directory setup fills that git ignores.
  printf 'original\n' >"$origin/README"
  printf 'cache/\n' >"$origin/.gitignore"
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

  session_cases "$tag" "$tmp" "$sha"
  network_cases "$tag" "$tmp" "$sha"
  root_exec_cases "$tag" "$tmp" "$sha"
  trust_cases "$tag"

  echo "ok: $runtime"
done
