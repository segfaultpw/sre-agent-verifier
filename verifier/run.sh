#!/usr/bin/env bash
# The verifier runner. One run, one repository, one patch, then it exits.
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
# They are forwarded to the steps (package managers need them).
PROXY_VARS=(HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy)

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

STEP_ENV=("PATH=$PATH" "HOME=$SANDBOX_HOME" "LANG=C.UTF-8" "CI=true" "TERM=dumb" "GIT_TERMINAL_PROMPT=0")
for name in "${PROXY_VARS[@]}"; do
  if [ -n "${!name:-}" ]; then STEP_ENV+=("$name=${!name}"); fi
done
if [ -r "$STEP_ENV_FILE" ]; then
  while IFS= read -r line; do
    case "$line" in
      '' | '#'*) ;;
      [A-Za-z_]*=*) STEP_ENV+=("$line") ;;
    esac
  done <"$STEP_ENV_FILE"
fi

# The prefix every command of the repository runs under: the unprivileged user, no
# way to gain privileges, an empty capability set, and an environment rebuilt from
# the whitelist above. Anything not on it does not exist for the command.
if [ -n "$SANDBOX_UID" ]; then
  SANDBOX_PREFIX=(setpriv --reuid="$SANDBOX_UID" --regid="$SANDBOX_GID" --clear-groups
    --no-new-privs --bounding-set=-all --inh-caps=-all -- env -i "${STEP_ENV[@]}")
else
  SANDBOX_PREFIX=(env -i "${STEP_ENV[@]}")
fi

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
  (cd "$REPO" && exec setsid "${SANDBOX_PREFIX[@]}" bash -c "$cmd" </dev/null) >"$fifo" 2>&1 &
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
# curl reads it from there: it is on no command line.
# ---------------------------------------------------------------------------
finish() {
  local count per result attempt code
  count=$(wc -l <"$STEPS_FILE")
  if [ "$count" -lt 1 ]; then count=1; fi
  per=$((RESULT_BUDGET / count))
  if [ "$per" -gt 3500 ]; then per=3500; fi
  if [ "$per" -lt "$EXCERPT_FLOOR" ]; then per=$EXCERPT_FLOOR; fi

  result="$OUT/result.json"
  jq -n \
    --argjson baseline "$BASELINE_GREEN" \
    --arg runner_error "$RUNNER_ERROR" \
    --arg runtime "$RUNTIME" \
    --argjson per "$per" \
    --slurpfile steps "$STEPS_FILE" \
    '{schema: 1, baseline_green: $baseline,
      steps: ($steps | map(.excerpt |= (.[-$per:]))),
      runner: {runtime: $runtime, version: 2}}
     + (if $runner_error == "" then {} else {runner_error: $runner_error} end)' >"$result"

  for attempt in 1 2 3; do
    code=$(curl -sS --config "$RESULT_CONFIG" -o "$OUT/response.txt" -w '%{http_code}' --max-time 60 \
      -X PUT -H 'content-type: application/json' --data-binary @"$result") || code=000
    case "$code" in
      2??)
        log "result accepted"
        exit 0
        ;;
      000 | 5??)
        log "upload attempt $attempt failed ($code)"
        sleep 2
        ;;
      *)
        log "result refused ($code)"
        exit 1
        ;;
    esac
  done
  exit 1
}

fail_run() {
  RUNNER_ERROR=$1
  log "$1"
  finish
}

# ---------------------------------------------------------------------------
# The bootstrap: one GET of the one URL the task was started with. Everything the
# task needs comes back over HTTPS and nothing of it was in the RunTask request. The
# URL goes to curl through a file, like the result URL later.
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
  (umask 077 && printf 'url = "%s"\n' "$url" >"$file")
}

BOOTSTRAP_CONFIG="$OUT/bootstrap.curl"
RESULT_CONFIG="$OUT/result.curl"
BOOTSTRAP_FILE="$OUT/bootstrap.json"

if ! curl_config "$BOOTSTRAP_CONFIG" "$BOOTSTRAP_URL"; then exit 1; fi
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

# Without the bootstrap there is no result URL, so nothing can be reported: the
# platform ends the run at its deadline.
case "$bootstrap_code" in
  2??) ;;
  *)
    log "the bootstrap was refused or unreachable ($bootstrap_code); nothing can be reported"
    exit 1
    ;;
esac

if ! jq -e '(.repo_url | type == "string") and (.result_url | type == "string") and (.steps | type == "array") and (.files | type == "array")' "$BOOTSTRAP_FILE" >/dev/null 2>&1; then
  log "the bootstrap is not what the contract says; nothing can be reported"
  exit 1
fi

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
PATCH_FILE="$OUT/patch.json"
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

# The rule pack is written below, by this process, to a directory of its own: only plain
# `.yml` file names, at most 20 files of at most 20,000 bytes each, so a name can never
# point outside it and the pack can never grow without bound.
if ! jq -e 'type == "object" and length <= 20 and all(to_entries[]; (.key | test("^[a-z0-9][a-z0-9-]{0,60}[.]yml$")) and (.value | type == "string" and utf8bytelength <= 20000))' <<<"$RULES_JSON" >/dev/null 2>&1; then
  fail_run "the bootstrap's rule pack is not what the contract says"
fi

# ---------------------------------------------------------------------------
# Clone at the base commit into a root-owned staging directory nobody else can
# reach. The token is used for this one command, through git's own environment
# configuration, so it is written to no file and no git config and is on no command
# line. Once the tree is complete it is handed to the unprivileged user and moved into
# place with one rename: root never writes inside a directory that user can write.
# ---------------------------------------------------------------------------
clone_started=$(now_ms)
mkdir -p "$STAGE"
(
  cd "$STAGE" || exit 1
  git init -q . || exit 1
  git remote add origin "$REPO_URL" || exit 1
  export GIT_TERMINAL_PROMPT=0
  if [ -n "$REPO_TOKEN" ]; then
    auth=$(printf 'x-access-token:%s' "$REPO_TOKEN" | base64 -w0)
    GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraheader GIT_CONFIG_VALUE_0="Authorization: Basic $auth" \
      git fetch -q --depth 1 --no-tags origin "${BASE_SHA:-HEAD}" || exit 1
  else
    git fetch -q --depth 1 --no-tags origin "${BASE_SHA:-HEAD}" || exit 1
  fi
  git checkout -q FETCH_HEAD || exit 1
) >"$OUT/clone.log" 2>&1
clone_rc=$?
if [ "$clone_rc" -ne 0 ]; then
  clone_text=$(tail -c 1500 "$OUT/clone.log" | tr -d '\000')
  if [ -n "$REPO_TOKEN" ]; then clone_text=${clone_text//"$REPO_TOKEN"/[token]}; fi
  record clone error $(($(now_ms) - clone_started)) "$clone_text"
  fail_run "the repository could not be cloned"
fi
unset REPO_TOKEN auth

if [ -n "$SANDBOX_UID" ]; then
  chown -R "$SANDBOX_UID:$SANDBOX_GID" "$STAGE"
  chmod 0711 "$WORK"
fi
mv "$STAGE" "$REPO"
record clone pass $(($(now_ms) - clone_started)) ""

# ---------------------------------------------------------------------------
# The patch: already in hand, from the bootstrap. Paths are checked again (the
# platform already contained them) because this is the process that decides where it
# is written, and every write is then made by the unprivileged user.
# ---------------------------------------------------------------------------
if ! jq -e '(.files | type == "array") and (.files | length > 0) and (.files | length <= 20)' "$PATCH_FILE" >/dev/null 2>&1; then
  fail_run "the patch is not a list of 1 to 20 files"
fi

FILE_COUNT=$(jq '.files | length' "$PATCH_FILE")
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
# its clock.
apply_files() {
  local which=$1 i path is_test rc
  for ((i = 0; i < FILE_COUNT; i++)); do
    path=$(jq -r ".files[$i].path" "$PATCH_FILE")
    is_test=$(jq -r ".files[$i].test == true" "$PATCH_FILE")
    if [ "$which" = tests ] && [ "$is_test" != true ]; then continue; fi
    if ! safe_path "$path"; then fail_run "the patch names a path the runner refuses"; fi
    sandbox_op "${GIT[@]}" ls-files --error-unmatch -- "$path"
    rc=$?
    if [ "$rc" -eq 124 ]; then out_of_time; fi
    if [ "$rc" -eq 0 ]; then
      printf 'tracked\t%s\n' "$path" >>"$WRITTEN"
    else
      printf 'untracked\t%s\n' "$path" >>"$WRITTEN"
    fi
    jq -j ".files[$i].content" "$PATCH_FILE" >"$INCOMING/$i"
    chmod 0644 "$INCOMING/$i"
    sandbox_op sh -c 'mkdir -p -- "$(dirname -- "$1")" && cat -- "$2" >"$1"' sh "$path" "$INCOMING/$i"
    rc=$?
    if [ "$rc" -eq 124 ]; then out_of_time; fi
    if [ "$rc" -ne 0 ]; then fail_run "the patch could not be written"; fi
  done
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
if ! jq -e 'type == "array"' <<<"$STEPS_JSON" >/dev/null 2>&1; then
  fail_run "the bootstrap's steps are not a list"
fi

commands_of() { jq -c --arg kind "$1" '.[] | select(.kind == $kind)' <<<"$STEPS_JSON"; }

# The platform's rule pack, written by root into a directory only root can write, which
# the analysis commands read as {rules_dir}. Nothing of the repository is in it.
RULES_DIR="$WORK/rules"
if [ "$(jq 'length' <<<"$RULES_JSON")" -gt 0 ]; then
  install -d -m 0755 "$RULES_DIR"
  while IFS= read -r rule_name; do
    jq -j --arg k "$rule_name" '.[$k]' <<<"$RULES_JSON" >"$RULES_DIR/$rule_name"
    chmod 0644 "$RULES_DIR/$rule_name"
  done < <(jq -r 'keys[]' <<<"$RULES_JSON")
fi
RULES_DIR_QUOTED=$(printf '%q' "$RULES_DIR")

# setup: once, before anything is judged.
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
  apply_files tests
  run_step new_test_fails_before "$NEW_TEST_CMD" "$NEW_TEST_INDEX" yes
  revert_written
else
  record new_test_fails_before skipped 0 "the proposal adds no test, or the profile has no command that runs only the new test"
fi

# the patch.
apply_files all

while IFS= read -r entry; do
  [ -n "$entry" ] || continue
  run_step "$(jq -r .name <<<"$entry")" "$(jq -r .cmd <<<"$entry")" "$(jq -r '.index // empty' <<<"$entry")"
done < <(commands_of check)

# Static analysis on the files the proposal touches. A finding is evidence for the person
# reading the pull request and never a verdict, and a tool this image does not carry is
# `skipped` (the shell's "command not found", 127), not a failure: the platform judges
# neither, it shows them.
while IFS= read -r entry; do
  [ -n "$entry" ] || continue
  analysis_name=$(jq -r .name <<<"$entry")
  analysis_index=$(jq -r '.index // empty' <<<"$entry")
  analysis_cmd=$(jq -r .cmd <<<"$entry")
  analysis_cmd=${analysis_cmd//\{rules_dir\}/$RULES_DIR_QUOTED}
  run_step "$analysis_name" "$analysis_cmd" "$analysis_index" no no
  if [ "$LAST_STATUS" = fail ] && [ "$LAST_RC" = 127 ]; then
    record "$analysis_name" skipped "$LAST_DURATION" "the analysis tool is not installed in this image, so nothing was analysed" "$LAST_RC" "$analysis_index"
  else
    record "$analysis_name" "$LAST_STATUS" "$LAST_DURATION" "$LAST_EXCERPT" "$LAST_RC" "$analysis_index" "$LAST_LIMIT"
  fi
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
