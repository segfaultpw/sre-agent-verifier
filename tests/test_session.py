"""Session mode (runner version 3), driven through verifier/run.sh against a loopback platform.

Runs the real runner as an unprivileged process (VERIFIER_ALLOW_UNPRIVILEGED=1), so what it
cannot show is the uid separation and the uid-wide kill; verifier/test/contract.sh proves
both inside the images. Standard library only; skipped when bash, jq, curl or git are
missing (VERIFIER_TESTS_REQUIRE_TOOLS=1 fails instead)."""

import json
import time
import unittest

from session_support import (
    RUN_PATH,
    SessionCase,
    job,
    runner_tools,
    step,
)

SETUP = step(0, "deps", "setup", "mkdir -p cache && echo run >> cache/setup-runs")
UNIT = step(1, "unit", "test", "sh test_old.sh")
LINT = step(2, "lint", "check", "echo lint")
NEWT = step(3, "new_test", "new_test", 'sh test_new.sh')
SNAPSHOT = [SETUP, UNIT, LINT, NEWT]
FILES = [("calc.sh", "echo 4\n", False), ("test_new.sh", 'test "$(sh calc.sh)" = 4\n', True)]


@runner_tools
class SessionTest(SessionCase):
    def test_bootstrap_asks_for_runner_3(self):
        platform, done = self.run_scenario(self.session_scenario(SNAPSHOT, []))
        self.assertRunnerOk(done)
        queries = [e["query"] for e in platform.requests("GET", "/bootstrap")]
        self.assertEqual(queries, ["runner=3"])

    def test_run_bootstrap_asks_for_runner_3_and_is_unchanged(self):
        scenario = {
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
                ],
                "files": [{"path": p, "content": c, "test": t} for p, c, t in FILES],
            }
        }
        platform, done = self.run_scenario(scenario, path=RUN_PATH)
        self.assertRunnerOk(done)
        self.assertEqual([e["query"] for e in platform.requests("GET", "/bootstrap")], ["runner=3"])
        result = platform.body("result.json")
        self.assertEqual(
            [s["name"] for s in result["steps"]],
            ["clone", "deps", "baseline", "new_test_fails_before", "lint", "unit", "new_test_passes_after"],
        )
        self.assertAllPass(result)
        self.assertEqual(sorted(result), ["baseline_green", "runner", "schema", "steps"])
        self.assertEqual(result["runner"], {"runtime": "node", "version": 3})
        self.assertTrue(result["baseline_green"])
        self.assertEqual(platform.requests("PUT", "/ready"), [])

    def test_ready_then_two_jobs(self):
        jobs = [
            job(1, [NEWT, UNIT, LINT], FILES),
            job(2, [LINT, UNIT], [("calc.sh", "echo 4\n", False)]),
        ]
        platform, done = self.run_scenario(self.session_scenario(SNAPSHOT, jobs))
        self.assertRunnerOk(done)

        ready = self.ready()
        self.assertEqual([(s["name"], s.get("index")) for s in ready["steps"]], [("clone", None), ("deps", 0), ("baseline", 1)])
        self.assertAllPass(ready)
        self.assertTrue(ready["baseline_green"])
        self.assertEqual(ready["runner"], {"runtime": "node", "version": 3})

        first = self.result(1)
        self.assertEqual(
            [(s["name"], s["index"]) for s in first["steps"]],
            [("lint", 2), ("unit", 1), ("new_test", 3)],
            "steps run in kind order: check, analysis, test, new_test",
        )
        self.assertAllPass(first)
        self.assertEqual(first["runner"]["version"], 3)
        second = self.result(2)
        self.assertEqual([(s["name"], s["index"]) for s in second["steps"]], [("lint", 2), ("unit", 1)])
        self.assertAllPass(second)

        queries = [e["query"] for e in platform.requests("GET", "/next")]
        self.assertEqual(queries, ["after=0", "after=1", "after=2"])

    def test_ready_and_job_bodies_hold_no_url_or_token(self):
        platform, done = self.run_scenario(self.session_scenario(SNAPSHOT, [job(1, [LINT, UNIT], FILES)]))
        self.assertRunnerOk(done)
        for name in ("ready.json", "result-1.json"):
            text = (self.results / name).read_text()
            for secret in ("ghs_session_token", "127.0.0.1", "SIGSECRET", "http://", "file://"):
                self.assertNotIn(secret, text, name)

    def test_tree_restored_between_jobs(self):
        tamper = step(
            4,
            "tamper",
            "check",
            "git update-index --skip-worktree README && echo tampered > README"
            " && echo stray > stray.txt && echo c > cache/job1-cache"
            " && echo y > staged.txt && git add staged.txt"
            " && git -c user.name=t -c user.email=t@t commit -q -m sneaky && rm calc.sh"
            " && echo '*' >> .git/info/exclude && echo h > hidden.txt"
            " && git config filter.slow.smudge 'sleep 300; cat'",
        )
        restored = step(
            5,
            "restored",
            "check",
            'test "$(cat README)" = original && test -f calc.sh && ! test -e stray.txt'
            " && ! test -e staged.txt && ! test -e hidden.txt && test -e cache/job1-cache"
            " && ! grep -q '^[*]$' .git/info/exclude && ! git config --get filter.slow.smudge"
            ' && test "$(git rev-parse HEAD)" = ' + self.sha
            + ' && test -z "$(git status --porcelain --untracked-files=all)"',
        )
        jobs = [job(1, [tamper], [("calc.sh", "echo 4\n", False)]), job(2, [restored])]
        platform, done = self.run_scenario(self.session_scenario(SNAPSHOT + [tamper, restored], jobs))
        self.assertRunnerOk(done)
        self.assertAllPass(self.result(1))
        self.assertAllPass(self.result(2))
        self.assertEqual((self.work / "repo" / "README").read_text(), "original\n")

    def test_setup_never_reruns(self):
        count = step(4, "count", "check", 'test "$(grep -c run cache/setup-runs)" = 1')
        jobs = [job(n, [count, LINT]) for n in (1, 2, 3)]
        platform, done = self.run_scenario(self.session_scenario(SNAPSHOT + [count], jobs))
        self.assertRunnerOk(done)
        for n in (1, 2, 3):
            self.assertAllPass(self.result(n))
        deps = [s for s in self.ready()["steps"] if s["name"] == "deps"]
        self.assertEqual(len(deps), 1)

    def test_idle_exit(self):
        scenario = self.session_scenario(SNAPSHOT, [], exhausted_status=204, session={"idle_seconds": 2})
        started = time.time()
        platform, done = self.run_scenario(scenario)
        self.assertRunnerOk(done)
        self.assertLess(time.time() - started, 15)
        self.assertGreaterEqual(len(platform.requests("GET", "/next")), 1)
        self.assertEqual([e for e in platform.requests("PUT") if "/jobs/" in e["path"]], [])

    def test_410_exits(self):
        platform, done = self.run_scenario(self.session_scenario(SNAPSHOT, [], exhausted_status=410))
        self.assertRunnerOk(done)
        self.assertEqual(len(platform.requests("GET", "/next")), 1)

    def test_a_job_result_answered_410_exits_cleanly(self):
        scenario = self.session_scenario(SNAPSHOT, [job(1, [LINT]), job(2, [LINT])], result_statuses={"1": 410})
        platform, done = self.run_scenario(scenario)
        self.assertRunnerOk(done)
        self.assertEqual(len(platform.requests("GET", "/next")), 1)

    def test_404_429_5xx_back_off_without_exiting_early(self):
        scenario = self.session_scenario(SNAPSHOT, [job(1, [LINT])], session={"idle_seconds": 600})
        scenario["next"] = [{"status": 404}, {"status": 429}, {"status": 503}] + scenario["next"]
        platform = self.platform(scenario)
        done = self.run_runner(platform, extra=self.stub_sleep())
        self.assertRunnerOk(done)
        self.assertAllPass(self.result(1))
        order = []
        for event in self.events():
            if "sleep" in event:
                order.append("sleep %d" % event["sleep"])
            elif event.get("path", "").endswith("/next"):
                order.append("next")
        self.assertEqual(
            order,
            ["next", "sleep 2", "next", "sleep 4", "next", "sleep 8", "next", "next"],
            "404, 429 and 503 are retried after 2, 4 and 8 seconds, the job runs, then 410 ends it",
        )

    def test_backoff_stops_at_twenty_seconds_and_resets_after_an_answer(self):
        failures = [{"status": 503}] * 5 + [{"status": 204}] + [{"status": 503}] * 2
        scenario = self.session_scenario(SNAPSHOT, [], session={"idle_seconds": 600})
        scenario["next"] = failures
        platform = self.platform(scenario)
        done = self.run_runner(platform, extra=self.stub_sleep())
        self.assertRunnerOk(done)
        sleeps = [e["sleep"] for e in self.events() if "sleep" in e]
        self.assertEqual(sleeps, [2, 4, 8, 20, 20, 2, 4])

    def test_no_poll_past_end_at(self):
        end = self.now() + 6
        from session_support import iso

        scenario = self.session_scenario(
            SNAPSHOT, [], exhausted_status=204, session={"end_at": iso(end), "idle_seconds": 300}
        )
        platform, done = self.run_scenario(scenario)
        self.assertRunnerOk(done)
        polls = platform.requests("GET", "/next")
        self.assertTrue(all(p["t"] < end for p in polls), [p["t"] - end for p in polls])
        self.assertLess(time.time(), end + 10)

    def test_index_outside_snapshot_refused(self):
        jobs = [
            job(1, [step(99, "ghost", "check", "touch ghost")]),
            job(2, [step(1, "unit", "check", "touch ghost")]),
            job(3, [step(0, "deps", "setup", "echo run >> cache/setup-runs")]),
            job(4, [LINT]),
        ]
        platform, done = self.run_scenario(self.session_scenario(SNAPSHOT, jobs))
        self.assertRunnerOk(done)
        for n in (1, 2, 3):
            result = self.result(n)
            self.assertEqual(result["steps"], [], n)
            self.assertIn("snapshot", result["runner_error"], n)
        self.assertAllPass(self.result(4))
        self.assertFalse((self.work / "repo" / "ghost").exists())
        self.assertEqual((self.work / "repo" / "cache" / "setup-runs").read_text(), "run\n")

    def test_job_clock_holds(self):
        hang = step(4, "hang", "check", "sleep 100")
        jobs = [job(1, [hang, LINT]), job(2, [LINT])]
        started = time.time()
        platform, done = self.run_scenario(
            self.session_scenario(SNAPSHOT + [hang], jobs, session={"job_seconds": 2})
        )
        self.assertRunnerOk(done)
        self.assertLess(time.time() - started, 40)
        first = self.result(1)
        self.assertEqual(self.status_of(first, "hang"), ["timeout"])
        self.assertEqual(self.status_of(first, "lint"), ["timeout"], "the clock was used up before the next step")
        self.assertFalse((self.results / "result-2.json").exists(), "a session whose job timed out ends")

    def test_a_path_the_runner_refuses_ends_the_job_not_the_session(self):
        jobs = [
            job(1, [LINT], [("../escape", "x", False)]),
            job(2, [LINT], [(".git/config", "x", False)]),
            job(3, [LINT], [("/abs", "x", False)]),
            job(4, [LINT], [("ok.txt", "x", False)]),
        ]
        platform, done = self.run_scenario(self.session_scenario(SNAPSHOT, jobs))
        self.assertRunnerOk(done)
        for n in (1, 2, 3):
            self.assertIn("path", self.result(n)["runner_error"])
        self.assertAllPass(self.result(4))
        self.assertFalse((self.dir / "escape").exists())

    def test_more_than_twenty_files_refused(self):
        files = [("f%d.txt" % n, "x", False) for n in range(21)]
        platform, done = self.run_scenario(self.session_scenario(SNAPSHOT, [job(1, [LINT], files), job(2, [LINT])]))
        self.assertRunnerOk(done)
        self.assertIn("20", self.result(1)["runner_error"])
        self.assertAllPass(self.result(2))

    def test_setup_failure_is_reported_ready_and_ends_the_session(self):
        bad = step(0, "deps", "setup", "echo nope >&2; exit 3")
        platform, done = self.run_scenario(self.session_scenario([bad, UNIT, LINT], [job(1, [LINT])]))
        self.assertRunnerOk(done)
        ready = self.ready()
        self.assertFalse(ready["baseline_green"])
        self.assertEqual(self.status_of(ready, "deps"), ["fail"])
        self.assertEqual(platform.requests("GET", "/next"), [])

    def test_a_red_baseline_is_reported_and_the_session_goes_on(self):
        red = step(1, "unit", "test", "exit 1")
        green = step(4, "other", "test", "true")
        platform, done = self.run_scenario(
            self.session_scenario([SETUP, red, LINT, NEWT, green], [job(1, [LINT, green])])
        )
        self.assertRunnerOk(done)
        ready = self.ready()
        self.assertFalse(ready["baseline_green"])
        self.assertEqual(
            [(s["name"], s["index"], s["status"]) for s in ready["steps"] if s["name"] == "baseline"],
            [("baseline", 1, "fail"), ("baseline", 4, "pass")],
        )
        self.assertAllPass(self.result(1))

    def test_a_clone_failure_is_reported_on_the_ready_url(self):
        scenario = self.session_scenario(SNAPSHOT, [job(1, [LINT])], bootstrap={"repo_url": "file:///nonexistent"})
        platform, done = self.run_scenario(scenario)
        self.assertRunnerOk(done)
        ready = self.ready()
        self.assertIn("cloned", ready["runner_error"])
        self.assertEqual(self.status_of(ready, "clone"), ["error"])
        self.assertEqual(platform.requests("GET", "/next"), [])

    def test_a_bootstrap_that_is_not_what_the_contract_says_is_reported(self):
        bad = {"session": {"ready_url": "http://127.0.0.1:@PORT@/sessions/s1/ready", "next_url": 5}}
        platform, done = self.run_scenario({"bootstrap": bad})
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertIn("contract", self.ready()["runner_error"])

    def test_no_url_or_credential_on_a_command_line(self):
        scanner = step(
            4,
            "scanner",
            "setup",
            "setsid sh -c 'echo $$ >\"$HOME/scanner.pid\"; n=0; a=127.0.0.; b=1:@PORT@; c=ghs_session; d=_token;"
            " e=SIGSE; f=CRET; g=job; h=s/; i=nex; j=t?after;"
            ' while [ ! -e "$HOME/stop" ] && [ $n -lt 1200 ]; do'
            " for p in /proc/[0-9]*/cmdline; do"
            ' if tr "\\000" " " <$p 2>/dev/null | grep -q -e "$a$b" -e "$c$d" -e "$e$f" -e "$g$h" -e "$i$j"; then'
            ' echo found >>"$HOME/found"; fi; done; n=$((n+1)); sleep 0.05; done\' >/dev/null 2>&1 &'
            " sleep 0.3",
        )
        self.pidfiles.append(self.home / "scanner.pid")
        self.pidfiles.append(self.work / "home" / "scanner.pid")
        final = step(5, "verdict", "check", 'touch "$HOME/stop"; sleep 0.4; test ! -e "$HOME/found"')
        jobs = [job(1, [LINT, UNIT], FILES), job(2, [LINT]), job(3, [final])]
        platform, done = self.run_scenario(self.session_scenario(SNAPSHOT + [scanner, final], jobs))
        self.assertRunnerOk(done)
        self.assertAllPass(self.result(3))
        self.assertTrue((self.work / "home" / "scanner.pid").exists(), "the scanner ran")


if __name__ == "__main__":
    unittest.main()
