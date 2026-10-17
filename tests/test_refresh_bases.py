"""verifier/refresh-bases.sh rewrites each pinned FROM line to the digest its tag names today,
and verifier/test/contract.sh shows a failing build's own output. Both are driven against a
stand-in `docker` first on PATH."""

import hashlib
import unittest
from pathlib import Path

from support import VERIFIER, TempDirCase, requires_tools, run, write_executable


def digest_of(ref):
    return hashlib.sha256(ref.encode()).hexdigest()[:64]


@requires_tools("bash", "sha256sum", "cut")
class RefreshBasesTest(TempDirCase):
    script = VERIFIER / "refresh-bases.sh"

    def path_env(self):
        import os

        return {"PATH": "%s:%s" % (self.dir, os.environ["PATH"])}

    def test_rewrites_each_pinned_from_line_to_the_digest_its_tag_names_today(self):
        # a stand-in registry: the digest is a function of the image reference
        write_executable(
            self.dir / "docker",
            "# docker buildx imagetools inspect REF --format '{{.Manifest.Digest}}'\n"
            'ref="$4"\n'
            "printf 'sha256:%s\\n' \"$(printf '%s' \"$ref\" | sha256sum | cut -c1-64)\"\n",
        )
        old = "0" * 64
        dockerfile = self.dir / "Dockerfile"
        dockerfile.write_text(
            "# syntax=docker/dockerfile:1\n"
            "FROM composer:2@sha256:%s AS composer\n"
            "\n"
            "FROM php:8.5-cli-trixie@sha256:%s\n"
            "RUN echo FROM not-a-base@sha256:%s\n"
            "COPY run.sh /usr/local/bin/verifier-run\n" % (old, old, old)
        )

        output, status = run(["bash", self.script, dockerfile], env=self.path_env())
        self.assertEqual(status, 0, output)

        text = dockerfile.read_text()
        self.assertIn("FROM composer:2@sha256:%s AS composer" % digest_of("composer:2"), text)
        self.assertIn("FROM php:8.5-cli-trixie@sha256:%s" % digest_of("php:8.5-cli-trixie"), text)
        self.assertNotIn("FROM composer:2@sha256:%s" % old, text)
        # only FROM lines are touched
        self.assertIn("RUN echo FROM not-a-base@sha256:%s" % old, text)
        self.assertIn("COPY run.sh /usr/local/bin/verifier-run", text)

    def test_leaves_the_file_alone_and_fails_when_a_digest_cannot_be_resolved(self):
        write_executable(self.dir / "docker", "echo 'not a digest'\n")
        old = "1" * 64
        dockerfile = self.dir / "Dockerfile"
        dockerfile.write_text("FROM node:24@sha256:%s\n" % old)

        output, status = run(["bash", self.script, dockerfile], env=self.path_env())
        self.assertNotEqual(status, 0)
        self.assertIn("could not resolve", output)
        self.assertEqual(dockerfile.read_text(), "FROM node:24@sha256:%s\n" % old)

    def test_needs_a_file(self):
        output, status = run(["bash", self.script])
        self.assertEqual(status, 2)
        self.assertIn("usage:", output)


@requires_tools("bash")
class ContractScriptWhenTheImageDoesNotBuildTest(TempDirCase):
    def test_shows_the_builds_own_output_so_a_failing_fix_step_explains_itself_in_the_ci_log(self):
        import os

        # a build that fails the way the fix step does: its message is on stderr, and only a
        # build that is not quiet shows it
        write_executable(
            self.dir / "docker",
            'echo "docker $*" >>"%s/calls.log"\n'
            'if [ "$1" = build ]; then\n'
            "  quiet=\n"
            '  for arg in "$@"; do [ "$arg" = -q ] && quiet=1; done\n'
            '  [ -n "$quiet" ] || echo "#10 1.2 pip 26.3 has no pinned source: review its commit" >&2\n'
            "  exit 1\n"
            "fi\n"
            "exit 99\n" % self.dir,
        )

        output, status = run(
            ["bash", VERIFIER / "test" / "contract.sh", "python"],
            env={"PATH": "%s:%s" % (self.dir, os.environ["PATH"])},
        )

        self.assertNotEqual(status, 0)
        self.assertIn("pip 26.3 has no pinned source: review its commit", output)
        self.assertIn("FAIL: building the python image failed", output)

        calls = (self.dir / "calls.log").read_text()
        self.assertIn("docker build", calls)
        self.assertIn("--progress=plain", calls)
        # nothing ran after the failed build
        self.assertEqual(len([l for l in calls.split("\n") if l]), 1)


@requires_tools("bash", "git", "jq", "python3", "sed", "cut")
class ContractScriptGoToolchainFloorTest(TempDirCase):
    def contract_for_go(self, version_line):
        import os

        # the build succeeds and `go version` answers what the case says; any other
        # `docker run` (the contract itself) exits 99, which ends the script right after the floor
        write_executable(
            self.dir / "docker",
            'echo "docker $*" >>"%s/calls.log"\n'
            'case "$1" in\n'
            "  build) exit 0 ;;\n"
            "  run)\n"
            '    case "$*" in\n'
            '      *"--entrypoint go"*) echo "$GO_VERSION_LINE"; exit 0 ;;\n'
            '      *busybox*) exit 0 ;;\n'
            "    esac\n"
            "    exit 99 ;;\n"
            "esac\n"
            "exit 99\n" % self.dir,
        )
        return run(
            ["bash", VERIFIER / "test" / "contract.sh", "go"],
            env={"PATH": "%s:%s" % (self.dir, os.environ["PATH"]), "GO_VERSION_LINE": version_line},
        )

    def test_refuses_a_go_image_older_than_1_26_9(self):
        for line, release in [
            ("go version go1.26.8 linux/amd64", "go1.26.8"),
            ("go version go1.26 linux/amd64", "go1.26"),
            ("go version go1.25.12 linux/arm64", "go1.25.12"),
        ]:
            with self.subTest(line=line):
                output, status = self.contract_for_go(line)
                self.assertNotEqual(status, 0)
                self.assertIn("FAIL: the go image has %s, below the go1.26.9 floor" % release, output)
                self.assertNotIn("the runner exited non-zero", output)

    def test_accepts_go_1_26_9_and_later_and_goes_on_to_the_contract(self):
        for line in [
            "go version go1.26.9 linux/amd64",
            "go version go1.26.10 linux/arm64",
            "go version go1.27.0 linux/amd64",
            "go version go1.27 linux/amd64",
        ]:
            with self.subTest(line=line):
                output, status = self.contract_for_go(line)
                self.assertNotEqual(status, 0)
                self.assertNotIn("floor", output)
                self.assertIn("FAIL: the runner exited non-zero", output)

    def test_fails_when_go_version_prints_nothing_it_can_read(self):
        output, status = self.contract_for_go("not a go version")
        self.assertNotEqual(status, 0)
        self.assertIn("FAIL: could not read a Go release from: not a go version", output)

    def test_other_runtimes_are_not_asked_for_a_go_version(self):
        import os

        write_executable(
            self.dir / "docker",
            'echo "docker $*" >>"%s/calls.log"\n'
            '[ "$1" = build ] && exit 0\n'
            "exit 99\n" % self.dir,
        )
        run(
            ["bash", VERIFIER / "test" / "contract.sh", "node"],
            env={"PATH": "%s:%s" % (self.dir, os.environ["PATH"])},
        )
        self.assertNotIn("--entrypoint go", (self.dir / "calls.log").read_text())


if __name__ == "__main__":
    unittest.main()
