"""verifier/sync-fixtures.sh and the places that use its output: the copy of the platform's static
analysis corpus (rules and positive/negative fixtures) that the contract test runs semgrep over.
The platform repository does not hold the corpus yet, so what is pinned here includes an
absent corpus (a clear message, not a failure), a path that is not a checkout, a corpus that is
not well formed, drift in every direction, and that the CI step is skipped without a path."""

import os
import re
import unittest

from support import VERIFIER, WORKFLOW, TempDirCase, job_blocks, read, requires_tools, run

SCRIPT = VERIFIER / "sync-fixtures.sh"
CORPUS_PATH = "apps/sre_core/priv/code_analysis"


@requires_tools("bash", "diff", "find", "cp", "mv", "mktemp")
class SyncFixturesTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.platform = self.dir / "platform"
        self.platform.mkdir()
        self.dest = self.dir / "copy"

    def source(self):
        return self.platform / CORPUS_PATH

    def put(self, relative, text="x = 1\n", root=None):
        path = (root or self.source()) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def corpus(self, classes=("missing-timeouts", "swallowed-exceptions")):
        for name in classes:
            self.put("rules/%s.yml" % name, "rules:\n  - id: %s\n" % name)
            self.put("fixtures/%s/positive/bad.py" % name, "bad()\n")
            self.put("fixtures/%s/negative/good.py" % name, "good()\n")

    def sync(self, *args, platform=None):
        return run(["bash", SCRIPT, "--dest", self.dest, *args, platform or self.platform])

    def check(self):
        return self.sync("--check")

    def files(self, root):
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())

    def test_copies_the_rules_and_every_fixture_of_the_platforms_corpus(self):
        self.corpus()
        output, status = self.sync()
        self.assertEqual(status, 0, output)
        self.assertIn("copied the platform's corpus (2 rule files, 4 fixtures)", output)
        self.assertEqual(self.files(self.dest), self.files(self.source()))
        self.assertEqual((self.dest / "fixtures/missing-timeouts/positive/bad.py").read_text(), "bad()\n")
        self.assertEqual((self.dest / "rules/swallowed-exceptions.yml").read_text(), "rules:\n  - id: swallowed-exceptions\n")

    def test_a_second_sync_replaces_the_copy_and_leaves_nothing_of_a_removed_class(self):
        self.corpus()
        self.assertEqual(self.sync()[1], 0)
        os.remove(self.source() / "rules/swallowed-exceptions.yml")
        for path in sorted((self.source() / "fixtures/swallowed-exceptions").rglob("*"), reverse=True):
            os.rmdir(path) if path.is_dir() else os.remove(path)
        os.rmdir(self.source() / "fixtures/swallowed-exceptions")
        self.put("fixtures/missing-timeouts/positive/bad.py", "bad2()\n")
        self.assertEqual(self.sync()[1], 0)
        self.assertEqual(self.files(self.dest), self.files(self.source()))
        self.assertFalse((self.dest / "rules/swallowed-exceptions.yml").exists())
        self.assertEqual((self.dest / "fixtures/missing-timeouts/positive/bad.py").read_text(), "bad2()\n")

    def test_check_passes_when_the_copy_is_the_platforms(self):
        self.corpus()
        self.sync()
        output, status = self.check()
        self.assertEqual(status, 0, output)
        self.assertIn("is the platform's", output)

    def test_check_changes_nothing(self):
        self.corpus()
        output, status = self.check()
        self.assertEqual(status, 1, output)
        self.assertFalse(self.dest.exists(), "--check wrote a copy")

    def test_check_names_a_changed_added_and_removed_file_on_either_side(self):
        self.corpus()
        self.sync()

        self.put("fixtures/missing-timeouts/positive/bad.py", "changed()\n")
        output, status = self.check()
        self.assertEqual(status, 1, output)
        self.assertIn("fixtures/missing-timeouts/positive/bad.py differ", output)
        self.assertIn("sync-fixtures.sh", output)
        self.sync()

        self.put("fixtures/missing-timeouts/positive/second.py", "bad()\n")
        output, status = self.check()
        self.assertEqual(status, 1, output)
        self.assertIn("Only in", output)
        self.assertIn("second.py", output)
        self.sync()

        self.put("fixtures/missing-timeouts/negative/extra.py", "fine()\n", root=self.dest)
        output, status = self.check()
        self.assertEqual(status, 1, output)
        self.assertIn("extra.py", output)
        self.sync()

        self.put("rules/missing-timeouts.yml", "rules: []\n")
        output, status = self.check()
        self.assertEqual(status, 1, output)
        self.assertIn("rules/missing-timeouts.yml", output)

    def test_an_absent_corpus_is_said_so_clearly_and_is_not_a_failure(self):
        for args in ([], ["--check"]):
            with self.subTest(args=args):
                output, status = self.sync(*args)
                self.assertEqual(status, 0, output)
                self.assertIn("has no shared fixture corpus yet", output)
                self.assertIn("%s/rules" % self.source(), output)
                self.assertFalse(self.dest.exists())

    def test_a_platform_with_only_one_of_the_two_directories_has_no_corpus_yet(self):
        (self.source() / "rules").mkdir(parents=True)
        output, status = self.sync()
        self.assertEqual(status, 0, output)
        self.assertIn("has no shared fixture corpus yet", output)

    def test_check_fails_when_the_platform_dropped_a_corpus_this_repository_still_holds(self):
        self.corpus()
        self.sync()
        for path in sorted(self.source().rglob("*"), reverse=True):
            os.rmdir(path) if path.is_dir() else os.remove(path)
        os.rmdir(self.source())
        output, status = self.check()
        self.assertEqual(status, 1, output)
        self.assertIn("removed or moved", output)

    def test_a_path_that_is_not_a_directory_is_a_usage_error(self):
        output, status = self.sync(platform=self.dir / "nowhere")
        self.assertEqual(status, 2, output)
        self.assertIn("is not a directory", output)

    def test_usage_errors(self):
        for args in ([], ["--wrong", str(self.platform)], [str(self.platform), str(self.platform)], ["--dest"]):
            with self.subTest(args=args):
                output, status = run(["bash", SCRIPT, *args])
                self.assertEqual(status, 2, output)
                self.assertIn("usage:", output)

    def test_refuses_a_corpus_that_is_not_well_formed_and_copies_nothing(self):
        cases = {
            "a rule without a negative fixture": lambda: os.remove(self.source() / "fixtures/missing-timeouts/negative/good.py"),
            "a rule without a positive fixture": lambda: os.remove(self.source() / "fixtures/missing-timeouts/positive/bad.py"),
            "fixtures without a rule": lambda: self.put("fixtures/orphan/positive/a.py") and self.put("fixtures/orphan/negative/b.py"),
            "a rule with an unplain name": lambda: self.put("rules/Bad_Name.yml"),
            "a rule that is not yml": lambda: self.put("rules/notes.txt"),
            "something besides positive and negative": lambda: self.put("fixtures/missing-timeouts/other/a.py"),
            "a symbolic link": lambda: os.symlink("/etc/passwd", self.source() / "fixtures/missing-timeouts/positive/link.py"),
            "a file over 200 KB": lambda: self.put("fixtures/missing-timeouts/positive/big.py", "x" * 210000),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                self.corpus(classes=("missing-timeouts",))
                try:
                    mutate()
                    output, status = self.sync()
                    self.assertEqual(status, 1, output)
                    self.assertFalse(self.dest.exists(), "nothing is copied from a corpus that is refused")
                    self.assertEqual(self.check()[1], 1)
                finally:
                    self.reset()

    def reset(self):
        import shutil

        shutil.rmtree(self.platform)
        self.platform.mkdir()
        if self.dest.exists():
            shutil.rmtree(self.dest)


class FixtureCorpusWiringTest(unittest.TestCase):
    def test_the_script_is_executable_and_stops_on_error(self):
        text = SCRIPT.read_text()
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        self.assertIn("set -euo pipefail", text)
        self.assertIn(CORPUS_PATH, text)

    def test_the_workflow_runs_the_drift_check_only_when_a_platform_path_is_given(self):
        text = read(WORKFLOW)
        job = job_blocks(text)["unit-tests"]
        self.assertIn("PLATFORM_CORPUS_PATH: ${{ vars.PLATFORM_CORPUS_PATH }}", job)
        step = job.split("name: Shared fixture corpus")[1].split("\n      - ")[0]
        self.assertIn("if: ${{ env.PLATFORM_CORPUS_PATH != '' }}", step)
        self.assertIn('bash verifier/sync-fixtures.sh --check "$PLATFORM_CORPUS_PATH"', step)
        self.assertEqual(text.count("sync-fixtures.sh"), 1)

    def test_the_contract_test_runs_semgrep_over_the_copied_corpus_and_skips_when_there_is_none(self):
        text = read(VERIFIER / "test" / "contract.sh")
        self.assertIn("corpus_cases", text)
        self.assertIn("no shared fixture corpus yet", text)
        body = text.split("corpus_cases() {")[1].split("\n}\n")[0]
        self.assertIn('"fixtures/$class/positive"', body)
        self.assertIn('"^fixtures/$class/negative/"', body)
        self.assertIn("--network none", body)
        self.assertIn(":/corpus:ro", body)

    def test_the_contract_documents_the_corpus_and_how_to_refresh_it(self):
        text = " ".join(read(VERIFIER / "contract.md").split())
        for needle in (
            "verifier/sync-fixtures.sh",
            "apps/sre_core/priv/code_analysis/fixtures/<class>/{positive,negative}/",
            "apps/sre_core/priv/code_analysis/rules/<class>.yml",
            "verifier/test/corpus",
            "PLATFORM_CORPUS_PATH",
        ):
            self.assertIn(needle, text)
        self.assertTrue(re.search(r"public", text))


if __name__ == "__main__":
    unittest.main()
