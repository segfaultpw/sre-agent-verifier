"""Clocks, ordering and refusals of session mode (and the clone bound of a run) that no other
suite pins: the idle limit restarts after a job, a job's `seq` must follow the last, the job
clock never outlasts `end_at`, a hung clone is reported as such, a `next` request is not held
past `end_at`, `base_sha` must be a commit id, and a PUT whose answer was lost is not a reason to
end a session. Runs the real runner against the loopback platform. Standard library only."""

import os
import time
import unittest

from session_support import (
    RUN_PATH,
    HangingServer,
    SessionCase,
    iso,
    job,
    runner_tools,
    step,
)

SETUP = step(0, "deps", "setup", "mkdir -p cache")
UNIT = step(1, "unit", "test", "sh test_old.sh")
LINT = step(2, "lint", "check", "echo lint")
COUNT = step(3, "count", "check", "echo x >> cache/runs")
SNAPSHOT = [SETUP, UNIT, LINT, COUNT]


@runner_tools
class LimitsTest(SessionCase):
    def test_the_idle_limit_restarts_after_every_job(self):
        scenario = self.session_scenario(
            SNAPSHOT, [], session={"idle_seconds": 4}
        )
        slow = lambda n: {"status": 200, "delay_ms": 3000, "job": job(n, [LINT])}
        scenario["next"] = [slow(1), slow(2), {"status": 200, "job": job(3, [LINT])}]
        platform, done = self.run_scenario(scenario)
        self.assertRunnerOk(done)
        for n in (1, 2, 3):
            self.assertAllPass(self.result(n))

    def test_a_job_whose_seq_is_not_after_the_last_one_is_skipped(self):
        scenario = self.session_scenario(
            SNAPSHOT, [job(1, [COUNT]), job(1, [COUNT]), job(2, [COUNT])], session={"idle_seconds": 600}
        )
        platform = self.platform(scenario)
        done = self.run_runner(platform, extra=self.stub_sleep())
        self.assertRunnerOk(done)
        self.assertEqual(len(platform.requests("PUT", "/jobs/1/result")), 1)
        self.assertEqual(len(platform.requests("PUT", "/jobs/2/result")), 1)
        self.assertEqual((self.work / "repo" / "cache" / "runs").read_text(), "x\nx\n", "the repeated job did not run")

    def test_the_job_clock_never_outlasts_end_at(self):
        hang = step(4, "hang", "check", "sleep 100")
        scenario = self.session_scenario(
            SNAPSHOT + [hang],
            [job(1, [hang])],
            session={"job_seconds": 60, "end_at": iso(self.now() + 6)},
        )
        started = time.time()
        platform, done = self.run_scenario(scenario)
        self.assertRunnerOk(done)
        self.assertLess(time.time() - started, 25)
        self.assertEqual(self.status_of(self.result(1), "hang"), ["timeout"])

    def test_a_next_request_is_not_held_past_end_at(self):
        end = self.now() + 9
        scenario = self.session_scenario(
            SNAPSHOT, [], session={"end_at": iso(end), "idle_seconds": 600}
        )
        scenario["next"] = [{"status": 204, "delay_ms": 30000}]
        started = time.time()
        platform, done = self.run_scenario(scenario)
        self.assertRunnerOk(done)
        self.assertLess(time.time(), end + 4, "the runner waited out a 30 second answer past end_at")
        self.assertLess(time.time() - started, 20)

    def test_a_session_clone_that_hangs_is_cut_off_by_the_clock_and_says_so(self):
        server = HangingServer()
        self.servers.append(server)
        scenario = self.session_scenario(
            SNAPSHOT,
            [job(1, [LINT])],
            session={"setup_seconds": 3},
            bootstrap={"repo_url": "http://127.0.0.1:%d/repo.git" % server.port},
        )
        started = time.time()
        platform, done = self.run_scenario(scenario)
        self.assertRunnerOk(done)
        self.assertLess(time.time() - started, 20)
        ready = self.ready()
        clone = [s for s in ready["steps"] if s["name"] == "clone"][0]
        self.assertEqual(clone["status"], "error")
        self.assertIn("cut off by the clock", clone["excerpt"])
        self.assertEqual(platform.requests("GET", "/next"), [])

    def test_a_run_clone_that_hangs_is_cut_off_by_the_clock_and_says_so(self):
        server = HangingServer()
        self.servers.append(server)
        scenario = {
            "bootstrap": {
                "repo_url": "http://127.0.0.1:%d/repo.git" % server.port,
                "repo_token": "ghs_run_token",
                "base_sha": self.sha,
                "result_url": "http://127.0.0.1:@PORT@/result",
                "wall_clock_seconds": 34,
                "max_output_bytes": 100000,
                "steps": [step(0, "unit", "test", "sh test_old.sh")],
                "files": [{"path": "calc.sh", "content": "echo 4\n", "test": False}],
            }
        }
        started = time.time()
        platform, done = self.run_scenario(scenario, path=RUN_PATH)
        self.assertRunnerOk(done)
        self.assertLess(time.time() - started, 25)
        result = platform.body("result.json")
        clone = [s for s in result["steps"] if s["name"] == "clone"][0]
        self.assertIn("cut off by the clock", clone["excerpt"])

    def _refused_sha(self, sha):
        marker = self.dir / "pwned"
        value = sha.replace("@MARKER@", str(marker))
        scenario = self.session_scenario(SNAPSHOT, [job(1, [LINT])], bootstrap={"base_sha": value})
        platform, done = self.run_scenario(scenario)
        self.assertRunnerOk(done)
        ready = self.ready()
        self.assertIn("contract", ready["runner_error"])
        self.assertEqual(self.status_of(ready, "clone"), [])
        self.assertFalse(marker.exists())
        self.assertEqual(platform.requests("GET", "/next"), [])

    def test_a_base_sha_that_is_not_a_commit_id_is_refused_in_a_session(self):
        for value in ("--upload-pack=touch @MARKER@", "-x", "abc123", "HEAD", "G" * 40, self.sha.upper(), self.sha + "0"):
            with self.subTest(value=value):
                self._refused_sha(value)

    def test_a_base_sha_that_is_not_a_commit_id_is_refused_in_a_run(self):
        marker = self.dir / "pwned"
        for value in ("--upload-pack=touch %s" % marker, "-x", "abc123", "HEAD"):
            with self.subTest(value=value):
                scenario = {
                    "bootstrap": {
                        "repo_url": self.repo_url(),
                        "repo_token": "ghs_run_token",
                        "base_sha": value,
                        "result_url": "http://127.0.0.1:@PORT@/result",
                        "wall_clock_seconds": 300,
                        "max_output_bytes": 100000,
                        "steps": [step(0, "unit", "test", "sh test_old.sh")],
                        "files": [{"path": "calc.sh", "content": "echo 4\n", "test": False}],
                    }
                }
                platform, done = self.run_scenario(scenario, path=RUN_PATH)
                self.assertRunnerOk(done)
                result = platform.body("result.json")
                self.assertIn("base_sha", result["runner_error"])
                self.assertEqual(self.status_of(result, "clone"), [])
                self.assertFalse(marker.exists())
                for server in self.servers:
                    server.stop()
                self.servers.clear()

    def test_a_ready_report_whose_answer_was_lost_does_not_end_the_session(self):
        scenario = self.session_scenario(SNAPSHOT, [job(1, [COUNT])], lose_answer=["ready"])
        platform = self.platform(scenario)
        done = self.run_runner(platform, extra=self.stub_sleep())
        self.assertRunnerOk(done)
        self.assertEqual(len(platform.requests("PUT", "/ready")), 2)
        self.assertAllPass(self.result(1))

    def test_a_job_result_whose_answer_was_lost_does_not_end_the_session(self):
        scenario = self.session_scenario(SNAPSHOT, [job(1, [COUNT]), job(2, [COUNT])], lose_answer=["result-1"])
        platform = self.platform(scenario)
        done = self.run_runner(platform, extra=self.stub_sleep())
        self.assertRunnerOk(done)
        self.assertEqual(len(platform.requests("PUT", "/jobs/1/result")), 2)
        self.assertAllPass(self.result(2))
        self.assertEqual((self.work / "repo" / "cache" / "runs").read_text(), "x\nx\n")

    def test_a_409_on_a_first_attempt_is_still_a_refusal(self):
        scenario = self.session_scenario(SNAPSHOT, [job(1, [COUNT])], result_statuses={"1": 409})
        platform = self.platform(scenario)
        done = self.run_runner(platform, extra=self.stub_sleep())
        self.assertEqual(done.returncode, 1, done.stdout[-1500:])
        self.assertEqual(len(platform.requests("PUT", "/jobs/1/result")), 1)


if __name__ == "__main__":
    unittest.main()
