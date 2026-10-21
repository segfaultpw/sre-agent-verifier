# Verifier runner contract

One verification is one container start. The container fetches what it needs from one
URL, clones a repository at a base commit, runs the repository's own commands before
and after a proposed patch, reports what happened to a one-time URL and exits. The same
image runs on SRE Agent's hosted verifier (an ephemeral container task) and on
infrastructure a customer owns; nothing in it is specific to one account.

A runner of version 3 can also serve a **session**: one container start, one clone and a
sequence of jobs for one fix, each reported on its own URL, until the platform ends the
session (see "Session mode (runner version 3)" below). A run is unchanged by that: a
version 3 runner given a run bootstrap does what a version 2 runner did, and a version 2
platform keeps working against a version 3 runner.

The platform starts the task, mints the token and URLs, serves the two URLs and judges
what a result may contain. This file is the other half: what the runner reads, what it
does and what it sends back.

## Environment the platform sets

Exactly one variable, as an ECS container override. A change here is a change to the
platform's task request and to the test that lists it, and it must never be a
credential: the RunTask request is returned by `ecs:DescribeTasks` and recorded in the
verifier account's CloudTrail.

| variable | meaning |
| --- | --- |
| `BOOTSTRAP_URL` | a signed, single-claim `GET` that expires in minutes (the startup margin, 300 seconds by default) |

The URL carries an HMAC signature over the run, its organization, its expiry and the
purpose. It answers once. A second claim, or a claim after the expiry, is refused, so
once the task has started (or the minutes have passed) the URL in the RunTask request is
worth nothing to anyone who reads it later. The task is given **no** platform secret and
**no** write credential, and its task role has no permissions.

The runner reads `BOOTSTRAP_URL`, writes it to a mode-600 file only root can open, unsets
it, and passes it to `curl` with `--config`, so it is on no command line. The same is true
of the result URL below.

## The bootstrap response

The runner fetches the bootstrap with the query parameter `runner=3` (its version) added to
the URL, every time. A version 2 platform ignores the parameter. A platform that can serve
sessions gives a session only to a request that carries it, so a runner that does not know
what a session is is never given one.

```json
{
  "repo_url": "https://github.com/<owner>/<repo>.git",
  "repo_token": "a read-only GitHub installation token for this one repository",
  "base_sha": "the commit to check out",
  "result_url": "a one-time PUT that takes the result JSON",
  "wall_clock_seconds": 900,
  "max_output_bytes": 1000000,
  "steps": [{"index": 0, "name": "tests", "kind": "test", "cmd": "mix test"}],
  "files": [{"path": "lib/a.ex", "content": "...", "test": false}],
  "analysis": {"rule_pack": "v1-3fa9c012", "files": {"missing-timeouts.yml": "rules: ..."}},
  "proxy": {"user": "run:<id>", "runner": "<secret>", "setup": "<secret>"},
  "setup_done_url": "a one-time PUT the runner makes once the last setup command has ended",
  "network": false
}
```

* `repo_token` is minted at the moment of the claim (`contents: read`, one repository,
  nothing else) and is stored nowhere on the platform.
* `steps` are the profile's commands in order, at most 12, each with its `index` in the
  run's own list. The new-test command has `{test_files}` already replaced by the quoted
  paths of the proposed files the runtime's own runner would collect; a new-test command
  with nothing to name is left out.
* `files` are the proposed files, at most 20; `test` marks test code (written before the
  unmodified-tree run so the new test has what it needs).
* `analysis` is present only when the run has an `analysis` command. `files` is the
  platform's static analysis rule pack, file name to text, and `rule_pack` its version
  (the rules travel with the platform's release and never with an image). The runner refuses a pack that is not an object of at most 20 files
  named `[a-z0-9][a-z0-9-]{0,60}.yml`, each a string of at most 20,000 bytes, with a
  runner error that names the rule pack, before it clones anything. It writes the files, as
  root and mode 0644, to a directory of its own (`$WORK/rules`) that no command can write to,
  and expands `{rules_dir}` in analysis commands to it. In an analysis command the platform
  has already replaced `{touched_files}` with the quoted paths of every file of the patch
  (a command that names them and has none is left out, as a `new_test` command is) and
  `{rule_pack}` with the version.
* `proxy`, `setup_done_url` and `network` are the network rule, below. A run bootstrap
  without them, as a version 2 platform sends it, gives every command the task definition's
  proxy variables, exactly as before.
* If the bootstrap is refused or unreachable there is no result URL, so nothing can be
  reported: the runner exits `1`. A hosted run ends at its deadline; a customer-run's job
  does not wait that long: the platform ends it as `error` / `start_failed` the moment its
  bootstrap URL expires, and the pull request says the verifier collected the job but its
  task never started.

Variables baked into the image or set by a test, never by the platform:
`VERIFIER_RUNTIME`, `VERIFIER_STEP_ENV_FILE` (default `/etc/verifier/step.env`),
`VERIFIER_WORKDIR` (default `/work`), `VERIFIER_SANDBOX_USER` (default `sandbox`) and
`VERIFIER_ALLOW_UNPRIVILEGED` (tests only). `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY`
(and lower-case forms) may come from the task definition; they are forwarded to the
commands, subject to the network rule below. `VERIFIER_PROXY_URL` is the one more variable
of the task definition (never of the RunTask request): on the hosted verifier it names the
egress proxy as `host:port` (an `http://` prefix is accepted) and switches the runner to
per-run proxy secrets; a value that is not a host and port ends the runner with exit `2`.
The runner reads no other environment variable.

## Command kinds

| kind | when it runs |
| --- | --- |
| `setup` | once, on the unmodified tree, before anything is judged |
| `test` | the repository's tests: first on the unmodified tree (the baseline), then again after the patch |
| `check` | formatter, linter, after the patch |
| `analysis` | static analysis on the files the patch touches, after the checks and before the tests (at most four). Its findings are evidence for the person reading the pull request and never a verdict: a nonzero exit is a `fail` step that the platform shows as "findings" and does not count against the change. Exit 127, the shell's "command not found", is reported `skipped`: the tool is not in this image, which is not a finding |
| `new_test` | only the new test files: on the tree with only the patch's test files applied, expecting it to fail, then after the full patch, expecting it to pass |

Command names are `[a-z][a-z0-9_]{0,39}` and never `baseline`, `new_test_fails_before`,
`new_test_passes_after`, `clone` or `patch`: those are steps the runner reports itself.

## What the runner does

1. Refuses to run commands unless it can separate them from its own secrets: it must
   start as root (the images do) so it can run every command as the unprivileged
   `sandbox` user, or the test knob must be set. A sandbox user whose uid or primary group is 0
   is refused too: every command would run as root (or in root's group) and the uid-wide kill below would end the runner.
2. Fetches the bootstrap, once.
3. Clones: `git init`, `git fetch --depth 1 origin $BASE_SHA`, `git checkout`, into a
   root-owned staging directory nobody else can reach. The token (and, on the hosted
   verifier, the proxy and its secret) is given to that one command through git's
   environment configuration, so it is written to no file and no git config and is on no
   command line. The fetch is held to the run's clock. The tree is then handed to the
   unprivileged user and moved into place with one rename: root never writes inside a
   directory that user can write. Step `clone`.
4. Checks every path in the patch again (no absolute path, no `..`, no `.git`, no doubled
   or trailing separator, no symbolic link on the way), because the runner decides where it
   is written. At most 20 files. Every write to the tree from here on (the patch, taking it
   back) is made by the unprivileged user, from a copy of the content that only root can
   write, and every git command the runner runs on the tree (`git ls-files` to learn whether
   a path is tracked, `git checkout -- <path>` to take it back) runs the way a step runs,
   below.
5. Runs the `setup` commands. A failure ends the run with `baseline_green: false`. When
   the bootstrap carried a `setup_done_url`, the runner then `PUT`s `{}` to it, and the
   answer may carry `{"proxy": {"checks": "<secret>"}}` (the network rule, below).
6. Runs the `test` commands on the unmodified tree, reported as ONE step named
   `baseline`. The first failure sets `baseline_green: false` and ends the run: the change
   cannot be judged.
7. If the patch has a test file and the profile has a `new_test` command: writes only the
   test files, runs the command expecting it to **fail** (step `new_test_fails_before` is
   `pass` when it failed, `fail` when the new test passed on the unmodified tree, which
   proves nothing), and restores the tree. Otherwise the step is reported `skipped`.
8. Writes the whole patch, runs the `check` commands, the `analysis` commands, the `test`
   commands and, if it ran before, `new_test_passes_after`. The analysis commands are the
   only ones that read the rule pack, and they run like every command: as the unprivileged
   user, in a session of their own, with the rebuilt environment, killed with everything
   they started.
9. Uploads the result and exits `0`. A refused upload exits `1`. A missing
   `BOOTSTRAP_URL` exits `2`: nothing can be fetched.

The runner never reads another variable, never writes outside its workspace, never
pushes, comments or opens anything, and never makes a request other than the bootstrap
`GET`, the clone, the `PUT` of `setup_done_url` when there is one and the one `PUT` of the
result (and whatever the repository's own commands do). A session makes the requests listed
under "Session mode (runner version 3)" instead of the one `PUT`.

## What a command sees, and what it leaves behind

Each command is started as the unprivileged user, in **its own session**, with an
environment rebuilt from scratch: `PATH`, `HOME`, `LANG`, `CI=true`, `TERM`,
`GIT_TERMINAL_PROMPT`, the proxy variables, and the lines of the image's `step.env`. It
sees nothing the container was started with, and because the runner is root and the command
is not, it cannot read the runner's `/proc/<pid>/environ` either. The runner's own
directory is mode `0700`, and the files in it that hold a URL are mode `0600`.

When a command ends, **everything it started is ended with it**: the runner sends
`SIGTERM` and then `SIGKILL` to the command's process group (a `kill` to the negative group id), and then
`SIGKILL` to every process the unprivileged user still owns, which catches a daemon that
left the group with a session of its own, and waits until none is left. Nothing a command
leaves behind can keep running into the next step, hold the result open, read what the
runner holds or race the runner's own work. (When the runner is not root, as in the host
tests, the group is all there is and nothing else is signalled.)

The runner's own operations on the tree get the same treatment, because the repository's
commands can plant what they run: a `post-checkout` hook in `.git/hooks`, a `core.hooksPath`
or `core.fsmonitor` or a filter in `.git/config` or the home directory's `.gitconfig`, a FIFO
where a patched file goes. Each operation runs as the unprivileged user in its own session,
under the run's clock, with an environment of its own (`HOME` that does not exist,
`GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`), git itself started with
`-c core.hooksPath=/dev/null -c core.fsmonitor=false` (command-line settings outrank the
repository's own), and everything it started is ended when it returns. An operation the clock
cuts short is reported as a `timeout` of the runner's own `patch` step, which the platform
reads as a limit. The helper that sends the uid-wide `SIGKILL` runs with an empty
environment: `env -i` starts it, as root, before it becomes the unprivileged user.

## Own limits (second line behind the platform's reaper)

* wall clock: `wall_clock_seconds` minus 30 seconds for the upload. A command that is still
  running when it runs out is ended the way every command is ended (group, then user), and
  its step is `timeout`; the cap holds whatever the command does with signals;
* output: each command's combined output is cut at `max_output_bytes`; such a step is
  `limit` with `limit: "output"`, which the platform reads as "output too large", not as a
  failing change;
* memory: a command killed with `SIGKILL` (exit 137) when the runner's own clock did not do
  it is `limit` with `limit: "memory"`: that is what the kernel's out-of-memory killer does
  to a step;
* result size: the excerpts are shortened so the whole result stays under the 64 KB the
  platform accepts.

## The result

```json
{
  "schema": 1,
  "baseline_green": true,
  "steps": [
    {"name": "tests", "status": "pass", "duration_ms": 1234, "excerpt": "...", "exit_code": 0, "index": 1}
  ],
  "runner": {"runtime": "elixir", "version": 3},
  "runner_error": "only when the run could not start"
}
```

Status is one of `pass`, `fail`, `error`, `timeout`, `limit`, `skipped`. A step of the
profile carries its `index` in the run's command list, and **never the command's text**:
the platform already holds the command (it came from the profile), shows it from its own
copy, and a result must not depend on how long a command is. At most 20 steps; the runner
adds `clone`, `baseline`, `new_test_fails_before` and `new_test_passes_after` to the
profile's commands (at most 12), so a full profile is 16 steps. The result is
**untrusted** and the runner never says whether it succeeded: the platform bounds it (64 KB,
20 steps, 4000 characters of excerpt, an index inside the run's list), redacts it and judges
it (`ResultValidator.outcome/2`). A run is `verified` only when the repository's own test
suite ran green on the unmodified tree (`baseline`) and again after the patch (every `test`
command, by index), nothing failed or hit a limit, and the new test failed before and
passed after. The runner's own `clone` step proves nothing, and a profile with no `test`
command is never `verified`: it ends `unverified` with the reason "no test suite
configured".

## The network rule and the proxy secrets

Whether a repository's commands may reach the network is a setting of the repository on the
platform (`check_network`, off unless an administrator turns it on). The runner does its
half of enforcing it by what it hands each command, in three phases:

| phase | commands | what they are given |
| --- | --- | --- |
| setup | `setup` | the network: the task definition's proxy variables, or on the hosted verifier the `setup` secret |
| checks | everything after setup: `check`, `analysis`, `test`, `new_test`, and the baseline of a run or of a session | the network only when the repository's setting allows it |

Setup ends at one point, the same in a run and in a session: after the last `setup` command and
before any other command (the baseline included), the runner `PUT`s `{}` to `setup_done_url` and
the platform revokes the `setup` secret there. A session's baseline therefore runs under the
`checks` rule, never under the `setup` secret.

**Hosted verifier** (`VERIFIER_PROXY_URL` set, and the bootstrap carries `proxy`). The task
can reach nothing but the egress proxy, and the proxy admits a request only with a secret the
platform lists as live for the host asked for. The secrets are per run or session, random,
and stored only as hashes on the platform:

* Before the bootstrap is claimed the runner authenticates to the proxy as `run:<id>` or
  `session:<id>` (read from its own bootstrap URL, the path
  `.../verifier/<id>/<signature>/bootstrap` or `.../verifier/sessions/<id>/<signature>/bootstrap`)
  with the URL's `<signature>` segment as the secret. That reaches the platform host and
  nothing else.
* The bootstrap's `proxy` object holds `user`, `runner` and `setup`. The runner's own
  requests (the clone, every request to the platform) use `user` and `runner`, through
  `curl --config` and git's environment configuration, never on a command line and never in
  a command's environment.
* The proxy settings of a command reach it through a pipe its own shell reads and closes before
  it runs anything, never as an argument of `env`, `setpriv` or `bash`.
* `setup` commands get `HTTP_PROXY`, `HTTPS_PROXY` and the lower-case forms set to
  `http://<user>:<setup>@<host:port>` (percent-encoded), and `NO_PROXY` set to
  `localhost,127.0.0.1,::1` so a repository's tests still reach the services they start.
  The platform revokes the `setup` secret when `setup_done_url` is `PUT` (a run and a session
  alike), which happens before the baseline or any other later command runs, so a setup command
  that saves it to disk saves a secret that is dead before anything else runs. The runner cannot
  keep a setup command from writing what it was handed to a place a later command can read (both
  are the same unprivileged user); revoking it is the control, and the runner does not run another
  command when the platform did not take the message (a run ends with a `runner_error`, a session
  is reported on `ready_url` with one).
* Commands after setup get the same variables with the `checks` secret, which the platform
  hands over only in the answer to `setup_done_url`, and only when the repository's setting
  is on. Without it they get no proxy variable at all (not even `NO_PROXY`), and so no way to
  reach the network. A session bootstrap that carries `proxy` and no `setup_done_url` is
  refused (reported on `ready_url` as a `runner_error`) before anything is cloned.
* The proxy secret on the wire is HTTP Basic `Proxy-Authorization`, whose decoded value is
  `<kind>:<id>:<secret>` with `<kind>` `run` or `session`; the proxy splits at the last
  colon into the user `<kind>:<id>` and the secret.
* Commands never reach the platform host, whatever the setting; only the runner's own
  requests do.

**Customer-run verifier** (no `VERIFIER_PROXY_URL`). The customer's network rules decide.
`setup` commands get the task definition's proxy variables. Commands after setup get them
only when the bootstrap says `"network": true`; a session bootstrap without the key means
`false`. The runner cannot stop a command that reaches the network some other way, and
promises nothing about it. A run bootstrap that has no `network` key at all (a version 2
platform) keeps the version 2 behaviour: every command gets the proxy variables.

## Session mode (runner version 3)

A session is one container start that serves several jobs for one fix. The platform runs the
repository's own commands for the fix agent while it works (`run_checks`): the fix is staged
in a job, the runner restores the tree, writes the job's files and runs the named commands.
The runner holds only the profile's commands: every job names commands by `index` in the
session's snapshot, never by text of its own, and a job that names an index the snapshot does
not hold, or a kind it does not hold for that index, is refused. This amends "one run" above:
a session task serves one run of one organization and one repository, several jobs, and never
another run.

The session bootstrap carries `session` instead of `result_url` and `files`:

```json
{
  "session": {
    "ready_url": "one-time PUT for the clone, setup and baseline report",
    "setup_done_url": "one-time PUT the runner makes once the last setup command has ended",
    "next_url": "GET, valid until end_at, answers the next job",
    "end_at": "2026-10-21T12:30:00Z",
    "idle_seconds": 300,
    "job_seconds": 300,
    "setup_seconds": 600
  },
  "proxy": {"user": "session:<id>", "runner": "<secret>", "setup": "<secret>"},
  "network": false,
  "repo_url": "https://github.com/<owner>/<repo>.git",
  "repo_token": "a read-only GitHub installation token for this one repository",
  "base_sha": "the commit to check out",
  "steps": [{"index": 0, "name": "unit", "kind": "test", "cmd": "mix test"}],
  "analysis": {"rule_pack": "v1-3fa9c012", "files": {"missing-timeouts.yml": "rules: ..."}}
}
```

`repo_url`, `repo_token`, `base_sha`, `steps`, `analysis` and `max_output_bytes` are as in a run
bootstrap (`steps` is the session's whole command snapshot, at most 12; `analysis` and
`max_output_bytes` are optional). `base_sha` is required. `end_at` is an ISO 8601 UTC time, and
the three `_seconds` are whole numbers from 1 to 7200. A bootstrap that is not this shape is
reported on `ready_url` as a `runner_error` (when `ready_url` itself can be read) and the runner
exits `0`.

1. **Boot.** One clock, `setup_seconds`, for all of it. The runner clones `base_sha`, runs every
   `setup` command, `PUT`s `{}` to `setup_done_url` (the network rule, above), runs every `test`
   command on the unmodified tree (the session's baseline, under the `checks` rule) and `PUT`s
   the ready report to `ready_url`: the result schema (below) with the clone step,
   one step per `setup` command, and one step named `baseline` per `test` command, each with
   the `index` of its command. A red baseline does not end the session (the step says so and
   `baseline_green` is `false`); a failed `setup` command does: the runner reports it and
   exits `0`, and runs no job. A clone that fails is a `clone` step with status `error`, a
   `runner_error`, and the same exit.
2. **Setup done and the ready answer.** The answer to `setup_done_url` may carry
   `{"proxy": {"checks": "<secret>"}}` (the network rule); the answer to the ready report carries
   nothing the runner reads. A session whose `setup_done_url` the platform did not accept is
   reported on `ready_url` with a `runner_error` and no baseline, and the runner exits `0`. A
   ready report the platform answers `409` or `410` ends the runner with exit `0`; any other
   refusal with exit `1`.
3. **Loop.** `GET next_url?after=<seq of the last job answered>` (`after=0` first), with a
   timeout of 40 seconds (the platform holds a request up to 20 seconds):

   | next answers | the runner |
   | --- | --- |
   | `200` | runs the job |
   | `204` | asks again, after at least one second |
   | `410` | the session is over: exits `0` |
   | `404`, `429`, any `5xx`, no answer | waits 2, 4, 8 and then 20 seconds between attempts, and asks again |
   | any other answer | exits `1` |

   The runner makes no request after `end_at`, nor after `idle_seconds` without a job (counted
   from the ready report and from the end of each job), and exits `0` instead of waiting past
   either. A job it cannot report (no `seq`, no `result_url`, a `seq` not after the last one
   answered) is skipped and treated like a failed request.
4. **A job** is `{"seq": 3, "files": [{"path", "content", "test"}], "steps": [{"index",
   "kind", "cmd"}], "result_url": "one-time PUT"}`, `cmd` already expanded by the platform.
   Under the job clock (`job_seconds`, and never past `end_at`) the runner:
   * checks the job: at most 20 files, at most 12 steps, every step an `index` and `kind` of
     the snapshot, `kind` one of `check`, `analysis`, `test`, `new_test` (`setup` is never
     rerun). A refusal is a result with no steps and a `runner_error`, and the session goes on;
   * restores the tree to `base_sha` as the unprivileged user: git's own configuration, index,
     exclude and attributes files are put back from copies taken right after the clone,
     `HEAD` and the working tree and index are reset to `base_sha`, and every untracked file
     and directory that is not ignored is removed (`git clean -ffd`, never `-x`: the ignored
     caches `setup` built survive). A job therefore starts from the base commit plus its own
     files, whatever the previous job edited, committed, staged or left behind. Ignored files
     persist by design; the platform's final verification is a fresh clone;
   * writes the job's files with the same path rules as a patch (no absolute path, no `..`,
     no `.git`, no symbolic link on the way);
   * runs the job's steps in kind order `check`, `analysis`, `test`, `new_test`, each as every
     command runs (unprivileged user, own session, rebuilt environment, ended with everything
     it started). The step is reported under the snapshot's `name` for that `index`, with the
     `index`; `analysis` steps expand `{rules_dir}` as in a run;
   * `PUT`s the result to `result_url`: the result schema with `runner.version` `3`, steps
     by `index`, no baseline, no URL and no secret. A result answered `410` ends the runner with
     exit `0`; any other refusal with exit `1`.
5. **The end.** The session ends, with the runner exiting `0`, when the platform says `410`, at
   `end_at`, after `idle_seconds` without a job, after a job in which a command was cut off by
   the job clock (a `timeout` step; the platform stops the task as well), and when the tree
   cannot be restored (the result then carries a `runner_error`). Whatever ends a session, no
   command is left running.

Every URL and secret of a session lives in a mode 0600 file only root can open, like a run's.
Nothing but the runner's own `curl` and `git` is ever handed one, and on no command line.

## Running it in your own account

The runner is the same image and the same contract. Only the starter differs: instead of
our ECS starting the task, a poller in your account does, and it learns what to start
from an outbound request. **The platform never connects to your account.**

An organization chooses `hosted` (our sandbox, metered) or `customer` (its own, not
metered against the sandbox allowance; model tokens still count) in Integrations. A
`customer` run is a verification run like any other, in `customer` mode: it is queued
instead of started, and it uses none of the hosted sandbox's places.

The poller calls `GET /api/verifier/queue` with an organization API key holding the one
scope `verifier:poll` (no person's token can carry it), reporting `poller_version` and,
as `images[<runtime>]`, the digest of the image it would start for each runtime. It is
handed at most one job:

```json
{
  "run_id": "…", "runtime": "node", "repository": "acme/api", "base_sha": "…",
  "size": {"cpu": 1024, "memory": 2048, "ephemeral_gib": 30},
  "wall_clock_seconds": 900, "max_output_bytes": 1000000,
  "bootstrap_url": "https://…/webhooks/verifier/<run>/<signature>/bootstrap",
  "bootstrap_expires_at": "…", "test_job": false
}
```

A job holds the same `bootstrap_url` a hosted task is started with, minted when the job is
collected, and nothing else that is a credential: no repository token, no result URL, no
patch. The poller starts the published image for the job's runtime with `BOOTSTRAP_URL` set
to it, as the one environment variable, sized by `size`. From there the runner does exactly
what it does anywhere: claim the bootstrap once, run, PUT the result. A job nobody collects
within 15 minutes ends `uncollected` and the pull request opens unverified, with a line
that says so.

A job may instead be the start of a session: it then carries `"kind": "session"` and its
`wall_clock_seconds` covers the whole session (1,800). The poller starts the image exactly the
same way, and the runner serves the session until the platform ends it or the session
bootstrap's `end_at` (which falls inside `wall_clock_seconds`). A poller is given a session only
when the platform knows that the image digest it reported for the runtime is one that serves
sessions (runner version 3); an older digest keeps being given runs. With no
`VERIFIER_PROXY_URL` the network rule is the customer's own (see above): the task definition's
proxy variables go to `setup` commands, and to the commands after setup only when the session
bootstrap says `"network": true`.

Answers: `200` with `data` (jobs) and `refused` (runtimes not given work, each with a
`code` and a sentence); `401`, `403 insufficient_scope`; `403 feature_not_available` and
`409 hosted_mode`, which a poller waits through; and `426` with `poller_outdated`,
`runner_outdated`, `runner_unknown` or `runner_unreported`, whose `message` says what to
upgrade to. A poller is judged against the list the platform ships
(published with each platform release): a digest is `current`, `supported`
(still given work) or `retired` (too old), and any digest that is not on the list is
unknown. Results are validated and judged by the platform exactly as for hosted runs.

The poller's key handling, since its copy of the key is the one moving part the rotation
touches: the organization's API key is read from Secrets Manager once per container and
cached; when the platform refuses the key itself (`401`, or a `403` other than the
platform's two back-off answers `feature_not_available` and `hosted_mode`), the poller
drops the cache, reads the secret again and retries the poll once with the fresh key, so a
rotation needs no redeploy. A key still refused after the retry raises, which fails the
invocation: a revoked key must surface as an error, never as a healthy quiet poller.
`403 insufficient_scope` raises too, for the same reason. The poller pins its images by
digest in its own configuration (the module's `images` variable). When a published digest
rotates, the customer updates that variable and applies; until then the platform answers
`426` with the sentence naming what to upgrade to.

The poller ships in the `verifier` module of
[terraform-sre-agent](https://github.com/segfaultpw/terraform-sre-agent) (an AWS Lambda
that only dispatches, so the 15-minute Lambda limit never applies to a run).

## Images

One per runtime: `elixir`, `node` (JavaScript and TypeScript), `python`, `go`, `java`,
`php`. Each is built from `verifier/images/<runtime>/Dockerfile` with `verifier/` as the
context, from a base pinned by the digest of its multi-arch index (a tag its publisher
re-points, so the refresh below can move it), for both `amd64` and `arm64`, each on a native
runner, and published on its own: one runtime failing never stops another. The leg that builds an image contract-tests it, scans it
and pushes it (`.github/workflows/verifier-images.yml`); Dependabot proposes new base
digests, and the weekly rebuild re-resolves them itself (`verifier/refresh-bases.sh`).
The `node` and `python` images also raise the libraries their package manager bundles inside
itself (npm's `node_modules`, pip's `pip/_vendor`) to the releases the scan wants, because a
scan reads them as installed packages and no npm or pip release has the fixed ones yet
(`images/node/fix-npm-bundled.sh` and `images/python/fix-pip-vendored.sh`, with the floors
written in each Dockerfile). Each step looks at what the base carries first and, once the
base is new enough, does nothing and fetches nothing. When it has work to do, everything it
fetches is pinned and checked: the node floors by the sha512 their tarball must have, and the
python step's pip source by commit and its build tools and bundled libraries by the hashes in
`pip-fix-tools.lock` and `pip-vendored.lock`. What a repository can observe in the python
image: `pip freeze` still prints `pip==X` and `pip-compile` and `pip-sync` still run; the
rebuilt pip also replaces the wheel `python -m venv` seeds from (`ensurepip`'s bundled
wheel), so such a venv carries the fixed libraries too, while virtualenv, tox and uv do not
use that wheel.
`verifier/test/contract.sh` runs the contract against a local fixture repository inside an
image, including that a command sees none of the runner's variables and cannot read the
runner's `/proc/<pid>/environ`, that a daemon with its own session is dead when its step
ends, that no URL is on any command line, that the patch is written by the unprivileged
user and that the runner's own time cap holds. For session mode it also proves, with the
same uid separation: that a daemon a job started is dead before the next job; that the next
job's tree is the base commit as the unprivileged user restores it (an edited tracked file, an
untracked file, a staged file, a planted git configuration, hook and exclude file gone; an
ignored cache kept); that the job clock holds against a command that ignores `SIGTERM`; that
`setup` never reruns and that a job naming an index outside the snapshot is refused; that the
runner exits on `410` and when idle; that no URL or secret is on any command line (a scanner
running as root for the whole run looks) or in a command's environment; and, through a local
proxy that admits only the secrets the platform lists as live, that a job command opening a
connection to an allowed host fails after setup when the network rule is off and succeeds when it
is on, and that the secret a `setup` command saved is refused once setup is done, for the
baseline as for every later command. `tests/test_session.py` and `tests/test_session_network.py` run the same cases, and the
back-off table, against `run.sh` without Docker.

### The one scanner exception: pip's `pkg_resources` (CVE-2025-47273)

The Trivy gate blocks every fixable HIGH or CRITICAL finding. One finding in the `python`
image cannot be fixed by raising a library, and is recorded as a scoped exception instead.

| | |
| --- | --- |
| What the scan reports | `setuptools 70.3.0`, `CVE-2025-47273`, fixed in 78.1.1: a path traversal in setuptools' `PackageIndex` |
| Where it comes from | the `pkg:pypi/setuptools@70.3.0` entry of pip's own `pip/_vendor/bom.cdx.json`; there is no setuptools package in the image |
| What the image really holds | only `pip/_vendor/pkg_resources/__init__.py` (and its licence). `PackageIndex` lives in `setuptools/package_index.py`, which is absent |
| Why not drop or bump it | pip cannot use `pkg_resources` on Python 3.14, but pip-tools (`pip-compile`, `pip-sync`) and other tools import `pip._vendor.pkg_resources`, so a repository's baseline would fail in this image and pass in the stock one; pip's own `vendor.txt` pins setuptools 70.3.0 for it, and the latest setuptools (84.0.0) no longer ships `pkg_resources` at all |
| The exception | `verifier/images/python/pip-pkg-resources.openvex.json`: OpenVEX, `not_affected`, `vulnerable_code_not_present`, one statement, one CVE, one purl. The workflow's Trivy step passes every `verifier/images/<runtime>/*.openvex.json` with `--vex`; only the python image has one |
| What pins its scope | `verifier/check-vex.sh`, which the publishing workflow runs before every scan, refuses any document that is not exactly this exception, and any document in another runtime's directory; `tests/test_check_vex.py` holds the same rules, and `tests/test_images.py` fails if another OpenVEX document appears under `verifier/` |

Evidence (Trivy 0.58.1, the workflow's flags `--severity HIGH,CRITICAL --ignore-unfixed --exit-code 1`,
the python image built from this tree, 2026-10-07):

- with the document: `Total: 0 (HIGH: 0, CRITICAL: 0)`, exit 0;
- without it: exit 1, exactly one finding, `setuptools 70.3.0 CVE-2025-47273` (`pkg:pypi/setuptools@70.3.0`);
- on the image before this branch's fixes, the same document hid only that finding: msgpack 1.1.2
  and urllib3 2.7.0 (two CVEs) were still reported;
- in a `python -m venv` venv on the image, `pip-compile` compiles a requirement, and
  `pip-sync` installs it and then reports `Everything up-to-date`, both exit 0.

The exception is removed when pip vendors a fixed setuptools (or stops vendoring `pkg_resources`):
delete the document, and the Trivy step goes back to suppressing nothing.

### Bumping pip or Python in the python image

The python step pins pip by commit and every input by hash, so a base whose pip (or Python
minor version) is not the pinned one fails the python leg until the pins move. Nothing is
published for python meanwhile, the other runtimes are unaffected, and the failing step's own
message is in the CI log. The version is written in one place, the `--pip-source VERSION=COMMIT`
pair in `images/python/Dockerfile`, so a bump is that edit plus two regenerations, in this order
(`V` is the version the failing step names):

1. Pin the commit. `git ls-remote https://github.com/pypa/pip "refs/tags/$V^{}"` prints the commit
   of release `$V`. Review it, then replace the `--pip-source` pair in the Dockerfile with
   `--pip-source $V=<commit>`. `tests/test_images.py` accepts any single
   `--pip-source <version>=<40 hex>`, so nothing else names the version. If the new pip already
   carries a `--min` floor, delete that floor from the Dockerfile.
2. Regenerate `pip-vendored.lock` from that commit's `vendor.txt`, with each `--min` floor applied
   where `vendor.txt` is below it (the loop below applies urllib3 2.8.0 and msgpack 1.2.1, and
   builds msgpack from its sdist, as the Dockerfile says), keeping the comment header:

   ```bash
   cd verifier/images/python
   SHA=<the commit from step 1>
   SDISTS="msgpack"   # the Dockerfile's --build-from-sdist names
   curl -sf "https://raw.githubusercontent.com/pypa/pip/$SHA/src/pip/_vendor/vendor.txt" |
     sed -E 's/^(\s*)urllib3==.*/\1urllib3==2.8.0/; s/^msgpack==.*/msgpack==1.2.1/' > /tmp/vendor.txt
   { grep '^#' pip-vendored.lock
     sed -nE 's/^[[:space:]]*([A-Za-z0-9._-]+)==([^[:space:]#]+).*/\1 \2/p' /tmp/vendor.txt |
     while read -r name version; do
       sdist=false; case " $SDISTS " in *" $name "*) sdist=true ;; esac
       curl -sf "https://pypi.org/pypi/$name/$version/json" | jq -r --arg n "$name" --arg v "$version" --argjson sdist "$sdist" '
         [.urls[] | select(if $sdist then .packagetype == "sdist" else (.filename | endswith("-none-any.whl")) end) | .digests.sha256] | sort
         | "\($n)==\($v) \\\n" + (map("    --hash=sha256:" + .) | join(" \\\n"))'
     done; } > /tmp/pip-vendored.lock && mv /tmp/pip-vendored.lock pip-vendored.lock
   ```

3. Check `pip-fix-tools.lock`. Its `# pip-noxfile:` line must equal the `session.install("vendoring...")`
   requirement in that commit's `noxfile.py`, and it needs wheels for the base's Python. If either
   changed, regenerate it with the vendoring version the noxfile asks for and any setuptools of 78.1.1 or
   later, in the base image (Python 3.14 on Linux, pip-tools 7.6), and set the header line:

   ```bash
   printf 'vendoring==1.4.0\nsetuptools==84.0.0\n' > tools.in
   pip-compile --generate-hashes --strip-extras --allow-unsafe --no-header -o tools.lock tools.in
   { grep '^#' pip-fix-tools.lock
     awk '/^[A-Za-z]/ {skip = ($0 ~ /^pip==/)} NF && !/^#/ && !skip' tools.lock; } > /tmp/new.lock && mv /tmp/new.lock pip-fix-tools.lock
   ```

Then run `python3 -I -m unittest discover -s tests`, `verifier/test/contract.sh
python`, and the Trivy step as the workflow runs it. Both commands above were run against the pinned
pip 26.2.1 and reproduce the committed bodies of `pip-vendored.lock` and `pip-fix-tools.lock` byte for
byte. The node image needs no such procedure: a new floor is one `--min NAME@VERSION=sha512-...` line,
whose integrity is `npm view NAME@VERSION dist.integrity`.
