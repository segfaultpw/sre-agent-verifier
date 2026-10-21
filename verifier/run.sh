#!/usr/bin/env bash
# The verifier runner. One run, one repository, one patch, then it exits. Version 3 adds
# session mode: the same image can also serve a sequence of jobs for one fix, over one
# clone, until the platform ends the session (see "Session mode" in verifier/contract.md).
# The contract it implements is verifier/contract.md; read that first.
#
# Trust model in one paragraph: this script holds a read-only token and a one-time
# result URL and runs commands written by somebody else. Those commands never run as
# this process and never see its environment: each one is started as an unprivileged
# user (when this script is root, as it is in the images) in its own session, with a
# whitelisted, rebuilt environment, and EVERYTHING it started is killed when the step
# ends, so nothing a command leaves behind can outlive its step, read what this
# process holds, or race this process's own work. The secrets are never on a command
# line and never in a step's environment: curl reads each URL from a mode-600 file
# only root can open, and git gets the token through its own environment for the one
# clone. Root never writes inside a directory the unprivileged user can write: the
# clone is staged in a root-owned directory and moved into place with one rename, and
# every later write to the tree (the patch, its removal) is made BY the unprivileged
# user. The runner's own few operations on the tree (git, a file write) are held to
# the same discipline as a step: the repository's commands can plant what they run (a
# git hook, a filter, a FIFO where a file goes), so each runs as the unprivileged user,
# in its own session, under the run's clock, with git's hooks and untrusted
# configuration switched off, and ends everything it started.

set -uo pipefail

log() { printf '[verifier] %s\n' "$*" >&2; }

# ---------------------------------------------------------------------------
# Inputs. The platform sets exactly one variable, BOOTSTRAP_URL, a single-claim URL
# that expires in minutes. The rest are knobs baked into the image or set by a test,
# never by the platform.
# ---------------------------------------------------------------------------
BOOTSTRAP_URL=${BOOTSTRAP_URL:-}
RUNTIME=${VERIFIER_RUNTIME:-unknown}
STEP_ENV_FILE=${VERIFIER_STEP_ENV_FILE:-/etc/verifier/step.env}
WORK=${VERIFIER_WORKDIR:-/work}
ALLOW_UNPRIVILEGED=${VERIFIER_ALLOW_UNPRIVILEGED:-0}
SANDBOX_USER=${VERIFIER_SANDBOX_USER:-sandbox}

# Proxy settings come from the task definition, not from the platform's response.
# They are forwarded to the steps (package managers need them), except on the hosted
# verifier, where VERIFIER_PROXY_URL names the egress proxy (host and port) and the
# credentials that decide what a command may reach come from the bootstrap.
PROXY_VARS=(HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy)
PROXY_ENDPOINT=""
if [ -n "${VERIFIER_PROXY_URL:-}" ]; then
  PROXY_ENDPOINT=${VERIFIER_PROXY_URL#http://}
  PROXY_ENDPOINT=${PROXY_ENDPOINT%/}
  if ! [[ "$PROXY_ENDPOINT" =~ ^[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]]; then
    printf '[verifier] VERIFIER_PROXY_URL is not a host and port\n' >&2
    exit 2
  fi
fi
RUNNER_VERSION=3

EXCERPT_FLOOR=400
RESULT_BUDGET=56000

if [ -z "$BOOTSTRAP_URL" ]; then
  log "BOOTSTRAP_URL is not set: nothing can be fetched and nothing can be reported"
  exit 2
fi

# bash's own seconds counter measures every deadline: nothing forks to read a clock.
SECONDS=0
OUT="$WORK/out"
REPO="$WORK/repo"
STAGE="$OUT/stage"
STEPS_FILE="$OUT/steps.ndjson"
BASELINE_GREEN=false
RUNNER_ERROR=""
N=0
# Defaults until the bootstrap says otherwise.
WALL_CLOCK_SECONDS=900
MAX_OUTPUT_BYTES=1000000

mkdir -p "$OUT" "$WORK/home"
chmod 0700 "$OUT"
: >"$STEPS_FILE"

# ---------------------------------------------------------------------------
# Running as somebody who cannot read this process.
# ---------------------------------------------------------------------------
SANDBOX_UID=""
SANDBOX_GID=""
SANDBOX_HOME="$WORK/home"
SETPRIV=""
if [ "$(id -u)" = 0 ]; then
  if SANDBOX_UID=$(id -u "$SANDBOX_USER" 2>/dev/null) && SANDBOX_GID=$(id -g "$SANDBOX_USER" 2>/dev/null); then
    if [ "$SANDBOX_UID" = 0 ] || [ "$SANDBOX_GID" = 0 ]; then
      # Every command would run as root, and the uid-wide kill at the end of a step
      # would kill the runner itself; a primary group of 0 is root's group, which owns
      # what root's files are group-writable by.
      SANDBOX_UID=""
      log "the sandbox user '$SANDBOX_USER' is root or in root's group: refusing to run commands as it"
      RUNNER_ERROR="the image's sandbox user is root (uid or primary group 0), so commands cannot be separated from the runner"
    else
      install -d -o "$SANDBOX_UID" -g "$SANDBOX_GID" -m 0700 "$WORK/home"
      SETPRIV=$(command -v setpriv)
    fi
  else
    SANDBOX_UID=""
    log "no unprivileged user '$SANDBOX_USER' in this image"
    RUNNER_ERROR="the image has no unprivileged user to run commands as"
  fi
else
  if [ "$ALLOW_UNPRIVILEGED" != 1 ]; then
    log "not root: refusing to run commands in the same uid as the runner (set VERIFIER_ALLOW_UNPRIVILEGED=1 in a test)"
    RUNNER_ERROR="the runner was not started as root, so it cannot separate commands from its own secrets"
  fi
fi

BASE_ENV=("PATH=$PATH" "HOME=$SANDBOX_HOME" "LANG=C.UTF-8" "CI=true" "TERM=dumb" "GIT_TERMINAL_PROMPT=0")
TASK_PROXY_ENV=()
for name in "${PROXY_VARS[@]}"; do
  if [ -n "${!name:-}" ]; then TASK_PROXY_ENV+=("$name=${!name}"); fi
done
IMAGE_ENV=()
if [ -r "$STEP_ENV_FILE" ]; then
  while IFS= read -r line; do
    case "$line" in
      '' | '#'*) ;;
      [A-Za-z_]*=*) IMAGE_ENV+=("$line") ;;
    esac
  done <"$STEP_ENV_FILE"
fi

# What the commands may reach is decided per phase, by what they are handed:
#   setup     the install phase: the proxy settings of the task definition, or on the hosted
#             verifier the `setup` credential, which the platform revokes the moment setup is
#             over (setup_done_url), before the baseline or any other command;
#   checks    everything after setup, a session's baseline included: nothing, unless the
#             repository's setting allows it (the bootstrap's "network" key) and, on the hosted
#             verifier, the platform answered setup done with a `checks` credential.
# PROXY_USER, SETUP_CRED, CHECKS_CRED and NETWORK are filled in as the bootstrap and the
# platform's answers arrive. NETWORK is true, false, or legacy for a version 2 platform's
# run bootstrap, which has no such key and keeps the task definition's proxy everywhere.
STEP_PROXY_ENV=()
PROXY_USER=""
RUNNER_CRED=""
SETUP_CRED=""
CHECKS_CRED=""
NETWORK=legacy
RUNNER_PROXY_URL=""

# uri_encode STRING: percent-encodes everything but the unreserved characters, in the
# shell itself, because a credential must never be handed to another program as an
# argument. The result is in REPLY.
uri_encode() {
  local LC_ALL=C text=$1 out="" char hex i
  for ((i = 0; i < ${#text}; i++)); do
    char=${text:i:1}
    case "$char" in
      [A-Za-z0-9._~-]) out+=$char ;;
      *)
        printf -v hex '%%%02X' "'$char"
        out+=$hex
        ;;
    esac
  done
  REPLY=$out
}

# proxy_url USER SECRET: the hosted proxy's URL with the credential in it, in REPLY.
proxy_url() {
  local user secret
  uri_encode "$1"
  user=$REPLY
  uri_encode "$2"
  secret=$REPLY
  REPLY="http://$user:$secret@$PROXY_ENDPOINT"
}

# use_step_env PHASE: rebuilds the environment and the prefix every command of the
# repository runs under: the unprivileged user, no way to gain privileges, an empty
# capability set, and an environment rebuilt from a whitelist. Anything not on it does not
# exist for the command.
use_step_env() {
  local phase=$1 mode=none secret="" proxy_env=() url
  if [ -n "$PROXY_ENDPOINT" ] && [ -n "$PROXY_USER" ]; then
    case "$phase" in
      setup) if [ -n "$SETUP_CRED" ]; then mode=hosted secret=$SETUP_CRED; fi ;;
      checks) if [ -n "$CHECKS_CRED" ]; then mode=hosted secret=$CHECKS_CRED; fi ;;
    esac
  else
    case "$phase" in
      setup) mode=task ;;
      checks) if [ "$NETWORK" = true ] || [ "$NETWORK" = legacy ]; then mode=task; fi ;;
    esac
  fi
  case "$mode" in
    task) proxy_env=("${TASK_PROXY_ENV[@]}") ;;
    hosted)
      proxy_url "$PROXY_USER" "$secret"
      url=$REPLY
      # Loopback is never proxied: a repository's tests talk to services they start.
      proxy_env=("HTTP_PROXY=$url" "HTTPS_PROXY=$url" "http_proxy=$url" "https_proxy=$url"
        "NO_PROXY=localhost,127.0.0.1,::1" "no_proxy=localhost,127.0.0.1,::1")
      ;;
  esac
  STEP_ENV=("${BASE_ENV[@]}" "${IMAGE_ENV[@]}")
  STEP_PROXY_ENV=("${proxy_env[@]}")
  if [ -n "$SANDBOX_UID" ]; then
    SANDBOX_PREFIX=(setpriv --reuid="$SANDBOX_UID" --regid="$SANDBOX_GID" --clear-groups
      --no-new-privs --bounding-set=-all --inh-caps=-all -- env -i "${STEP_ENV[@]}")
  else
    SANDBOX_PREFIX=(env -i "${STEP_ENV[@]}")
  fi
}
use_step_env setup

# The runner's own operations on the tree (a git command, a file write). A smaller
# environment than a step's, and none of what lets git read configuration the
# repository's commands could have written: no system or global git configuration, and
# a home that does not exist. `-c` settings outrank the repository's own config, so
# hooks and the file system monitor stay off whatever `.git/config` says.
OP_ENV=("PATH=$PATH" "HOME=/nonexistent" "LANG=C.UTF-8" "GIT_TERMINAL_PROMPT=0"
  "GIT_CONFIG_NOSYSTEM=1" "GIT_CONFIG_GLOBAL=/dev/null" "GIT_OPTIONAL_LOCKS=0")
if [ -n "$SANDBOX_UID" ]; then
  OP_PREFIX=(setpriv --reuid="$SANDBOX_UID" --regid="$SANDBOX_GID" --clear-groups
    --no-new-privs --bounding-set=-all --inh-caps=-all -- env -i "${OP_ENV[@]}")
else
  OP_PREFIX=(env -i "${OP_ENV[@]}")
fi
GIT=(git -c core.hooksPath=/dev/null -c core.fsmonitor=false)

# ---------------------------------------------------------------------------
# Nothing a step started may outlive it.
# ---------------------------------------------------------------------------

# Whether any process of the unprivileged user is still running (a zombie is not).
sandbox_alive() {
  [ -n "$SANDBOX_UID" ] || return 1
  local dir uid state line
  for dir in /proc/[0-9]*; do
    uid="" state=""
    while IFS= read -r line; do
      case "$line" in
        Uid:*) read -r _ uid _ <<<"$line" ;;
        State:*) read -r _ state _ <<<"$line" ;;
      esac
    # stderr first: a process that vanishes between the listing and the read must
    # not leave a message behind
    done 2>/dev/null <"$dir/status"
    if [ "$uid" = "$SANDBOX_UID" ] && [ "$state" != Z ]; then return 0; fi
  done
  return 1
}

# End everything a step started: its process group first (TERM, then KILL), then
# every process the unprivileged user still owns, which catches a daemon that left
# the group with its own session. Only ever the dedicated user: when the runner is
# not root (a test) the group is all there is, and nothing else is signalled.
reap_step() {
  local pid=$1 waited=0
  kill -TERM -- "-$pid" 2>/dev/null
  while kill -0 -- "-$pid" 2>/dev/null && [ "$waited" -lt 20 ]; do
    sleep 0.1
    waited=$((waited + 1))
  done
  kill -KILL -- "-$pid" 2>/dev/null
  if [ -n "$SANDBOX_UID" ]; then
    # `env -i` runs first, as root: the process that becomes the unprivileged user
    # never held the runner's variables where another process of that user, one that is
    # about to be killed, could read them from /proc.
    env -i "$SETPRIV" --reuid="$SANDBOX_UID" --regid="$SANDBOX_GID" --clear-groups -- \
      "$BASH" -c 'kill -s KILL -- -1' 2>/dev/null
    waited=0
    while sandbox_alive && [ "$waited" -lt 100 ]; do
      sleep 0.03
      waited=$((waited + 1))
    done
  fi
}

# sandbox_op ARGV...: one short operation on the tree (a git command, a file write) as
# the unprivileged user, from the repository. Held to what a step is held to: its own
# session, the time the run has left, and everything it started is ended when it
# returns. Answers the command's status, or 124 when the run's clock ran out first.
sandbox_op() {
  local pid rc timed_out=0
  if [ "$((DEADLINE - SECONDS))" -le 0 ]; then return 124; fi
  (cd "$REPO" && exec setsid "${OP_PREFIX[@]}" "$@" </dev/null >/dev/null 2>&1) &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$SECONDS" -ge "$DEADLINE" ]; then
      timed_out=1
      break
    fi
    sleep 0.02
  done
  reap_step "$pid"
  wait "$pid" 2>/dev/null
  rc=$?
  if [ "$timed_out" -eq 1 ]; then return 124; fi
  return "$rc"
}

# ---------------------------------------------------------------------------
# Recording steps.
# ---------------------------------------------------------------------------
now_ms() { date +%s%3N; }

# record NAME STATUS DURATION_MS EXCERPT [EXIT_CODE] [INDEX] [LIMIT]
# INDEX is the command's index in the run's list, never its text.
record() {
  local name=$1 status=$2 duration=$3 text=$4 exit_code=${5:-} index=${6:-} limit=${7:-}
  local args=(--arg name "$name" --arg status "$status" --argjson duration "$duration" --arg excerpt "$text")
  local filter='{name: $name, status: $status, duration_ms: $duration, excerpt: $excerpt}'
  if [ -n "$exit_code" ]; then
    args+=(--argjson exit_code "$exit_code")
    filter="$filter + {exit_code: \$exit_code}"
  fi
  if [ -n "$index" ]; then
    args+=(--argjson index "$index")
    filter="$filter + {index: \$index}"
  fi
  if [ -n "$limit" ]; then
    args+=(--arg limit "$limit")
    filter="$filter + {limit: \$limit}"
  fi
  jq -nc "${args[@]}" "$filter" >>"$STEPS_FILE"
}

# The tail of a log, which is where a failure is. Never more than the cap the
# platform accepts per step, and never a NUL byte.
tail_of() { tail -c 3500 "$1" 2>/dev/null | tr -d '\000'; }

# run_step NAME COMMAND INDEX [yes] [record]
# Runs COMMAND in the repository as the unprivileged user, in its own session,
# bounded by the time left and by MAX_OUTPUT_BYTES, and ends everything it started
# when it returns. The optional fourth argument inverts the judgement: the step
# passes when the command FAILS (the new test on the unmodified tree). With a fifth
# argument of "no" nothing is recorded and the caller reads the LAST_* variables
# (the baseline folds several commands into one reported step).
# Sets LAST_STATUS to pass|fail|timeout|limit|error, plus LAST_DURATION, LAST_EXCERPT,
# LAST_RC, LAST_LIMIT.
LAST_STATUS=""
LAST_DURATION=0
LAST_EXCERPT=""
LAST_RC=""
LAST_LIMIT=""
run_step() {
  local name=$1 cmd=$2 index=${3:-} expect_failure=${4:-no} do_record=${5:-yes}
  local remaining=$((DEADLINE - SECONDS))
  N=$((N + 1))
  local log="$OUT/step-$N.log" fifo="$OUT/step-$N.fifo"
  : >"$log"
  LAST_LIMIT=""

  if [ "$remaining" -le 0 ]; then
    LAST_STATUS=timeout
    LAST_DURATION=0
    LAST_EXCERPT="the wall clock was used up before this step started"
    LAST_RC=""
    if [ "$do_record" = yes ]; then record "$name" timeout 0 "$LAST_EXCERPT" "" "$index"; fi
    return 0
  fi

  local started pid reader rc status size duration timed_out=0 spent=0 nap=0.05
  started=$(now_ms)

  # The output goes through a pipe the runner owns, read by `head`, which stops at the
  # cap; the step's own writes past it fail. The reader is a child of this process
  # (not of the step), so it outlives a step that is killed and ends when the last
  # writer is gone.
  mkfifo -m 0600 "$fifo"
  head -c "$MAX_OUTPUT_BYTES" <"$fifo" >"$log" &
  reader=$!

  # A new session, so one kill to the negative PID reaches the whole group. A background job is not
  # a process group leader, so setsid does not fork and PID is the step's own.
  # The proxy settings of the command (they can hold a secret) reach it through a pipe on
  # descriptor 3, which the command's own shell reads and closes before it runs anything,
  # so they are on no command line, in no file and in no environment but the command's.
  (cd "$REPO" && exec setsid "${SANDBOX_PREFIX[@]}" bash -c 'while IFS= read -r -d "" kv <&3; do [ -n "$kv" ] && export "$kv"; done; exec 3<&-; exec bash -c "$1"' sh "$cmd" \
    </dev/null 3< <(printf '%s\0' "${STEP_PROXY_ENV[@]}")) >"$fifo" 2>&1 &
  pid=$!

  while kill -0 "$pid" 2>/dev/null; do
    if [ "$SECONDS" -ge "$DEADLINE" ]; then
      timed_out=1
      break
    fi
    sleep "$nap"
    spent=$((spent + 1))
    # Quick to notice a short step, cheap to wait out a long one.
    if [ "$spent" -eq 40 ]; then nap=0.5; fi
  done

  reap_step "$pid"
  wait "$pid" 2>/dev/null
  rc=$?
  # The reader ends when the last writer is gone. A writer that escaped (possible only
  # when the runner is not root) must not hang the runner: it is cut loose.
  spent=0
  while kill -0 "$reader" 2>/dev/null && [ "$spent" -lt 50 ]; do
    sleep 0.1
    spent=$((spent + 1))
  done
  kill "$reader" 2>/dev/null
  wait "$reader" 2>/dev/null
  rm -f "$fifo"

  duration=$(($(now_ms) - started))
  size=$(wc -c <"$log")

  if [ "$timed_out" -eq 1 ]; then
    status=timeout
  elif [ "$size" -ge "$MAX_OUTPUT_BYTES" ]; then
    status=limit
    LAST_LIMIT=output
    printf '\n[verifier] output limit of %s bytes reached; the step was cut off\n' "$MAX_OUTPUT_BYTES" >>"$log"
  elif [ "$rc" -eq 137 ]; then
    # Killed with SIGKILL, and not by this runner's own clock: the kernel's out of
    # memory killer is what does that to a step.
    status=limit
    LAST_LIMIT=memory
    printf '\n[verifier] the step was killed (exit 137), most likely for running out of memory\n' >>"$log"
  elif [ "$expect_failure" = yes ]; then
    if [ "$rc" -eq 0 ]; then
      status=fail
      printf '\n[verifier] this command passed on the unmodified tree, so it proves nothing about the change\n' >>"$log"
    else
      status=pass
    fi
  else
    if [ "$rc" -eq 0 ]; then status=pass; else status=fail; fi
  fi

  LAST_STATUS=$status
  LAST_DURATION=$duration
  LAST_EXCERPT=$(tail_of "$log")
  LAST_RC=$rc
  if [ "$do_record" = yes ]; then record "$name" "$status" "$duration" "$LAST_EXCERPT" "$rc" "$index" "$LAST_LIMIT"; fi
}


# ---------------------------------------------------------------------------
# The result. Always written and always uploaded once the bootstrap has been read,
# even when the run could not start: a silent runner holds the organization's
# running slot until the reaper. The result URL is in a file only root can open, and
# curl reads it from there: it is on no command line. A session reports the same way:
# once for the ready report, once per job.
# ---------------------------------------------------------------------------
build_result() {
  # build_result FILE: the result schema, from the steps recorded so far.
  local out=$1 count per
  count=$(wc -l <"$STEPS_FILE")
  if [ "$count" -lt 1 ]; then count=1; fi
  per=$((RESULT_BUDGET / count))
  if [ "$per" -gt 3500 ]; then per=3500; fi
  if [ "$per" -lt "$EXCERPT_FLOOR" ]; then per=$EXCERPT_FLOOR; fi

  jq -n \
    --argjson baseline "$BASELINE_GREEN" \
    --arg runner_error "$RUNNER_ERROR" \
    --arg runtime "$RUNTIME" \
    --argjson version "$RUNNER_VERSION" \
    --argjson per "$per" \
    --slurpfile steps "$STEPS_FILE" \
    '{schema: 1, baseline_green: $baseline,
      steps: ($steps | map(.excerpt |= (.[-$per:]))),
      runner: {runtime: $runtime, version: $version}}
     + (if $runner_error == "" then {} else {runner_error: $runner_error} end)' >"$out"
}

# put_json CONFIG FILE: PUT FILE to the URL in the curl config CONFIG, again after two seconds
# on a network error or a 5xx (three attempts). Answers 0 for a 2xx; the last HTTP status
# (000 for none) is left in PUT_CODE and the answer's body in $OUT/response.txt.
PUT_CODE=000
put_json() {
  local config=$1 file=$2 attempt
  PUT_CODE=000
  for attempt in 1 2 3; do
    PUT_CODE=$(curl -sS --config "$config" -o "$OUT/response.txt" -w '%{http_code}' --max-time 60 \
      -X PUT -H 'content-type: application/json' --data-binary @"$file") || PUT_CODE=000
    case "$PUT_CODE" in
      2??) return 0 ;;
      000 | 5??)
        log "upload attempt $attempt failed ($PUT_CODE)"
        sleep 2
        ;;
      *) return 1 ;;
    esac
  done
  return 1
}

finish() {
  build_result "$OUT/result.json"
  if put_json "$RESULT_CONFIG" "$OUT/result.json"; then
    log "result accepted"
    exit 0
  fi
  case "$PUT_CODE" in 000 | 5??) ;; *) log "result refused ($PUT_CODE)" ;; esac
  exit 1
}

fail_run() {
  RUNNER_ERROR=$1
  log "$1"
  finish
}

# ---------------------------------------------------------------------------
# The URLs. Each goes to curl through a file only root can open, so it is on no command
# line; on the hosted verifier the file also names the proxy and the runner's credential.
# ---------------------------------------------------------------------------
curl_config() {
  # curl_config FILE URL: a config file holding one URL, readable by root only.
  local file=$1 url=$2
  case "$url" in
    *'"'* | *$'\n'* | *$'\r'* | *'\'*)
      log "refusing a URL that cannot be written to a curl config"
      return 1
      ;;
  esac
  (
    umask 077
    {
      printf 'url = "%s"\n' "$url"
      if [ -n "$RUNNER_PROXY_URL" ]; then printf 'proxy = "%s"\n' "$RUNNER_PROXY_URL"; fi
    } >"$file"
  )
}

# with_query URL PARAM: the URL with one more query parameter, in REPLY.
with_query() {
  case "$1" in
    *'?'*) REPLY="$1&$2" ;;
    *) REPLY="$1?$2" ;;
  esac
}

# json_string FILE FILTER: a string out of a JSON file, in REPLY; empty when it is not one.
json_string() {
  REPLY=$(jq -r "($2) | if type == \"string\" then . else \"\" end" "$1" 2>/dev/null) || REPLY=""
}

# The hosted verifier's credentials, from the bootstrap: the user (`run:<id>` or
# `session:<id>`), the runner's own and the one for setup commands. Without all three the
# bootstrap is treated as one from a platform that has no proxy credentials.
read_proxy_credentials() {
  [ -n "$PROXY_ENDPOINT" ] || return 0
  json_string "$1" '.proxy.user'
  PROXY_USER=$REPLY
  json_string "$1" '.proxy.runner'
  RUNNER_CRED=$REPLY
  json_string "$1" '.proxy.setup'
  SETUP_CRED=$REPLY
  if [ -n "$PROXY_USER" ] && [ -n "$RUNNER_CRED" ] && [ -n "$SETUP_CRED" ]; then
    proxy_url "$PROXY_USER" "$RUNNER_CRED"
    RUNNER_PROXY_URL=$REPLY
  else
    PROXY_USER="" RUNNER_CRED="" SETUP_CRED="" RUNNER_PROXY_URL=""
  fi
  use_step_env setup
}

# The `checks` credential, when the platform's answer to setup done carries one.
read_checks_credential() {
  json_string "$1" '.proxy.checks'
  CHECKS_CRED=$REPLY
}

# A run or a session on the hosted verifier tells the platform when setup is over, before any
# other command runs; the platform revokes the `setup` credential and answers with the `checks`
# credential only when the repository's setting allows commands after setup to reach the
# network. Answers 0 when there is nothing to tell or the platform accepted it, 1 when it did
# not: the `setup` credential may then still be live (a setup command could have saved it), so
# a caller must not run another command.
SETUP_DONE_URL=""
setup_done() {
  [ -n "$SETUP_DONE_URL" ] || return 0
  if ! curl_config "$OUT/setup-done.curl" "$SETUP_DONE_URL"; then return 1; fi
  printf '{}' >"$OUT/setup-done.body"
  if put_json "$OUT/setup-done.curl" "$OUT/setup-done.body"; then
    read_checks_credential "$OUT/response.txt"
    return 0
  fi
  log "setup done was not accepted ($PUT_CODE)"
  return 1
}

BOOTSTRAP_CONFIG="$OUT/bootstrap.curl"
RESULT_CONFIG="$OUT/result.curl"
READY_CONFIG="$OUT/ready.curl"
NEXT_CONFIG="$OUT/next.curl"
JOB_RESULT_CONFIG="$OUT/job-result.curl"
BOOTSTRAP_FILE="$OUT/bootstrap.json"
JOB_FILE="$OUT/job.json"

# ---------------------------------------------------------------------------
# Clone at the base commit into a root-owned staging directory nobody else can
# reach. The token is used for this one command, through git's own environment
# configuration, so it is written to no file and no git config and is on no command
# line (nor is the proxy credential). Once the tree is complete it is handed to the
# unprivileged user and moved into place with one rename: root never writes inside a
# directory that user can write. The fetch is held to the clock.
# ---------------------------------------------------------------------------
do_clone() {
  local clone_started clone_rc clone_text remaining
  clone_started=$(now_ms)
  remaining=$((DEADLINE - SECONDS))
  if [ "$remaining" -lt 1 ]; then remaining=1; fi
  mkdir -p "$STAGE"
  (
    cd "$STAGE" || exit 1
    git init -q . || exit 1
    git remote add origin "$REPO_URL" || exit 1
    export GIT_TERMINAL_PROMPT=0
    n=0
    if [ -n "$REPO_TOKEN" ]; then
      auth=$(printf 'x-access-token:%s' "$REPO_TOKEN" | base64 -w0)
      export "GIT_CONFIG_KEY_$n=http.extraheader" "GIT_CONFIG_VALUE_$n=Authorization: Basic $auth"
      n=$((n + 1))
    fi
    if [ -n "$RUNNER_PROXY_URL" ]; then
      export "GIT_CONFIG_KEY_$n=http.proxy" "GIT_CONFIG_VALUE_$n=$RUNNER_PROXY_URL"
      n=$((n + 1))
      export "GIT_CONFIG_KEY_$n=http.proxyAuthMethod" "GIT_CONFIG_VALUE_$n=basic"
      n=$((n + 1))
    fi
    export GIT_CONFIG_COUNT=$n
    timeout -k 5 "$remaining" git fetch -q --depth 1 --no-tags origin "${BASE_SHA:-HEAD}" || exit 1
    unset GIT_CONFIG_COUNT auth
    git checkout -q FETCH_HEAD || exit 1
  ) >"$OUT/clone.log" 2>&1
  clone_rc=$?
  if [ "$clone_rc" -ne 0 ]; then
    if [ "$clone_rc" -eq 124 ] || [ "$clone_rc" -eq 137 ]; then
      clone_text="the clone was cut off by the clock"
    else
      clone_text=$(tail -c 1500 "$OUT/clone.log" | tr -d '\000')
    fi
    if [ -n "$REPO_TOKEN" ]; then clone_text=${clone_text//"$REPO_TOKEN"/[token]}; fi
    if [ -n "$RUNNER_CRED" ]; then clone_text=${clone_text//"$RUNNER_CRED"/[credential]}; fi
    record clone error $(($(now_ms) - clone_started)) "$clone_text"
    unset REPO_TOKEN
    return 1
  fi
  unset REPO_TOKEN

  if [ -n "$SANDBOX_UID" ]; then
    chown -R "$SANDBOX_UID:$SANDBOX_GID" "$STAGE"
    chmod 0711 "$WORK"
  fi
  mv "$STAGE" "$REPO"
  record clone pass $(($(now_ms) - clone_started)) ""
}

# ---------------------------------------------------------------------------
# The patch (a run) or a job's files (a session). Paths are checked again (the
# platform already contained them) because this is the process that decides where it
# is written, and every write is then made by the unprivileged user.
# ---------------------------------------------------------------------------
PATCH_FILE="$OUT/patch.json"
FILE_COUNT=0
WRITTEN="$OUT/written.list"
: >"$WRITTEN"
# Each file's content, written by root into a directory only root can write, so the
# unprivileged user that writes the file into the tree reads it from there.
INCOMING="$WORK/incoming"
mkdir -p "$INCOMING"
chmod 0755 "$INCOMING"

safe_path() {
  local path=$1 expected actual
  [ -n "$path" ] || return 1
  case "$path" in
    /* | ../* | */../* | */.. | .. | ./* | */./* | */. | *//* | */) return 1 ;;
  esac
  case "$path" in .git | .git/* | */.git | */.git/*) return 1 ;; esac
  case "$path" in *$'\n'* | *$'\r'*) return 1 ;; esac
  expected="$(realpath -m "$REPO")/$path"
  actual=$(realpath -m "$REPO/$path")
  [ "$expected" = "$actual" ] || return 1
  [ ! -L "$REPO/$path" ] || return 1
  return 0
}

# The run's clock ran out while the tree was being prepared: that is a limit, not a
# refused patch, and the steps that never ran are not judged.
out_of_time() {
  record patch timeout 0 "the wall clock was used up while the runner was preparing the tree"
  finish
}

# apply_files all|tests. Each file is written by the unprivileged user from a file only
# root can write: a leftover process of the repository cannot turn this into a
# privileged write, and a FIFO planted where the file goes cannot hold the runner past
# its clock. Answers 0, 124 when the clock ran out, or 1 with APPLY_ERROR.
APPLY_ERROR=""
apply_files() {
  local which=$1 i path is_test rc
  APPLY_ERROR=""
  for ((i = 0; i < FILE_COUNT; i++)); do
    path=$(jq -r ".files[$i].path" "$PATCH_FILE")
    is_test=$(jq -r ".files[$i].test == true" "$PATCH_FILE")
    if [ "$which" = tests ] && [ "$is_test" != true ]; then continue; fi
    if ! safe_path "$path"; then
      APPLY_ERROR="the patch names a path the runner refuses"
      return 1
    fi
    sandbox_op "${GIT[@]}" ls-files --error-unmatch -- "$path"
    rc=$?
    if [ "$rc" -eq 124 ]; then return 124; fi
    if [ "$rc" -eq 0 ]; then
      printf 'tracked\t%s\n' "$path" >>"$WRITTEN"
    else
      printf 'untracked\t%s\n' "$path" >>"$WRITTEN"
    fi
    jq -j ".files[$i].content" "$PATCH_FILE" >"$INCOMING/$i"
    chmod 0644 "$INCOMING/$i"
    sandbox_op sh -c 'mkdir -p -- "$(dirname -- "$1")" && cat -- "$2" >"$1"' sh "$path" "$INCOMING/$i"
    rc=$?
    if [ "$rc" -eq 124 ]; then return 124; fi
    if [ "$rc" -ne 0 ]; then
      APPLY_ERROR="the patch could not be written"
      return 1
    fi
  done
  return 0
}

# apply_or_end all|tests: apply_files for a run, which ends at the first refusal.
apply_or_end() {
  local rc
  apply_files "$1"
  rc=$?
  case "$rc" in
    0) ;;
    124) out_of_time ;;
    *) fail_run "$APPLY_ERROR" ;;
  esac
}

# Put every written file back as it was: tracked files from git, new ones removed, both
# by the unprivileged user and under the run's clock. A revert that runs out of time
# leaves the tree as it is, and the next write finds no time left and ends the run as
# a timeout of the `patch` step.
revert_written() {
  local kind path
  while IFS=$'\t' read -r kind path; do
    [ -n "$path" ] || continue
    if [ "$kind" = tracked ]; then
      sandbox_op "${GIT[@]}" checkout -q -- "$path"
    else
      sandbox_op rm -f -- "$path"
    fi
  done <"$WRITTEN"
  : >"$WRITTEN"
}

# ---------------------------------------------------------------------------
# The steps, by kind.
# ---------------------------------------------------------------------------
STEPS_JSON="[]"
commands_of() { jq -c --arg kind "$1" '.[] | select(.kind == $kind)' <<<"$STEPS_JSON"; }

# The platform's rule pack: only plain `.yml` file names, at most 20 files of at most 20,000
# bytes each, so a name can never point outside the directory and the pack can never grow
# without bound.
RULES_JSON="{}"
rules_valid() {
  jq -e 'type == "object" and length <= 20 and all(to_entries[]; (.key | test("^[a-z0-9][a-z0-9-]{0,60}[.]yml$")) and (.value | type == "string" and utf8bytelength <= 20000))' <<<"$RULES_JSON" >/dev/null 2>&1
}

# The rule pack, written by root into a directory only root can write, which the analysis
# commands read as {rules_dir}. Nothing of the repository is in it.
RULES_DIR="$WORK/rules"
RULES_DIR_QUOTED=$(printf '%q' "$RULES_DIR")
write_rules() {
  local rule_name
  if [ "$(jq 'length' <<<"$RULES_JSON")" -gt 0 ]; then
    install -d -m 0755 "$RULES_DIR"
    while IFS= read -r rule_name; do
      jq -j --arg k "$rule_name" '.[$k]' <<<"$RULES_JSON" >"$RULES_DIR/$rule_name"
      chmod 0644 "$RULES_DIR/$rule_name"
    done < <(jq -r 'keys[]' <<<"$RULES_JSON")
  fi
}

# Static analysis on the files the proposal touches. A finding is evidence for the person
# reading the pull request and never a verdict, and a tool this image does not carry is
# `skipped` (the shell's "command not found", 127), not a failure: the platform judges
# neither, it shows them.
run_analysis_step() {
  local name=$1 index=$2 cmd=$3
  cmd=${cmd//\{rules_dir\}/$RULES_DIR_QUOTED}
  run_step "$name" "$cmd" "$index" no no
  if [ "$LAST_STATUS" = fail ] && [ "$LAST_RC" = 127 ]; then
    record "$name" skipped "$LAST_DURATION" "the analysis tool is not installed in this image, so nothing was analysed" "$LAST_RC" "$index"
  else
    record "$name" "$LAST_STATUS" "$LAST_DURATION" "$LAST_EXCERPT" "$LAST_RC" "$index" "$LAST_LIMIT"
  fi
}

# ---------------------------------------------------------------------------
# Session mode (runner version 3). One clone, one setup, a baseline, a ready report, then
# jobs until the platform says the session is over. Each job starts from the base commit
# exactly (the previous job's edits, commits and strays are gone), is held to its own
# clock, and is reported on its own URL; nothing a job started outlives it.
# ---------------------------------------------------------------------------

# fail_ready MESSAGE: the session cannot start. Say so on the ready URL, then end.
fail_ready() {
  RUNNER_ERROR=$1
  BASELINE_GREEN=false
  log "$1"
  build_result "$OUT/ready.json"
  put_json "$READY_CONFIG" "$OUT/ready.json" || log "the ready report was not accepted ($PUT_CODE)"
  exit 0
}

# What git needs to be told before a job, that the previous job could have changed: its
# own configuration (filters, hooks, fsmonitor), the exclude and attributes files that
# decide what `clean` removes, and the index, whose assume-unchanged and skip-worktree
# flags can hide an edit from a restore. The configuration and the exclude file come back
# from copies taken right after the clone, held where only root can write.
PRISTINE="$WORK/pristine"
snapshot_git_state() {
  install -d -m 0755 "$PRISTINE"
  cp -- "$REPO/.git/config" "$PRISTINE/config"
  if [ -e "$REPO/.git/info/exclude" ]; then cp -- "$REPO/.git/info/exclude" "$PRISTINE/exclude"; fi
  chmod -R a+rX "$PRISTINE"
}

# restore_tree: the tree as the base commit has it, as the unprivileged user and under
# the job's clock. Answers 0, or the failing operation's status (124 for the clock).
restore_tree() {
  local rc
  sandbox_op sh -c 'rm -rf -- .git/config .git/index .git/info/exclude .git/info/attributes .git/info/sparse-checkout && mkdir -p .git/info && cp -- "$1" .git/config && { [ ! -e "$2" ] || cp -- "$2" .git/info/exclude; }' sh "$PRISTINE/config" "$PRISTINE/exclude"
  rc=$?
  if [ "$rc" -ne 0 ]; then return "$rc"; fi
  sandbox_op "${GIT[@]}" update-ref --no-deref HEAD "$BASE_SHA"
  rc=$?
  if [ "$rc" -ne 0 ]; then return "$rc"; fi
  sandbox_op "${GIT[@]}" read-tree --reset -u "$BASE_SHA"
  rc=$?
  if [ "$rc" -ne 0 ]; then return "$rc"; fi
  # Untracked files and directories (nested repositories too) that are not ignored; never
  # -x, so the ignored caches setup built survive.
  sandbox_op "${GIT[@]}" clean -ffdq
}

# snapshot_name INDEX: the name the session's snapshot gives a command, in REPLY. A job's
# own spelling of it is never used.
snapshot_name() {
  REPLY=$(jq -r --argjson i "$1" '.[] | select(.index == $i) | .name' "$OUT/snapshot.json")
}

# job_run SEQ: restores the tree, writes the job's files and runs its steps in kind order.
# Records into STEPS_FILE; a refusal is left in RUNNER_ERROR, and END_SESSION is set when the
# tree can no longer be trusted or the clock ended the job.
job_run() {
  local rc kind entry index name cmd
  if ! jq -e '(.files | type == "array") and all(.files[]; (.path | type == "string") and (.content | type == "string"))' "$JOB_FILE" >/dev/null 2>&1; then
    RUNNER_ERROR="the job's files are not a list of paths and contents"
    return
  fi
  if [ "$(jq '.files | length' "$JOB_FILE")" -gt 20 ]; then
    RUNNER_ERROR="the job carries more than 20 files"
    return
  fi
  if ! jq -e --slurpfile snap "$OUT/snapshot.json" '
      (.steps | type == "array" and length <= 12) and
      all(.steps[]; (type == "object") and (.kind | IN("check", "analysis", "test", "new_test")) and (.cmd | type == "string")
        and (. as $s | any($snap[0][]; .index == $s.index and .kind == $s.kind)))' "$JOB_FILE" >/dev/null 2>&1; then
    RUNNER_ERROR="the job names a command that is not in the session's snapshot"
    return
  fi

  restore_tree
  rc=$?
  if [ "$rc" -eq 124 ]; then
    record patch timeout 0 "the wall clock was used up while the runner was preparing the tree"
    END_SESSION=true
    return
  elif [ "$rc" -ne 0 ]; then
    RUNNER_ERROR="the tree could not be restored to the base commit"
    END_SESSION=true
    return
  fi

  PATCH_FILE=$JOB_FILE
  FILE_COUNT=$(jq '.files | length' "$JOB_FILE")
  : >"$WRITTEN"
  apply_files all
  rc=$?
  if [ "$rc" -eq 124 ]; then
    record patch timeout 0 "the wall clock was used up while the runner was preparing the tree"
    END_SESSION=true
    return
  elif [ "$rc" -ne 0 ]; then
    RUNNER_ERROR=${APPLY_ERROR/the patch/the job}
    return
  fi

  for kind in check analysis test new_test; do
    while IFS= read -r entry; do
      [ -n "$entry" ] || continue
      index=$(jq -r '.index' <<<"$entry")
      cmd=$(jq -r '.cmd' <<<"$entry")
      snapshot_name "$index"
      name=$REPLY
      if [ "$kind" = analysis ]; then
        run_analysis_step "$name" "$index" "$cmd"
      else
        run_step "$name" "$cmd" "$index"
      fi
      if [ "$LAST_STATUS" = timeout ]; then END_SESSION=true; fi
    done < <(jq -c --arg k "$kind" '.steps[] | select(.kind == $k)' "$JOB_FILE")
  done
}

# handle_job: runs the job in JOB_FILE and reports it. Answers 1 for a job that cannot be
# reported at all (no seq, no result URL, a seq already answered); exits the runner when
# the platform says the session is over or refuses the result.
handle_job() {
  local seq result_url now budget
  seq=$(jq -r '.seq | if type == "number" and . >= 1 and . == floor then tostring else "" end' "$JOB_FILE" 2>/dev/null)
  json_string "$JOB_FILE" '.result_url'
  result_url=$REPLY
  if [ -z "$seq" ] || [ -z "$result_url" ]; then
    log "the job names no seq or no result URL, so it cannot be reported"
    return 1
  fi
  if [ "$seq" -le "$LAST_SEQ" ]; then
    log "the job's seq ($seq) is not after the last one answered ($LAST_SEQ); ignored"
    return 1
  fi
  if ! curl_config "$JOB_RESULT_CONFIG" "$result_url"; then return 1; fi

  : >"$STEPS_FILE"
  RUNNER_ERROR=""
  BASELINE_GREEN=true
  END_SESSION=false
  printf -v now '%(%s)T' -1
  budget=$JOB_SECONDS
  if [ $((END_EPOCH - now)) -lt "$budget" ]; then budget=$((END_EPOCH - now)); fi
  DEADLINE=$((SECONDS + budget))

  job_run

  build_result "$OUT/job-result.json"
  put_json "$JOB_RESULT_CONFIG" "$OUT/job-result.json"
  case "$PUT_CODE" in
    2??) ;;
    410)
      log "the session is over (the result was answered 410)"
      exit 0
      ;;
    *)
      log "the job's result was not accepted ($PUT_CODE)"
      exit 1
      ;;
  esac
  rm -f "$JOB_FILE" "$JOB_RESULT_CONFIG" "$OUT"/step-*.log "$INCOMING"/*
  LAST_SEQ=$seq
  IDLE_DEADLINE=$((SECONDS + IDLE_SECONDS))
  if [ "$END_SESSION" = true ]; then
    log "the session ends after this job"
    exit 0
  fi
  return 0
}

# session_loop: asks for the next job until the platform ends the session (410), the
# session is idle for IDLE_SECONDS, or it reaches its end time. A 404, a 429, a 5xx or no
# answer at all is waited out (2, 4, 8, then 20 seconds), never past the end time or the idle
# limit; any other answer ends the runner.
LAST_SEQ=0
IDLE_DEADLINE=0
session_loop() {
  local fails=0 delays=(2 4 8 20) now code started delay
  IDLE_DEADLINE=$((SECONDS + IDLE_SECONDS))
  while :; do
    printf -v now '%(%s)T' -1
    if [ "$now" -ge "$END_EPOCH" ]; then
      log "the session has reached its end time"
      exit 0
    fi
    if [ "$SECONDS" -ge "$IDLE_DEADLINE" ]; then
      log "no job for $IDLE_SECONDS seconds; the session is idle"
      exit 0
    fi
    with_query "$NEXT_URL" "after=$LAST_SEQ"
    if ! curl_config "$NEXT_CONFIG" "$REPLY"; then exit 1; fi
    started=$SECONDS
    code=$(curl -sS --config "$NEXT_CONFIG" -o "$JOB_FILE" -w '%{http_code}' --max-time 40) || code=000
    case "$code" in
      200)
        if handle_job; then
          fails=0
          continue
        fi
        ;;
      204)
        fails=0
        # The platform holds the request for up to 20 seconds; an instant answer is not
        # a reason to ask again at once.
        if [ $((SECONDS - started)) -lt 1 ]; then sleep 1; fi
        continue
        ;;
      410)
        log "the session is over"
        exit 0
        ;;
      404 | 429 | 5?? | 000) ;;
      *)
        log "the next request was refused ($code)"
        exit 1
        ;;
    esac
    delay=${delays[$((fails < 3 ? fails : 3))]}
    fails=$((fails + 1))
    printf -v now '%(%s)T' -1
    if [ $((now + delay)) -ge "$END_EPOCH" ] || [ $((SECONDS + delay)) -ge "$IDLE_DEADLINE" ]; then
      log "the next request answered $code and there is no time left to wait for it"
      exit 0
    fi
    log "the next request answered $code; asking again in $delay seconds"
    sleep "$delay"
  done
}

SESSION_VALID='
  (.session | type == "object")
  and ((.session.ready_url | type) == "string")
  and ((.session.next_url | type) == "string")
  and ((.session.end_at | type) == "string")
  and ((.session.setup_done_url // "") | type == "string")
  and ([.session.idle_seconds, .session.job_seconds, .session.setup_seconds]
       | all(type == "number" and . >= 1 and . <= 7200 and . == floor))
  and ((.repo_url | type) == "string")
  and ((.base_sha | type) == "string") and (.base_sha | length > 0)
  and ((.steps | type) == "array") and (.steps | length <= 12)
  and all(.steps[]; (type == "object") and (.index | type == "number" and . >= 0 and . == floor)
       and ((.name | type) == "string") and (.kind | IN("setup", "check", "analysis", "test", "new_test"))
       and ((.cmd | type) == "string"))
  and ((.steps | map(.index) | unique | length) == (.steps | length))
'

session_main() {
  local ready_url end_at entry SETUP_OK
  json_string "$BOOTSTRAP_FILE" '.session.ready_url'
  ready_url=$REPLY
  if [ -z "$ready_url" ]; then
    log "the session bootstrap names no ready URL; nothing can be reported"
    exit 1
  fi
  read_proxy_credentials "$BOOTSTRAP_FILE"
  if ! curl_config "$READY_CONFIG" "$ready_url"; then exit 1; fi
  log "session mode"

  if ! jq -e "$SESSION_VALID" "$BOOTSTRAP_FILE" >/dev/null 2>&1; then
    fail_ready "the session bootstrap is not what the contract says"
  fi
  json_string "$BOOTSTRAP_FILE" '.session.next_url'
  NEXT_URL=$REPLY
  json_string "$BOOTSTRAP_FILE" '.session.end_at'
  end_at=$REPLY
  IDLE_SECONDS=$(jq -r '.session.idle_seconds' "$BOOTSTRAP_FILE")
  JOB_SECONDS=$(jq -r '.session.job_seconds' "$BOOTSTRAP_FILE")
  local setup_seconds
  setup_seconds=$(jq -r '.session.setup_seconds' "$BOOTSTRAP_FILE")
  REPO_URL=$(jq -r '.repo_url' "$BOOTSTRAP_FILE")
  REPO_TOKEN=$(jq -r '.repo_token // ""' "$BOOTSTRAP_FILE")
  BASE_SHA=$(jq -r '.base_sha // ""' "$BOOTSTRAP_FILE")
  MAX_OUTPUT_BYTES=$(jq -r '.max_output_bytes // 1000000' "$BOOTSTRAP_FILE")
  NETWORK=$(jq -r 'if .network == true then "true" else "false" end' "$BOOTSTRAP_FILE")
  json_string "$BOOTSTRAP_FILE" '.session.setup_done_url'
  SETUP_DONE_URL=$REPLY
  if [ -n "$PROXY_USER" ] && [ -z "$SETUP_DONE_URL" ]; then
    # Without the message the platform cannot revoke the `setup` credential before the baseline.
    fail_ready "the session bootstrap carries proxy credentials and no setup_done_url"
  fi
  STEPS_JSON=$(jq -c .steps "$BOOTSTRAP_FILE")
  RULES_JSON=$(jq -c 'if (.analysis | type) == "object" then (.analysis.files // {}) elif .analysis == null then {} else "invalid" end' "$BOOTSTRAP_FILE")
  printf '%s' "$STEPS_JSON" >"$OUT/snapshot.json"
  # The token has been read; the response that carried it is not kept.
  rm -f "$BOOTSTRAP_FILE"

  case "$MAX_OUTPUT_BYTES" in '' | *[!0-9]* | 0) MAX_OUTPUT_BYTES=1000000 ;; esac
  if ! [[ "$end_at" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]+)?Z$ ]] ||
    ! END_EPOCH=$(date -u -d "$end_at" +%s 2>/dev/null); then
    fail_ready "the session bootstrap is not what the contract says"
  fi
  if ! rules_valid; then fail_ready "the bootstrap's rule pack is not what the contract says"; fi
  if [ -n "$RUNNER_ERROR" ]; then fail_ready "$RUNNER_ERROR"; fi
  if [ -z "$REPO_URL" ]; then fail_ready "the bootstrap named no repository"; fi

  # Boot: clone, setup, the baseline on the unmodified tree. One clock for all of it.
  DEADLINE=$((SECONDS + setup_seconds))
  if ! do_clone; then fail_ready "the repository could not be cloned"; fi
  snapshot_git_state
  write_rules

  use_step_env setup
  SETUP_OK=true
  while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    run_step "$(jq -r .name <<<"$entry")" "$(jq -r .cmd <<<"$entry")" "$(jq -r '.index // empty' <<<"$entry")"
    if [ "$LAST_STATUS" != pass ]; then SETUP_OK=false; fi
  done < <(commands_of setup)
  if [ "$SETUP_OK" != true ]; then
    # Nothing can be judged on a tree that does not build, and no job can be run on it.
    BASELINE_GREEN=false
    build_result "$OUT/ready.json"
    put_json "$READY_CONFIG" "$OUT/ready.json" || log "the ready report was not accepted ($PUT_CODE)"
    exit 0
  fi

  # Setup is over. The platform revokes the `setup` credential here and answers with the
  # `checks` credential only when the repository's setting allows commands after setup the
  # network; the baseline below is such a command, so it runs under whatever that answer
  # allows and never under the `setup` credential a setup command may have saved. A platform
  # that did not take the message leaves that credential live: no further command runs.
  if ! setup_done; then
    fail_ready "the platform did not accept the end of setup, so no further command was run"
  fi
  use_step_env checks

  # Every test command, each reported on its own: a red one does not end the session, the
  # platform shows it as "red on the unmodified tree" next to the same command's result.
  BASELINE_GREEN=true
  while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    run_step baseline "$(jq -r .cmd <<<"$entry")" "$(jq -r '.index // empty' <<<"$entry")"
    if [ "$LAST_STATUS" != pass ]; then BASELINE_GREEN=false; fi
  done < <(commands_of test)

  build_result "$OUT/ready.json"
  if ! put_json "$READY_CONFIG" "$OUT/ready.json"; then
    log "the ready report was not accepted ($PUT_CODE)"
    case "$PUT_CODE" in 409 | 410) exit 0 ;; *) exit 1 ;; esac
  fi
  : >"$STEPS_FILE"
  session_loop
}

# ---------------------------------------------------------------------------
# The bootstrap: one GET of the one URL the task was started with. Everything the
# task needs comes back over HTTPS and nothing of it was in the RunTask request. The
# URL goes to curl through a file, like the result URL later. ?runner=3 tells the
# platform which runner this is, so a session is only ever given to one that knows
# what a session is.
# ---------------------------------------------------------------------------
if [ -n "$PROXY_ENDPOINT" ]; then
  # Until the bootstrap is claimed the only credential is the URL's own signature, good
  # for the platform host and nothing else.
  BOOTSTRAP_RE='/verifier/(sessions/)?([^/?#]+)/([^/?#]+)/bootstrap([?#].*)?$'
  if [[ "$BOOTSTRAP_URL" =~ $BOOTSTRAP_RE ]]; then
    pre_kind=run
    if [ -n "${BASH_REMATCH[1]}" ]; then pre_kind=session; fi
    proxy_url "$pre_kind:${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}"
    RUNNER_PROXY_URL=$REPLY
  else
    log "the bootstrap URL names no run or session, so the proxy cannot be authenticated to"
    exit 1
  fi
fi
with_query "$BOOTSTRAP_URL" "runner=$RUNNER_VERSION"
if ! curl_config "$BOOTSTRAP_CONFIG" "$REPLY"; then exit 1; fi
unset BOOTSTRAP_URL

bootstrap_code=000
for attempt in 1 2 3; do
  bootstrap_code=$(curl -sS --config "$BOOTSTRAP_CONFIG" -o "$BOOTSTRAP_FILE" -w '%{http_code}' --max-time 60) || bootstrap_code=000
  case "$bootstrap_code" in
    2??) break ;;
    000 | 5??) sleep 2 ;;
    *) break ;;
  esac
done
chmod 0600 "$BOOTSTRAP_FILE" 2>/dev/null
# The signature is worth nothing once the bootstrap is claimed, and is not kept.
rm -f "$BOOTSTRAP_CONFIG"
RUNNER_PROXY_URL=""

# Without the bootstrap there is no result URL, so nothing can be reported: the
# platform ends the run at its deadline.
case "$bootstrap_code" in
  2??) ;;
  *)
    log "the bootstrap was refused or unreachable ($bootstrap_code); nothing can be reported"
    exit 1
    ;;
esac

if jq -e '(.session | type) == "object"' "$BOOTSTRAP_FILE" >/dev/null 2>&1; then
  session_main
  exit 0
fi

if ! jq -e '(.repo_url | type == "string") and (.result_url | type == "string") and (.steps | type == "array") and (.files | type == "array")' "$BOOTSTRAP_FILE" >/dev/null 2>&1; then
  log "the bootstrap is not what the contract says; nothing can be reported"
  exit 1
fi

read_proxy_credentials "$BOOTSTRAP_FILE"
if ! curl_config "$RESULT_CONFIG" "$(jq -r .result_url "$BOOTSTRAP_FILE")"; then exit 1; fi
REPO_URL=$(jq -r .repo_url "$BOOTSTRAP_FILE")
REPO_TOKEN=$(jq -r '.repo_token // ""' "$BOOTSTRAP_FILE")
BASE_SHA=$(jq -r '.base_sha // ""' "$BOOTSTRAP_FILE")
WALL_CLOCK_SECONDS=$(jq -r '.wall_clock_seconds // 900' "$BOOTSTRAP_FILE")
MAX_OUTPUT_BYTES=$(jq -r '.max_output_bytes // 1000000' "$BOOTSTRAP_FILE")
STEPS_JSON=$(jq -c .steps "$BOOTSTRAP_FILE")
# The platform's static analysis rules, file name to text, when the run has an analysis
# command. Anything that is not an object stays a string, which the check below refuses.
RULES_JSON=$(jq -c 'if (.analysis | type) == "object" then (.analysis.files // {}) elif .analysis == null then {} else "invalid" end' "$BOOTSTRAP_FILE")
NETWORK=$(jq -r 'if has("network") then (if .network == true then "true" else "false" end) else "legacy" end' "$BOOTSTRAP_FILE")
json_string "$BOOTSTRAP_FILE" '.setup_done_url'
SETUP_DONE_URL=$REPLY
jq -c '{files: .files}' "$BOOTSTRAP_FILE" >"$PATCH_FILE"
# The token has been read; the response that carried it is not kept.
rm -f "$BOOTSTRAP_FILE"

case "$WALL_CLOCK_SECONDS" in '' | *[!0-9]*) WALL_CLOCK_SECONDS=900 ;; esac
case "$MAX_OUTPUT_BYTES" in '' | *[!0-9]* | 0) MAX_OUTPUT_BYTES=1000000 ;; esac
# The upload needs its own thirty seconds at the end.
DEADLINE=$((WALL_CLOCK_SECONDS - 30))

# ---------------------------------------------------------------------------
# Checks on the input.
# ---------------------------------------------------------------------------
if [ -n "$RUNNER_ERROR" ]; then fail_run "$RUNNER_ERROR"; fi
if [ -z "$REPO_URL" ]; then fail_run "the bootstrap named no repository"; fi
if ! rules_valid; then fail_run "the bootstrap's rule pack is not what the contract says"; fi

if ! do_clone; then fail_run "the repository could not be cloned"; fi

# ---------------------------------------------------------------------------
# The patch: already in hand, from the bootstrap.
# ---------------------------------------------------------------------------
if ! jq -e '(.files | type == "array") and (.files | length > 0) and (.files | length <= 20)' "$PATCH_FILE" >/dev/null 2>&1; then
  fail_run "the patch is not a list of 1 to 20 files"
fi
FILE_COUNT=$(jq '.files | length' "$PATCH_FILE")

if ! jq -e 'type == "array"' <<<"$STEPS_JSON" >/dev/null 2>&1; then
  fail_run "the bootstrap's steps are not a list"
fi

write_rules

# setup: once, before anything is judged.
use_step_env setup
SETUP_OK=true
while IFS= read -r entry; do
  [ -n "$entry" ] || continue
  run_step "$(jq -r .name <<<"$entry")" "$(jq -r .cmd <<<"$entry")" "$(jq -r '.index // empty' <<<"$entry")"
  if [ "$LAST_STATUS" != pass ]; then SETUP_OK=false; fi
done < <(commands_of setup)

if [ "$SETUP_OK" != true ]; then
  # Nothing can be judged on a tree that does not build.
  BASELINE_GREEN=false
  finish
fi

# Setup is over: the platform revokes the setup credential, and hands over the one for the
# commands after it only when the repository's setting allows them the network. A platform
# that did not take the message leaves the setup credential live, so no command runs.
if ! setup_done; then fail_run "the platform did not accept the end of setup, so no further command was run"; fi
use_step_env checks

# baseline: the repository's own tests on the unmodified tree, reported as ONE
# step however many commands there are (the platform accepts 20 steps). The first
# red command decides and ends the run: the change cannot be judged.
BASELINE_GREEN=true
BASELINE_STATUS=pass
BASELINE_MS=0
BASELINE_TEXT=""
BASELINE_SEEN=false
BASELINE_INDEX=""
BASELINE_LIMIT=""
while IFS= read -r entry; do
  [ -n "$entry" ] || continue
  BASELINE_SEEN=true
  BASELINE_INDEX=$(jq -r '.index // empty' <<<"$entry")
  run_step baseline "$(jq -r .cmd <<<"$entry")" "$BASELINE_INDEX" no no
  BASELINE_MS=$((BASELINE_MS + LAST_DURATION))
  BASELINE_TEXT=$LAST_EXCERPT
  if [ "$LAST_STATUS" != pass ]; then
    BASELINE_GREEN=false
    BASELINE_STATUS=$LAST_STATUS
    BASELINE_LIMIT=$LAST_LIMIT
    break
  fi
done < <(commands_of test)
if [ "$BASELINE_SEEN" = true ]; then
  record baseline "$BASELINE_STATUS" "$BASELINE_MS" "$BASELINE_TEXT" "${LAST_RC:-0}" "$BASELINE_INDEX" "$BASELINE_LIMIT"
fi

if [ "$BASELINE_GREEN" != true ]; then
  finish
fi

# the new test, on the tree as it was.
NEW_TEST_ENTRY=$(commands_of new_test | head -n1)
NEW_TEST_CMD=""
NEW_TEST_INDEX=""
if [ -n "$NEW_TEST_ENTRY" ]; then
  NEW_TEST_CMD=$(jq -r '.cmd // empty' <<<"$NEW_TEST_ENTRY")
  NEW_TEST_INDEX=$(jq -r '.index // empty' <<<"$NEW_TEST_ENTRY")
fi
HAS_TEST_FILE=$(jq '[.files[] | select(.test == true)] | length' "$PATCH_FILE")
if [ -n "$NEW_TEST_CMD" ] && [ "$HAS_TEST_FILE" -gt 0 ]; then
  apply_or_end tests
  run_step new_test_fails_before "$NEW_TEST_CMD" "$NEW_TEST_INDEX" yes
  revert_written
else
  record new_test_fails_before skipped 0 "the proposal adds no test, or the profile has no command that runs only the new test"
fi

# the patch.
apply_or_end all

while IFS= read -r entry; do
  [ -n "$entry" ] || continue
  run_step "$(jq -r .name <<<"$entry")" "$(jq -r .cmd <<<"$entry")" "$(jq -r '.index // empty' <<<"$entry")"
done < <(commands_of check)

while IFS= read -r entry; do
  [ -n "$entry" ] || continue
  run_analysis_step "$(jq -r .name <<<"$entry")" "$(jq -r '.index // empty' <<<"$entry")" "$(jq -r .cmd <<<"$entry")"
done < <(commands_of analysis)

while IFS= read -r entry; do
  [ -n "$entry" ] || continue
  run_step "$(jq -r .name <<<"$entry")" "$(jq -r .cmd <<<"$entry")" "$(jq -r '.index // empty' <<<"$entry")"
done < <(commands_of test)

if [ -n "$NEW_TEST_CMD" ] && [ "$HAS_TEST_FILE" -gt 0 ]; then
  run_step new_test_passes_after "$NEW_TEST_CMD" "$NEW_TEST_INDEX"
else
  record new_test_passes_after skipped 0 "the proposal adds no test, or the profile has no command that runs only the new test"
fi

finish
