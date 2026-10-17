"""Helpers shared by the tests in this directory. Standard library only."""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERIFIER = ROOT / "verifier"
IMAGES = VERIFIER / "images"
WORKFLOW = ROOT / ".github" / "workflows" / "verifier-images.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
CONTRACT = VERIFIER / "contract.md"

RUNTIMES = ["elixir", "node", "python", "go", "java", "php"]


def missing_tools(*tools):
    return [tool for tool in tools if shutil.which(tool) is None]


def requires_tools(*tools):
    """Skips a test case when a tool is missing, unless VERIFIER_TESTS_REQUIRE_TOOLS is set.

    CI sets it, so a runner that lost a tool fails the run instead of skipping the proof.
    """
    missing = missing_tools(*tools)
    if not missing:
        return lambda cls: cls
    if os.environ.get("VERIFIER_TESTS_REQUIRE_TOOLS"):
        raise RuntimeError("these tests need %s on PATH" % ", ".join(missing))
    return unittest.skip("needs %s on PATH" % ", ".join(missing))


def dockerfile(runtime):
    return (IMAGES / runtime / "Dockerfile").read_text()


def read(path):
    return Path(path).read_text()


def write_executable(path, body, shebang="#!/usr/bin/env bash\n"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(shebang + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def run(args, env=None, cwd=None):
    """Runs a command with stderr folded into stdout; returns (output, status)."""
    merged = dict(os.environ)
    merged.update(env or {})
    done = subprocess.run(
        [str(a) for a in args],
        env=merged,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return done.stdout, done.returncode


class TempDirCase(unittest.TestCase):
    """A case with a fresh directory per test, removed afterwards."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="verifier-test-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)


def job_blocks(workflow_text):
    """Splits a workflow into {job id: its text}, by indentation.

    The workflows here are plain block YAML, so a job is a key at two spaces under `jobs:`.
    """
    lines = workflow_text.split("\n")
    start = lines.index("jobs:")
    blocks = {}
    current = None
    for line in lines[start + 1 :]:
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            current = match.group(1)
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return {job: "\n".join(body) for job, body in blocks.items()}

