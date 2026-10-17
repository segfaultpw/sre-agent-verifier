# Verifier runner contract

One verification is one container start. The container fetches what it needs from one
URL, clones a repository at a base commit, runs the repository's own commands before
and after a proposed patch, reports what happened to a one-time URL and exits. The same
image runs on SRE Agent's hosted verifier (an ephemeral container task) and on
infrastructure a customer owns; nothing in it is specific to one account.

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
  "analysis": {"rule_pack": "v1-3fa9c012", "files": {"missing-timeouts.yml": "rules: ..."}}
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
commands. The runner reads no other environment variable.

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
   root-owned staging directory nobody else can reach. The token is given to that one
   command through git's environment configuration, so it is written to no file and no git
   config and is on no command line. The tree is then handed to the unprivileged user and
   moved into place with one rename: root never writes inside a directory that user can
   write. Step `clone`.
4. Checks every path in the patch again (no absolute path, no `..`, no `.git`, no doubled
   or trailing separator, no symbolic link on the way), because the runner decides where it
   is written. At most 20 files. Every write to the tree from here on (the patch, taking it
   back) is made by the unprivileged user, from a copy of the content that only root can
   write, and every git command the runner runs on the tree (`git ls-files` to learn whether
   a path is tracked, `git checkout -- <path>` to take it back) runs the way a step runs,
   below.
5. Runs the `setup` commands. A failure ends the run with `baseline_green: false`.
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
`GET`, the clone and the one `PUT` (and whatever the repository's own commands do).

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
  "runner": {"runtime": "elixir", "version": 2},
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
user and that the runner's own time cap holds.

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
