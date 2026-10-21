"""The runner is root and a repository's commands are not, so nothing the commands can write may
ever be executed by the runner. This pins that in four ways, none of which needs root: the
source sets one fixed PATH before its first command and never hands the image's PATH to
anything but the unprivileged user's commands; a program planted under the name of each tool
the runner starts is never run, even when it is the only directory the runner was given to find
tools in; every program the runner starts as root is on the list the image check verifies; and
the ownership check itself judges owners, modes, links and missing paths correctly.
verifier/test/contract.sh proves the same inside every image with real root (the planted
binaries in every directory the unprivileged user can write, the image audit, the refusal to
start on a writable tool). Standard library only."""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import session_support
from session_support import RUN_PATH, SessionCase, job, runner_tools, step
from support import VERIFIER, TempDirCase, requires_tools

RUNNER_SOURCE = (VERIFIER / "run.sh").read_text()
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def code_lines(text):
    return [line for line in text.split("\n") if line.strip() and not line.lstrip().startswith("#")]


def listed(name, text=RUNNER_SOURCE):
    body = re.search(r"^%s=\(\n(.*?)^\)" % name, text, re.M | re.S)
    return body.group(1).split() if body else []


def optional_tools():
    found = re.search(r"^ROOT_TOOLS_OPTIONAL=\((.*?)\)", RUNNER_SOURCE, re.M)
    return found.group(1).split() if found else []


# What a runner without the list would still call, so the probe means something on any version.
ALWAYS_PLANTED = (
    "base64 cat chmod chown comm cp curl cut date env find getent git grep head id install jq mkdir mkfifo "
    "mv readlink realpath rm sed setpriv setsid sleep sort stat tail timeout tr wc xargs ipcrm"
).split()


class SourceTest(unittest.TestCase):
    def test_the_first_statements_fix_the_path_before_any_command(self):
        statements = code_lines(RUNNER_SOURCE)
        self.assertEqual(
            statements[:5],
            [
                "set -uo pipefail",
                "SAFE_PATH=%s" % SAFE_PATH,
                "IMAGE_PATH=${PATH:-$SAFE_PATH}",
                'if [ "$EUID" -ne 0 ] && [ "${VERIFIER_ALLOW_UNPRIVILEGED:-0}" = 1 ] && [ -n "${VERIFIER_TEST_BIN:-}" ]; then SAFE_PATH=$VERIFIER_TEST_BIN:$SAFE_PATH; fi',
                "PATH=$SAFE_PATH",
            ],
        )
        self.assertEqual(statements[5:7], ["export PATH", "hash -r"])

    def test_nothing_after_the_reset_sets_path_for_root(self):
        assignments = [
            (n, l.strip())
            for n, l in enumerate(RUNNER_SOURCE.split("\n"), 1)
            if re.match(r"\s*(export\s+)?PATH=", l)
        ]
        self.assertEqual([a[1] for a in assignments], ["PATH=$SAFE_PATH"])

    def test_the_image_path_reaches_only_the_unprivileged_users_commands(self):
        users = [l.strip() for l in RUNNER_SOURCE.split("\n") if "$IMAGE_PATH" in l and not l.lstrip().startswith("#")]
        self.assertEqual(
            [u for u in users if not u.startswith("IMAGE_PATH=")],
            ['BASE_ENV=("PATH=$IMAGE_PATH" "HOME=$SANDBOX_HOME" "LANG=C.UTF-8" "CI=true" "TERM=dumb" "GIT_TERMINAL_PROMPT=0")'],
        )
        self.assertIn('OP_ENV=("PATH=$SAFE_PATH"', RUNNER_SOURCE)
        self.assertNotIn('"PATH=$PATH"', RUNNER_SOURCE)

    def test_the_step_shell_is_the_one_the_runner_runs_under_never_a_name_looked_up(self):
        self.assertIn('setsid "${SANDBOX_PREFIX[@]}" "$BASH" -c', RUNNER_SOURCE)
        self.assertIn('exec "$BASH" -c "$1"', RUNNER_SOURCE)
        self.assertNotRegex(RUNNER_SOURCE, r"\bexec bash -c")

    def test_the_restore_covers_the_runtime_places_that_are_world_writable(self):
        self.assertIn("STATE_ROOTS+=(/tmp /var/tmp /dev/shm /run/lock /dev/mqueue)", RUNNER_SOURCE)

    def test_the_tool_list_is_checked_before_anything_else_runs_as_root(self):
        gate = RUNNER_SOURCE.index('if [ "$EUID" -eq 0 ] && ! verify_root_tools; then exit 2; fi')
        for first in ("mkdir -p \"$OUT\"", "SANDBOX_UID=$(id -u"):
            self.assertLess(gate, RUNNER_SOURCE.index(first), first)

    def test_the_list_names_tools_and_the_optional_one(self):
        tools = listed("ROOT_TOOLS")
        for must in ("jq", "wc", "date", "find", "git", "curl", "base64", "setpriv", "timeout"):
            self.assertIn(must, tools)
        self.assertEqual(sorted(set(tools)), sorted(tools), "a tool is listed twice")
        self.assertEqual(optional_tools(), ["ipcrm"])


def plant_wrappers(directory, names, marker_dir):
    """A wrapper for each name that records its use and then runs the real program."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        real = shutil.which(name)
        if real is None:
            continue
        script = directory / name
        script.write_text('#!/bin/sh\n: >>"%s/ran-%s"\nexec "%s" "$@"\n' % (marker_dir, name, real))
        script.chmod(0o755)


@runner_tools
class PlantedProgramTest(SessionCase):
    EXTRA = ("sh", "dirname", "ls", "touch", "awk", "mktemp", "true", "sha256sum", "md5sum", "openssl", "python3")

    def plant(self):
        self.markers = self.dir / "markers"
        self.markers.mkdir()
        self.evil = self.dir / "evil-bin"
        names = list(dict.fromkeys(ALWAYS_PLANTED + listed("ROOT_TOOLS") + optional_tools() + list(self.EXTRA)))
        plant_wrappers(self.evil, names, self.markers)
        self.assertTrue((self.evil / "jq").exists(), "the probe planted nothing")

    def only_the_planted_directory(self):
        # bash itself is how the test starts the runner; it is not one of the tools planted.
        bash_dir = self.dir / "bash-bin"
        bash_dir.mkdir(exist_ok=True)
        if not (bash_dir / "bash").exists():
            (bash_dir / "bash").symlink_to(shutil.which("bash"))
        return {"PATH": "%s:%s" % (self.evil, bash_dir)}

    def ran(self):
        return sorted(p.name for p in self.markers.iterdir())

    def test_a_session_never_runs_a_planted_program_and_needs_no_path_of_its_own(self):
        self.plant()
        steps = [
            step(0, "deps", "setup", ": setup"),
            step(1, "unit", "test", "test -f calc.sh"),
            step(2, "lint", "check", ": lint"),
        ]
        scenario = self.session_scenario(steps, [job(1, [steps[2]], [("calc.sh", "echo 4\n", False)]), job(2, [steps[2]])])
        platform, done = self.run_scenario_with(scenario)
        self.assertRunnerOk(done)
        self.assertAllPass(self.ready())
        self.assertAllPass(self.result(1))
        self.assertAllPass(self.result(2))
        self.assertEqual(self.ran(), [], "the runner started a program the repository could have planted")

    def test_a_run_never_runs_a_planted_program_and_needs_no_path_of_its_own(self):
        self.plant()
        scenario = {
            "bootstrap": {
                "repo_url": self.repo_url(),
                "repo_token": "ghs_run_token",
                "base_sha": self.sha,
                "result_url": "http://127.0.0.1:@PORT@/result",
                "wall_clock_seconds": 300,
                "max_output_bytes": 100000,
                "steps": [
                    step(0, "deps", "setup", ": setup"),
                    step(1, "unit", "test", "test -f calc.sh"),
                    step(2, "lint", "check", ": lint"),
                    step(3, "new_test", "new_test", "test -f test_new.txt"),
                ],
                "files": [
                    {"path": "calc.sh", "content": "echo 4\n", "test": False},
                    {"path": "test_new.txt", "content": "x\n", "test": True},
                ],
            }
        }
        platform = self.platform(scenario)
        done = self.run_runner(platform, path=RUN_PATH, extra=self.only_the_planted_directory())
        self.assertRunnerOk(done)
        result = platform.body("result.json")
        self.assertTrue(result["baseline_green"], result)
        self.assertEqual(self.ran(), [], "the runner started a program the repository could have planted")

    def run_scenario_with(self, scenario):
        platform = self.platform(scenario)
        return platform, self.run_runner(platform, extra=self.only_the_planted_directory())

    def test_the_probe_catches_a_runner_that_looks_tools_up_on_its_inherited_path(self):
        self.plant()
        broken = self.dir / "run-inheriting.sh"
        source = RUNNER_SOURCE.replace("PATH=$SAFE_PATH\nexport PATH\n", "export PATH\n", 1)
        self.assertNotEqual(source, RUNNER_SOURCE)
        broken.write_text(source)
        steps = [step(0, "deps", "setup", ": setup"), step(1, "unit", "test", "test -f calc.sh")]
        platform = self.platform(self.session_scenario(steps, []))
        with mock.patch.object(session_support, "RUNNER", broken):
            self.run_runner(platform, extra=self.only_the_planted_directory())
        self.assertNotEqual(self.ran(), [], "the probe saw nothing although the PATH was inherited")


@runner_tools
class EveryProgramIsListedTest(SessionCase):
    """Runs the runner with its own trace on, in both modes, and checks that every program it
    starts itself is on the list the image check verifies."""

    BUILTIN_WRAPPERS = {"exec", "command", "time"}

    def traced_runner(self):
        copy = self.dir / "run-traced.sh"
        trace = self.dir / "trace.log"
        trace.write_text("")
        source = RUNNER_SOURCE.replace(
            "set -uo pipefail\n",
            "set -uo pipefail\nexec 9>>'%s'\nBASH_XTRACEFD=9\nPS4='+TRACE '\nset -x\n" % trace,
            1,
        )
        self.assertNotEqual(source, RUNNER_SOURCE)
        copy.write_text(source)
        return copy, trace

    def programs(self, trace, functions):
        builtins = set(subprocess.run(["bash", "-c", "compgen -b; compgen -k"], capture_output=True, text=True).stdout.split())
        found = set()
        for line in trace.read_text().split("\n"):
            match = re.match(r"^\++TRACE (.*)$", line)
            if not match:
                continue
            words = match.group(1).split(" ")
            while words and words[0] in self.BUILTIN_WRAPPERS:
                words = words[1:]
            # An assignment (`args+=(...)`, `NAME=value`) starts no program.
            if not words or "=" in words[0].split("/")[0]:
                continue
            word = words[0].strip("'\"")
            if word in ("timeout", "setsid", "xargs"):
                found.add(word)
                rest = [w for w in words[1:] if not w.startswith("-") and not w.isdigit()]
                word = rest[0].strip("'\"") if rest else ""
            if not word or "/" in word or word in builtins or word in functions or not re.match(r"^[A-Za-z0-9._+-]+$", word):
                continue
            found.add(word)
        return found

    def test_every_program_either_flow_starts_is_on_the_list(self):
        functions = set(re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)\(\) \{", RUNNER_SOURCE, re.M))
        copy, trace = self.traced_runner()
        steps = [
            step(0, "deps", "setup", "mkdir -p cache"),
            step(1, "unit", "test", "sh test_old.sh"),
            step(2, "lint", "check", "echo lint"),
            step(3, "scan", "analysis", "echo {rules_dir}"),
        ]
        platform = self.platform(
            self.session_scenario(
                steps,
                [job(1, [steps[2], steps[3]], [("calc.sh", "echo 4\n", False)]), job(2, [steps[2]])],
                bootstrap={"analysis": {"rule_pack": "v1", "files": {"a.yml": "rules:\n"}}},
            )
        )
        with mock.patch.object(session_support, "RUNNER", copy):
            self.assertRunnerOk(self.run_runner(platform))
        session_programs = self.programs(trace, functions)

        trace.write_text("")
        run_scenario = {
            "bootstrap": {
                "repo_url": self.repo_url(),
                "repo_token": "ghs_run_token",
                "base_sha": self.sha,
                "result_url": "http://127.0.0.1:@PORT@/result",
                "wall_clock_seconds": 300,
                "max_output_bytes": 100000,
                "steps": [
                    step(0, "deps", "setup", "echo installing"),
                    step(1, "unit", "test", "sh test_old.sh"),
                    step(2, "lint", "check", "echo lint"),
                    step(3, "new_test", "new_test", "sh test_new.sh"),
                    step(4, "scan", "analysis", "echo {rules_dir}"),
                ],
                "files": [
                    {"path": "calc.sh", "content": "echo 4\n", "test": False},
                    {"path": "test_new.sh", "content": 'test "$(sh calc.sh)" = 4\n', "test": True},
                ],
                "analysis": {"rule_pack": "v1", "files": {"a.yml": "rules:\n"}},
            }
        }
        platform = self.platform(run_scenario)
        with mock.patch.object(session_support, "RUNNER", copy):
            self.assertRunnerOk(self.run_runner(platform, path=RUN_PATH))
        run_programs = self.programs(trace, functions)

        allowed = set(listed("ROOT_TOOLS")) | set(optional_tools())
        for flow, programs in (("session", session_programs), ("run", run_programs)):
            self.assertGreater(len(programs), 10, "the trace read nothing: %s" % sorted(programs))
            extra = sorted(programs - allowed)
            where = [l for l in trace.read_text().split("\n") if any(("TRACE %s " % w) in l or ("TRACE '%s'" % w) in l for w in extra)]
            self.assertEqual(extra, [], "%s starts programs the image check does not verify: %s" % (flow, where[:5]))
        self.assertIn("wc", session_programs & run_programs)


def run_bash(script, *args):
    done = subprocess.run(["bash", "-c", script, "bash", *args], capture_output=True, text=True)
    return done.returncode, done.stdout + done.stderr


def checker():
    """The two functions of the runner that judge ownership, loaded into a shell of their own."""
    start = RUNNER_SOURCE.index("declare -A TRUSTED_SEEN=()")
    end = RUNNER_SOURCE.index("# verify_root_tools:")
    return "set -uo pipefail\nUNTRUSTED=''\n" + RUNNER_SOURCE[start:end]


@requires_tools("bash", "stat", "readlink")
class OwnershipCheckTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.me = str(os.getuid())

    def path_ok(self, target, owner=None):
        code, out = run_bash(checker() + '\ntrusted_path "$1" "$2"', str(target), owner or self.me)
        return code == 0

    def chain_ok(self, target, owner="0"):
        code, out = run_bash(checker() + '\ntrusted_chain "$1" "$2" || { echo "UNTRUSTED=$UNTRUSTED"; exit 1; }', str(target), owner)
        return code == 0, out

    def test_a_path_is_trusted_only_without_a_group_or_other_write_bit(self):
        target = self.dir / "dir"
        target.mkdir()
        for mode, trusted in ((0o755, True), (0o700, True), (0o775, False), (0o757, False), (0o777, False), (0o1777, False), (0o2755, True)):
            target.chmod(mode)
            self.assertEqual(self.path_ok(target), trusted, oct(mode))
        plain = self.dir / "file"
        plain.write_text("x")
        for mode, trusted in ((0o755, True), (0o664, False), (0o646, False)):
            plain.chmod(mode)
            self.assertEqual(self.path_ok(plain), trusted, oct(mode))

    def test_a_path_owned_by_somebody_else_is_not_trusted(self):
        target = self.dir / "dir"
        target.mkdir()
        target.chmod(0o755)
        self.assertTrue(self.path_ok(target, self.me))
        self.assertFalse(self.path_ok(target, str(int(self.me) + 1)))

    @unittest.skipUnless(os.path.exists("/usr/bin") and os.stat("/usr/bin").st_uid == 0, "needs a root-owned /usr/bin")
    def test_a_chain_is_trusted_when_every_place_above_it_is_roots_and_closed(self):
        ok, out = self.chain_ok("/usr/bin")
        self.assertTrue(ok, out)
        ok, out = self.chain_ok(shutil.which("env"))
        self.assertTrue(ok, out)

    @unittest.skipUnless(os.path.exists("/usr/bin") and os.stat("/usr/bin").st_uid == 0, "needs a root-owned /usr/bin")
    def test_a_chain_through_a_world_writable_directory_is_not_trusted_and_names_it(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as scratch:
            target = Path(scratch) / "tool"
            target.write_text("#!/bin/sh\n")
            target.chmod(0o755)
            ok, out = self.chain_ok(target)
            self.assertFalse(ok)
            self.assertIn("UNTRUSTED=", out)

    @unittest.skipUnless(os.path.exists("/usr/bin") and os.stat("/usr/bin").st_uid == 0, "needs a root-owned /usr/bin")
    def test_a_link_is_judged_by_what_it_points_at(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as scratch:
            planted = Path(scratch) / "planted"
            planted.write_text("#!/bin/sh\n")
            planted.chmod(0o755)
            link = self.dir / "wc"
            link.symlink_to(planted)
            self.assertFalse(self.chain_ok(link)[0], "a link to a file in a writable directory")
            closed = self.dir / "env"
            closed.symlink_to(shutil.which("env"))
            self.assertTrue(self.chain_ok(closed)[0], "a link to a root-owned program")

    @unittest.skipUnless(os.path.exists("/usr/bin") and os.stat("/usr/bin").st_uid == 0, "needs a root-owned /usr/bin")
    def test_a_missing_path_is_judged_by_the_place_that_could_create_it(self):
        self.assertTrue(self.chain_ok("/usr/bin/a-program-that-is-not-there")[0])
        self.assertFalse(self.chain_ok("/tmp/a-program-that-is-not-there")[0])
        self.assertFalse(self.chain_ok("/tmp/a-directory/that-is-not-there/either")[0])


if __name__ == "__main__":
    unittest.main()
