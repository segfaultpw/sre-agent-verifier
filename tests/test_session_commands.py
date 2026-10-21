"""The runner runs the session's own command text, never a job's.

A session bootstrap carries the profile's commands (the snapshot). A job names commands by
`index` and `kind`; the text the runner executes is the snapshot's, with the placeholders the
contract names expanded from the job's own files, and a job step that also carries a `cmd` must
carry exactly that expansion or the job is refused. Standard library only."""

import unittest

from session_support import SessionCase, job, runner_tools, step

SETUP = step(0, "deps", "setup", "true")
UNIT = step(1, "unit", "test", "sh test_old.sh")
LINT = step(2, "lint", "check", "echo lint")
MARK = step(3, "mark", "check", "touch from_snapshot")
TOUCHED = step(4, "touched", "analysis", "printf '%s\\n' {touched_files} > touched.txt")
TESTS = step(5, "tests_only", "new_test", "printf '%s\\n' {test_files} > tests.txt")
BASE = [SETUP, UNIT, LINT, MARK, TOUCHED, TESTS]


def bare(index, kind):
    return {"index": index, "kind": kind}


@runner_tools
class SessionCommandsTest(SessionCase):
    def run_jobs(self, jobs, steps=None):
        platform, done = self.run_scenario(self.session_scenario(steps or BASE, jobs))
        self.assertRunnerOk(done)
        return platform

    def repo(self, name):
        return self.work / "repo" / name

    def test_a_substituted_command_text_is_refused_and_never_runs(self):
        jobs = [
            job(1, [step(1, "unit", "test", "touch ghost_from_text")]),
            job(2, [step(2, "lint", "check", "echo lint; touch ghost_appended")]),
            job(3, [step(3, "mark", "check", "")]),
            job(4, [LINT]),
        ]
        self.run_jobs(jobs)
        for n in (1, 2, 3):
            result = self.result(n)
            self.assertEqual(result["steps"], [], n)
            self.assertIn("snapshot", result["runner_error"], n)
        self.assertAllPass(self.result(4))
        for name in ("ghost_from_text", "ghost_appended"):
            self.assertFalse(self.repo(name).exists(), name)

    def test_a_job_that_names_no_command_text_runs_the_snapshots(self):
        self.run_jobs([job(1, [bare(3, "check"), bare(2, "check")])])
        result = self.result(1)
        self.assertAllPass(result)
        self.assertEqual([(s["name"], s["index"]) for s in result["steps"]], [("mark", 3), ("lint", 2)])
        self.assertTrue(self.repo("from_snapshot").exists())

    def test_the_text_that_runs_is_the_snapshots_even_when_the_job_names_another_step_name(self):
        named = {"index": 3, "kind": "check", "name": "something_else"}
        self.run_jobs([job(1, [named])])
        result = self.result(1)
        self.assertEqual([(s["name"], s["index"]) for s in result["steps"]], [("mark", 3)])
        self.assertTrue(self.repo("from_snapshot").exists())

    def test_a_command_that_is_not_text_is_refused(self):
        bad = {"index": 3, "kind": "check", "cmd": 7}
        self.run_jobs([job(1, [bad]), job(2, [MARK])])
        self.assertIn("snapshot", self.result(1)["runner_error"])
        self.assertAllPass(self.result(2))

    def test_placeholders_are_expanded_from_the_jobs_files_with_single_quotes(self):
        files = [("a b.txt", "x\n", False), ("it's.txt", "y\n", True), ("plain.txt", "z\n", False), ("x&y.txt", "w\n", False)]
        touched = step(4, "touched", "analysis", "printf '%s\\n' 'a b.txt' 'it'\\''s.txt' 'plain.txt' 'x&y.txt' > touched.txt")
        tests = step(5, "tests_only", "new_test", "printf '%s\\n' 'it'\\''s.txt' > tests.txt")
        self.run_jobs([job(1, [touched, tests], files)])
        result = self.result(1)
        self.assertAllPass(result)
        self.assertEqual(self.repo("touched.txt").read_text(), "a b.txt\nit's.txt\nplain.txt\nx&y.txt\n")
        self.assertEqual(self.repo("tests.txt").read_text(), "it's.txt\n")

    def test_the_expansion_is_computed_by_the_runner_when_the_job_sends_none(self):
        files = [("a b.txt", "x\n", False), ("it's.txt", "y\n", True), ("x&y.txt", "w\n", False)]
        self.run_jobs([job(1, [bare(4, "analysis"), bare(5, "new_test")], files)])
        self.assertAllPass(self.result(1))
        self.assertEqual(self.repo("touched.txt").read_text(), "a b.txt\nit's.txt\nx&y.txt\n")
        self.assertEqual(self.repo("tests.txt").read_text(), "it's.txt\n")

    def test_an_expansion_that_differs_from_the_runners_is_refused(self):
        files = [("a.txt", "x\n", True), ("b.txt", "y\n", False)]
        wrong_quote = step(4, "touched", "analysis", 'printf "%s\\n" "a.txt" "b.txt" > touched.txt')
        wrong_set = step(5, "tests_only", "new_test", "printf '%s\\n' 'a.txt' 'b.txt' > tests.txt")
        wrong_order = step(4, "touched", "analysis", "printf '%s\\n' 'b.txt' 'a.txt' > touched.txt")
        unexpanded = step(4, "touched", "analysis", "printf '%s\\n' {touched_files} > touched.txt")
        jobs = [job(n, [bad], files) for n, bad in enumerate([wrong_quote, wrong_set, wrong_order, unexpanded], 1)]
        self.run_jobs(jobs + [job(5, [bare(4, "analysis")], files)])
        for n in (1, 2, 3, 4):
            self.assertIn("snapshot", self.result(n)["runner_error"], n)
        self.assertAllPass(self.result(5))
        self.assertEqual(self.repo("touched.txt").read_text(), "a.txt\nb.txt\n")

    def test_a_command_that_names_files_the_job_does_not_have_is_skipped(self):
        files = [("a.txt", "x\n", False)]
        self.run_jobs([job(1, [bare(1, "test"), bare(5, "new_test"), bare(4, "analysis")], files)])
        result = self.result(1)
        self.assertEqual(self.status_of(result, "tests_only"), ["skipped"])
        self.assertEqual(self.status_of(result, "unit"), ["pass"])
        self.assertEqual(self.status_of(result, "touched"), ["pass"])
        self.assertFalse(self.repo("tests.txt").exists())
        self.assertEqual(self.repo("touched.txt").read_text(), "a.txt\n")

    def test_rules_dir_is_still_the_runners_to_expand(self):
        rules = step(6, "rules", "analysis", "grep -q sre-x {rules_dir}/a.yml")
        scenario = self.session_scenario(
            BASE + [rules],
            [job(1, [rules]), job(2, [bare(6, "analysis")])],
            bootstrap={"analysis": {"rule_pack": "v1-test", "files": {"a.yml": "rules:\n  - id: sre-x\n"}}},
        )
        platform, done = self.run_scenario(scenario)
        self.assertRunnerOk(done)
        self.assertAllPass(self.result(1))
        self.assertAllPass(self.result(2))

    def test_the_rule_pack_version_placeholder_is_the_bootstraps(self):
        pack = step(6, "pack", "check", "test {rule_pack} = v1-test")
        scenario = self.session_scenario(
            BASE + [pack],
            [job(1, [step(6, "pack", "check", "test v1-test = v1-test")]), job(2, [bare(6, "check")]), job(3, [pack])],
            bootstrap={"analysis": {"rule_pack": "v1-test", "files": {"a.yml": "rules:\n  - id: sre-x\n"}}},
        )
        platform, done = self.run_scenario(scenario)
        self.assertRunnerOk(done)
        self.assertAllPass(self.result(1))
        self.assertAllPass(self.result(2))
        self.assertIn("snapshot", self.result(3)["runner_error"], "an unexpanded placeholder is not the expansion")


if __name__ == "__main__":
    unittest.main()
