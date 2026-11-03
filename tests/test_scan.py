"""Scan mode (runner version 4), driven through verifier/run.sh against a loopback platform.

The runner is the real one, started as an unprivileged process (VERIFIER_ALLOW_UNPRIVILEGED=1);
semgrep is a stand-in on PATH that records how it was started and answers with JSON of semgrep's
own shape, so what is pinned here is everything the runner decides: what a bootstrap must hold,
the order (clone, end-of-clone message, only then semgrep), what semgrep is given and what it is
not (no token, no proxy variable, no network), what is reported and how it is capped, and that a
scan which changed the tree reports nothing. verifier/test/contract.sh runs the real semgrep
inside every image, as the real unprivileged user. Standard library only."""

import json
import os
import pathlib
import stat
import subprocess

from session_support import (
    RUN_PATH,
    RUNNER,
    SessionCase,
    git,
    runner_tools,
)

STUB = r"""#!/usr/bin/env bash
mkdir -p "$FAKE_DIR"
printf '%s\n' "$@" >"$FAKE_DIR/argv"
env >"$FAKE_DIR/env"
pwd >"$FAKE_DIR/cwd"
if [ -f "$FAKE_RESULTS/setup_done.json" ]; then echo yes >"$FAKE_DIR/after-setup-done"; else echo no >"$FAKE_DIR/after-setup-done"; fi
config=
prev=
for arg in "$@"; do
  if [ "$prev" = --config ]; then config=$arg; fi
  prev=$arg
done
mkdir -p "$FAKE_DIR/rules-seen"
cp "$config"/* "$FAKE_DIR/rules-seen/" 2>/dev/null
echo "a line of semgrep's progress on stderr" >&2
case "${FAKE_MODE:-json}" in
  json) cat "$FAKE_JSON" ;;
  write) echo changed >>README; cat "$FAKE_JSON" ;;
  stray) echo new >stray.txt; cat "$FAKE_JSON" ;;
  ignored) echo cache >cache/new; cat "$FAKE_JSON" ;;
  fail) echo "boom: bad rule" >&2; exit 2 ;;
  refusal) echo '{"results": [], "errors": [{"long_msg": "One of these properties is missing: message"}, {"message": "invalid configuration file found"}]}'; exit 7 ;;
  garbage) echo 'not json' ;;
  sleep) sleep 60 ;;
  big) head -c 70000000 /dev/zero | tr '\0' 'x' ;;
  missing) exit 127 ;;
esac
"""


def semgrep_json(results, scanned=None, errors=()):
    """semgrep's JSON, in the parts the runner reads. `results` is a list of
    (check_id, path, start_line, end_line, message)."""
    return {
        "version": "1.183.0",
        "results": [
            {
                "check_id": check,
                "path": path,
                "start": {"line": start, "col": 1, "offset": 0},
                "end": {"line": end, "col": 2, "offset": 1},
                "extra": {"message": message, "lines": "requires login", "severity": "WARNING", "metadata": {}},
            }
            for check, path, start, end, message in results
        ],
        "errors": list(errors),
        "paths": {"scanned": scanned if scanned is not None else sorted({r[1] for r in results})},
    }


APP = "".join("line %d of app\n" % n for n in range(1, 121))
RULE = "rules:\n  - id: sre-timeouts\n    languages: [python]\n    message: m\n    severity: WARNING\n    pattern: f()\n"


@runner_tools
class ScanCase(SessionCase):
    def setUp(self):
        super().setUp()
        origin = self.origin
        (origin / "src").mkdir()
        (origin / "src" / "app.py").write_text(APP)
        (origin / "src" / "wide.txt").write_text("w" * 5000 + "\n")
        (origin / "src" / "wide4.txt").write_text("\U0001F600" * 5000 + "\n", encoding="utf-8")
        (origin / "src" / "bin.dat").write_bytes(b"a\x00b\xff\xfec\nsecond\n")
        (origin / "src" / "with space.py").write_text("one\ntwo\n")
        os.symlink("/etc/passwd", origin / "link.txt")
        git(origin, "add", "-A")
        git(origin, "commit", "-q", "-m", "files for the scan")
        self.sha = git(origin, "rev-parse", "HEAD")

        self.fake = self.dir / "fake"
        self.fake.mkdir()
        self.stubs = self.dir / "semgrep-bin"
        self.stubs.mkdir()
        stub = self.stubs / "semgrep"
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.json_file = self.dir / "semgrep.json"
        self.step_env = self.dir / "step.env"

    def bootstrap(self, **overrides):
        bootstrap = {
            "mode": "scan",
            "repo_url": self.repo_url(),
            "repo_token": "ghs_scan_token",
            "base_sha": self.sha,
            "result_url": "http://127.0.0.1:@PORT@/result",
            "setup_done_url": "http://127.0.0.1:@PORT@/setup_done",
            "wall_clock_seconds": 120,
            "max_output_bytes": 100000,
            "analysis": {"rule_pack": "v1-scan", "files": {"timeouts.yml": RULE, "swallowed.yml": RULE.replace("timeouts", "swallowed")}},
        }
        bootstrap.update(overrides)
        return {k: v for k, v in bootstrap.items() if v is not None}

    def scan(self, results=(), mode="json", scanned=None, errors=(), bootstrap=None, scenario=None, extra=None, timeout=120, raw=None):
        self.json_file.write_text(json.dumps(raw if raw is not None else semgrep_json(list(results), scanned, errors)))
        self.step_env.write_text(
            "FAKE_DIR=%s\nFAKE_RESULTS=%s\nFAKE_JSON=%s\nFAKE_MODE=%s\n" % (self.fake, self.results, self.json_file, mode)
        )
        scenario = dict(scenario or {})
        scenario["bootstrap"] = bootstrap if bootstrap is not None else self.bootstrap()
        platform = self.platform(scenario)
        env = {
            "PATH": "%s:%s" % (self.stubs, os.environ["PATH"]),
            "VERIFIER_STEP_ENV_FILE": str(self.step_env),
        }
        env.update(extra or {})
        done = self.run_runner(platform, path=RUN_PATH, extra=env, timeout=timeout)
        return platform, done

    def report(self, platform):
        return platform.body("result.json")

    def seen(self, name):
        path = self.fake / name
        return path.read_text() if path.exists() else None


class ScanReportTest(ScanCase):
    def test_bootstrap_asks_for_runner_4_and_the_report_is_the_scan_shape(self):
        platform, done = self.scan([("sre-timeouts", "src/app.py", 3, 3, "no timeout")])
        self.assertRunnerOk(done)
        self.assertEqual([e["query"] for e in platform.requests("GET", "/bootstrap")], ["runner=4"])
        result = self.report(platform)
        self.assertEqual(sorted(result), ["files_scanned", "findings", "runner", "schema", "truncated"])
        self.assertEqual(result["schema"], 1)
        self.assertEqual(result["runner"], {"runtime": "node", "version": 4})
        self.assertEqual(result["files_scanned"], 1)
        self.assertFalse(result["truncated"])
        (finding,) = result["findings"]
        self.assertEqual(
            finding,
            {
                "rule_id": "sre-timeouts",
                "path": "src/app.py",
                "start_line": 3,
                "end_line": 3,
                "text": "line 3 of app\n",
                "message": "no timeout",
            },
        )


    def report_bytes(self):
        return (self.results / "result.json").stat().st_size

    def test_a_report_is_cut_to_fit_the_platform_bound_and_says_so(self):
        # 1000 findings of a full 2000 character text and a full 500 character message are about
        # 2.7 MB: more than the 2,000,000 bytes the platform's result endpoint takes.
        results = [("r", "src/wide.txt", 1, 1, "m" * 900) for _ in range(1000)]
        platform, done = self.scan(results)
        self.assertRunnerOk(done)
        result = self.report(platform)
        self.assertLessEqual(self.report_bytes(), 2_000_000)
        self.assertTrue(result["truncated"])
        self.assertLess(len(result["findings"]), 1000)
        self.assertGreater(len(result["findings"]), 600, "the cut keeps every finding that fits")
        self.assertGreater(self.report_bytes(), 2_000_000 - 4096 - 3000, "and stops at the bound, not far below it")
        self.assertNotIn("runner_error", result)

    def test_the_bound_counts_bytes_not_characters(self):
        # 2000 four-byte characters are 8000 bytes: only about 245 such findings fit.
        results = [("r", "src/wide4.txt", 1, 1, "m") for _ in range(1000)]
        platform, done = self.scan(results)
        self.assertRunnerOk(done)
        result = self.report(platform)
        self.assertLessEqual(self.report_bytes(), 2_000_000)
        self.assertTrue(result["truncated"])
        self.assertTrue(0 < len(result["findings"]) < 400)
        self.assertEqual(result["findings"][0]["text"], "\U0001F600" * 2000)

    def test_a_report_under_the_bound_is_not_cut(self):
        results = [("r", "src/wide.txt", 1, 1, "m" * 900) for _ in range(500)]
        platform, done = self.scan(results)
        self.assertRunnerOk(done)
        result = self.report(platform)
        self.assertLess(self.report_bytes(), 2_000_000 - 4096)
        self.assertEqual(len(result["findings"]), 500)
        self.assertFalse(result["truncated"])

    def test_the_largest_possible_finding_still_fits_many_times(self):
        # A 200 character rule id, a 2000 character text and a 500 character message, the last two
        # of four-byte characters: about 10 KB each. The bound leaves room for a hundred of them.
        results = [("r" * 200, "src/wide4.txt", 1, 1, "\U0001F600" * 900) for _ in range(1000)]
        platform, done = self.scan(results)
        self.assertRunnerOk(done)
        result = self.report(platform)
        self.assertLessEqual(self.report_bytes(), 2_000_000)
        self.assertTrue(result["truncated"])
        self.assertGreater(len(result["findings"]), 100)

    def test_the_report_is_compact_json_so_the_bound_is_what_is_sent(self):
        platform, done = self.scan([("r", "src/app.py", 1, 1, "m")])
        self.assertRunnerOk(done)
        raw = (self.results / "result.json").read_text()
        self.assertEqual(raw.strip().count("\n"), 0, "one line, no indentation")


    SAMPLE_FILES = {
        "src/reports.py": "import requests\n\n\ndef fetch(url):\n    return requests.get(url)\n",
        "src/orders.py": "from shop.models import Order\n\n\ndef export():\n    for order in Order.objects.all():\n        print(order)\n",
        "src/store.py": "def save(record):\n    try:\n        write(record)\n    except Exception:\n        pass\n",
        "src/config.py": 'DB_PASSWORD = "zq81kd7x2mw9v4tb"\n',
        ".github/workflows/ci.yml": (
            "name: ci\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - uses: actions/checkout@v4\n      - run: make\n"
        ),
    }
    SAMPLE_FINDINGS = [
        ("sre-ci-unpinned-action-github-actions", ".github/workflows/ci.yml", 7, 7, "The action is referenced by a tag or branch."),
        ("sre-hardcoded-secret-assigned", "src/config.py", 1, 1, "A credential is written in the source."),
        ("sre-unbounded-query-python", "src/orders.py", 5, 5, "The query has no filter and no limit."),
        ("sre-swallowed-exception-python", "src/store.py", 2, 5, "An error is caught and thrown away."),
        ("sre-missing-timeout-python-requests", "src/reports.py", 5, 5, "The call has no timeout."),
    ]

    def test_the_report_of_a_scan_is_the_sample_the_platform_repository_pins(self):
        # tests/fixtures/scan_runner_report.json is copied into the platform repository
        # (apps/sre_core/test/fixtures/scan_runner_report.json), whose contract test feeds it through
        # the result validator and compares its findings with the platform's own static engine over the
        # same files (SAMPLE_FILES, repeated there). Set VERIFIER_UPDATE_FIXTURES=1 to rewrite it.
        origin = self.origin
        for name, content in self.SAMPLE_FILES.items():
            target = origin / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        git(origin, "add", "-A")
        git(origin, "commit", "-q", "-m", "sample files")
        sha = git(origin, "rev-parse", "HEAD")
        platform, done = self.scan(
            self.SAMPLE_FINDINGS,
            scanned=sorted(self.SAMPLE_FILES),
            bootstrap=self.bootstrap(base_sha=sha),
            extra={"VERIFIER_RUNTIME": "python"},
        )
        self.assertRunnerOk(done)
        report = self.report(platform)
        sample = pathlib.Path(__file__).parent / "fixtures" / "scan_runner_report.json"
        if os.environ.get("VERIFIER_UPDATE_FIXTURES") == "1":
            sample.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        self.assertEqual(report, json.loads(sample.read_text()))
        self.assertEqual(report["runner"], {"runtime": "python", "version": 4})
        self.assertEqual(len(report["findings"]), 5)

    def test_a_scan_with_nothing_found_reports_an_empty_list_and_the_files_it_read(self):
        platform, done = self.scan([], scanned=["src/app.py", "calc.sh", "README"])
        self.assertRunnerOk(done)
        result = self.report(platform)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["files_scanned"], 3)
        self.assertFalse(result["truncated"])
        self.assertNotIn("runner_error", result)

    def test_the_text_is_the_lines_the_finding_covers_read_from_the_tree(self):
        platform, done = self.scan(
            [("r", "src/app.py", 10, 12, "m"), ("r", "src/with space.py", 2, 2, "m")]
        )
        self.assertRunnerOk(done)
        by_path = {f["path"]: f for f in self.report(platform)["findings"]}
        self.assertEqual(by_path["src/app.py"]["text"], "line 10 of app\nline 11 of app\nline 12 of app\n")
        self.assertEqual(by_path["src/with space.py"]["text"], "two\n")

    def test_a_long_finding_is_cut_at_fifty_lines_and_the_text_at_2000_characters(self):
        platform, done = self.scan(
            [("r", "src/app.py", 1, 120, "m"), ("r", "src/wide.txt", 1, 1, "m" * 900)]
        )
        self.assertRunnerOk(done)
        by_path = {f["path"]: f for f in self.report(platform)["findings"]}
        wide = by_path["src/wide.txt"]
        self.assertEqual(wide["text"], "w" * 2000)
        self.assertEqual(wide["message"], "m" * 500)
        app = by_path["src/app.py"]
        self.assertEqual(app["end_line"], 120, "the reported range is semgrep's own")
        self.assertEqual(app["text"].count("\n"), 50)
        self.assertTrue(app["text"].startswith("line 1 of app\nline 2 of app\n"))
        self.assertTrue(app["text"].endswith("line 50 of app\n"))

    def test_nul_bytes_and_invalid_utf8_never_reach_the_report(self):
        platform, done = self.scan([("r", "src/bin.dat", 1, 2, "m")])
        self.assertRunnerOk(done)
        (finding,) = self.report(platform)["findings"]
        self.assertNotIn("\x00", finding["text"])
        self.assertTrue(finding["text"].startswith("ab"))
        self.assertIn("second", finding["text"])
        raw = (self.results / "result.json").read_bytes()
        self.assertNotIn(b"\\u0000", raw)

    def test_a_finding_in_a_symlink_a_missing_file_or_outside_the_tree_is_not_read(self):
        platform, done = self.scan(
            [
                ("r", "link.txt", 1, 1, "through a link"),
                ("r", "src/gone.py", 1, 1, "not a file"),
                ("r", "../outside", 1, 1, "outside"),
                ("r", "/etc/passwd", 1, 1, "absolute"),
                ("r", ".git/config", 1, 1, "the git directory"),
                ("r", "src/app.py", 0, 0, "line zero"),
                ("r", "src/app.py", 5, 4, "backwards"),
            ]
        )
        self.assertRunnerOk(done)
        findings = {f["path"]: f for f in self.report(platform)["findings"]}
        self.assertEqual(sorted(findings), ["link.txt", "src/gone.py"])
        self.assertEqual(findings["link.txt"]["text"], "", "a symbolic link is never opened")
        self.assertEqual(findings["src/gone.py"]["text"], "")
        self.assertNotIn("root:", json.dumps(findings))

    def test_findings_are_ordered_by_path_line_and_rule(self):
        platform, done = self.scan(
            [
                ("b", "src/app.py", 9, 9, "m"),
                ("a", "src/app.py", 9, 9, "m"),
                ("a", "calc.sh", 1, 1, "m"),
                ("a", "./src/app.py", 2, 2, "m"),
            ]
        )
        self.assertRunnerOk(done)
        order = [(f["path"], f["start_line"], f["rule_id"]) for f in self.report(platform)["findings"]]
        self.assertEqual(
            order,
            [("calc.sh", 1, "a"), ("src/app.py", 2, "a"), ("src/app.py", 9, "a"), ("src/app.py", 9, "b")],
        )

    def test_more_than_a_thousand_findings_are_cut_and_marked_truncated(self):
        results = [("r", "src/app.py", 1 + n % 100, 1 + n % 100, "m%d" % n) for n in range(1200)]
        platform, done = self.scan(results)
        self.assertRunnerOk(done)
        result = self.report(platform)
        self.assertEqual(len(result["findings"]), 1000)
        self.assertTrue(result["truncated"])

    def test_exactly_a_thousand_findings_are_not_truncated(self):
        results = [("r", "src/app.py", 1 + n % 100, 1 + n % 100, "m%d" % n) for n in range(1000)]
        platform, done = self.scan(results)
        self.assertRunnerOk(done)
        result = self.report(platform)
        self.assertEqual(len(result["findings"]), 1000)
        self.assertFalse(result["truncated"])

    def test_a_file_semgrep_gave_up_on_marks_the_result_truncated(self):
        errors = [{"code": 3, "level": "warn", "type": ["Timeout", {"rule_id": "sre-timeouts"}], "message": "timed out"}]
        platform, done = self.scan([("r", "src/app.py", 1, 1, "m")], errors=errors)
        self.assertRunnerOk(done)
        result = self.report(platform)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["findings"]), 1)

    def test_other_semgrep_errors_do_not(self):
        errors = [{"code": 3, "level": "warn", "type": "PartialParsing", "message": "partial"}]
        platform, done = self.scan([("r", "src/app.py", 1, 1, "m")], errors=errors)
        self.assertRunnerOk(done)
        self.assertFalse(self.report(platform)["truncated"])

    def test_the_report_holds_no_token_url_or_path_of_the_runner(self):
        platform, done = self.scan([("r", "src/app.py", 1, 1, "m")])
        self.assertRunnerOk(done)
        text = (self.results / "result.json").read_text()
        for secret in ("ghs_scan_token", "127.0.0.1", "SIGSECRET", "file://", str(self.work), "/rules"):
            self.assertNotIn(secret, text)


class ScanSemgrepTest(ScanCase):
    def test_semgrep_is_started_with_the_pinned_flags_over_the_tree_and_the_rule_pack(self):
        platform, done = self.scan([("r", "src/app.py", 1, 1, "m")])
        self.assertRunnerOk(done)
        argv = self.seen("argv").splitlines()
        self.assertEqual(argv[0], "scan")
        for flag in (
            "--json",
            "--quiet",
            "--metrics=off",
            "--disable-version-check",
            "--oss-only",
            "--no-rewrite-rule-ids",
        ):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--timeout") + 1], "30")
        self.assertEqual(argv[argv.index("--max-target-bytes") + 1], "1000000")
        self.assertEqual(argv[argv.index("--config") + 1], str(self.work / "rules"))
        self.assertEqual(argv[-1], ".")
        self.assertNotIn("--autofix", argv)
        self.assertNotIn("--config=auto", argv)
        self.assertEqual(self.seen("cwd").strip(), str(self.work / "repo"))

    def test_the_rule_pack_is_what_the_bootstrap_carried(self):
        platform, done = self.scan([])
        self.assertRunnerOk(done)
        seen = sorted(p.name for p in (self.fake / "rules-seen").iterdir())
        self.assertEqual(seen, ["swallowed.yml", "timeouts.yml"])
        self.assertEqual((self.fake / "rules-seen" / "timeouts.yml").read_text(), RULE)

    def test_the_end_of_clone_message_comes_first_and_the_token_is_dead_by_then(self):
        platform, done = self.scan([])
        self.assertRunnerOk(done)
        self.assertEqual(self.seen("after-setup-done").strip(), "yes")
        puts = [e["path"].rsplit("/", 1)[-1] for e in platform.requests("PUT")]
        self.assertEqual(puts, ["setup_done", "result"])

    def test_semgrep_gets_no_token_no_url_no_proxy_and_the_rebuilt_environment(self):
        platform, done = self.scan(
            [],
            scenario={"setup_done_answer": {"proxy": {"checks": "CHECKSCRED"}}},
            extra={
                "HTTP_PROXY": "http://proxy.invalid:3128",
                "HTTPS_PROXY": "http://proxy.invalid:3128",
                "NO_PROXY": "127.0.0.1,localhost",
                "AWS_SECRET_ACCESS_KEY": "leak-aws",
            },
        )
        self.assertRunnerOk(done)
        env = self.seen("env")
        for secret in ("ghs_scan_token", "127.0.0.1", "SIGSECRET", "CHECKSCRED", "leak-aws", "BOOTSTRAP_URL", "REPO_TOKEN"):
            self.assertNotIn(secret, env)
        lowered = env.lower()
        self.assertNotIn("proxy", lowered, "a scan has no network, whatever the task definition says")
        self.assertIn("CI=true\n", env)
        self.assertIn("GIT_CONFIG_NOSYSTEM=1\n", env)
        self.assertIn("GIT_CONFIG_GLOBAL=/dev/null\n", env)
        self.assertIn("GIT_CONFIG_KEY_0=core.hooksPath\n", env)
        self.assertIn("GIT_CONFIG_KEY_1=core.fsmonitor\n", env)

    def test_neither_the_token_nor_a_url_is_on_the_command_line_of_curl_or_git(self):
        env, log = self.record_argv("curl", "git")
        platform, done = self.scan([], extra=env)
        self.assertRunnerOk(done)
        argv = log.read_text()
        for secret in ("ghs_scan_token", "SIGSECRET", "127.0.0.1", "http://", "Authorization"):
            self.assertNotIn(secret, argv)

    def test_a_platform_that_does_not_take_the_end_of_clone_gets_no_scan(self):
        platform, done = self.scan([("r", "src/app.py", 1, 1, "m")], scenario={"setup_done_status": 403})
        self.assertRunnerOk(done)
        result = self.report(platform)
        self.assertIn("end of the clone", result["runner_error"])
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["files_scanned"], 0)
        self.assertIsNone(self.seen("argv"), "semgrep must not start when the token may still be live")

    def test_a_bootstrap_with_steps_and_files_runs_none_of_them(self):
        marker = self.dir / "ran"
        bootstrap = self.bootstrap(
            steps=[
                {"index": 0, "name": "setup", "kind": "setup", "cmd": "touch %s" % marker},
                {"index": 1, "name": "tests", "kind": "test", "cmd": "touch %s" % marker},
                {"index": 2, "name": "lint", "kind": "analysis", "cmd": "touch %s" % marker},
            ],
            files=[{"path": "calc.sh", "content": "echo hacked\n", "test": False}],
        )
        platform, done = self.scan([("r", "calc.sh", 1, 1, "m")], bootstrap=bootstrap)
        self.assertRunnerOk(done)
        self.assertFalse(marker.exists())
        (finding,) = self.report(platform)["findings"]
        self.assertEqual(finding["text"], "echo 5\n", "the patch of a run is never written in a scan")

    def test_nothing_is_written_to_the_tree_by_the_runner(self):
        platform, done = self.scan([("r", "src/app.py", 1, 1, "m")])
        self.assertRunnerOk(done)
        self.assertEqual(git(self.work / "repo", "status", "--porcelain", "--untracked-files=all", "--ignored"), "")
        self.assertEqual(git(self.work / "repo", "rev-parse", "HEAD"), self.sha)


class ScanTreeIntegrityTest(ScanCase):
    def assertTreeChangeRefused(self, mode):
        platform, done = self.scan([("r", "src/app.py", 1, 1, "m")], mode=mode)
        self.assertRunnerOk(done)
        result = self.report(platform)
        self.assertIn("changed the work tree", result["runner_error"])
        self.assertEqual(result["findings"], [])

    def test_a_scan_that_edits_a_tracked_file_reports_nothing(self):
        self.assertTreeChangeRefused("write")

    def test_a_scan_that_adds_a_file_reports_nothing(self):
        self.assertTreeChangeRefused("stray")

    def test_a_scan_that_adds_an_ignored_file_reports_nothing(self):
        (self.origin / "cache").mkdir(exist_ok=True)
        (self.origin / "cache" / "keep").write_text("x\n")
        git(self.origin, "add", "-f", "cache/keep")
        git(self.origin, "commit", "-q", "-m", "a tracked file in an ignored directory")
        self.sha = git(self.origin, "rev-parse", "HEAD")
        self.assertTreeChangeRefused("ignored")


class ScanFailureTest(ScanCase):
    def assertFailed(self, platform, done, text):
        self.assertRunnerOk(done)
        result = self.report(platform)
        self.assertIn(text, result["runner_error"])
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["files_scanned"], 0)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["runner"]["version"], 4)
        return result

    def test_semgrep_not_installed_is_said_so_and_is_no_finding(self):
        platform, done = self.scan([], mode="missing")
        self.assertFailed(platform, done, "semgrep is not installed in this image")

    def test_semgrep_failing_reports_its_standard_error_and_status(self):
        platform, done = self.scan([], mode="fail")
        result = self.assertFailed(platform, done, "semgrep failed (exit 2)")
        self.assertIn("boom: bad rule", result["runner_error"])

    def test_a_rule_pack_semgrep_refuses_is_explained_from_the_errors_in_its_json(self):
        platform, done = self.scan([], mode="refusal")
        result = self.assertFailed(platform, done, "semgrep failed (exit 7)")
        self.assertIn("One of these properties is missing: message; invalid configuration file found", result["runner_error"])
        self.assertIn("progress on stderr", result["runner_error"])

    def test_output_that_is_not_semgrep_json_is_not_a_clean_scan(self):
        platform, done = self.scan([], mode="garbage")
        self.assertFailed(platform, done, "could not be read")

    def test_json_without_results_is_not_a_clean_scan(self):
        platform, done = self.scan(raw={"version": "1", "errors": []})
        self.assertFailed(platform, done, "could not be read")

    def test_a_scan_the_clock_cuts_off_is_a_runner_error_not_an_empty_scan(self):
        platform, done = self.scan([], mode="sleep", bootstrap=self.bootstrap(wall_clock_seconds=36))
        self.assertFailed(platform, done, "ran out of time")

    def test_output_over_the_cap_is_a_runner_error_not_a_truncated_json(self):
        platform, done = self.scan([], mode="big")
        self.assertFailed(platform, done, "over the limit")

    def test_a_repository_that_cannot_be_cloned_stops_before_the_end_of_clone_message(self):
        platform, done = self.scan([], bootstrap=self.bootstrap(base_sha="0" * 40))
        self.assertFailed(platform, done, "could not be cloned")
        self.assertEqual(platform.requests("PUT", "/setup_done"), [])
        self.assertIsNone(self.seen("argv"))


class ScanBootstrapTest(ScanCase):
    def assertRefused(self, bootstrap, text="not what the contract says"):
        platform, done = self.scan([], bootstrap=bootstrap)
        self.assertRunnerOk(done)
        result = self.report(platform)
        self.assertIn(text, result["runner_error"])
        self.assertEqual(result["findings"], [])
        self.assertEqual(platform.requests("PUT", "/setup_done"), [], "nothing is cloned for a bootstrap that is refused")
        self.assertFalse((self.work / "repo").exists())
        self.assertIsNone(self.seen("argv"))

    def test_a_scan_names_a_commit(self):
        self.assertRefused(self.bootstrap(base_sha=None))

    def test_a_commit_is_a_full_lower_case_hash_never_an_option_or_a_branch(self):
        for value in ("main", "--upload-pack=x", "ABCDEF" * 6 + "ABCD", "abc123", self.sha[:39], self.sha + "0"):
            with self.subTest(value=value):
                self.assertRefused(self.bootstrap(base_sha=value))

    def test_a_scan_names_the_end_of_clone_url(self):
        self.assertRefused(self.bootstrap(setup_done_url=None))
        self.assertRefused(self.bootstrap(setup_done_url=""))

    def test_a_scan_names_a_repository(self):
        self.assertRefused(self.bootstrap(repo_url=None))

    def test_a_scan_carries_a_rule_pack(self):
        self.assertRefused(self.bootstrap(analysis=None))
        self.assertRefused(self.bootstrap(analysis={"rule_pack": "v1", "files": {}}))
        self.assertRefused(self.bootstrap(analysis={"rule_pack": "v1"}))
        self.assertRefused(self.bootstrap(analysis="rules"))

    def test_the_rule_pack_is_held_to_the_run_mode_rules(self):
        self.assertRefused(self.bootstrap(analysis={"rule_pack": "v1", "files": {"../evil.yml": RULE}}), "rule pack")
        self.assertRefused(self.bootstrap(analysis={"rule_pack": "v1", "files": {"UPPER.yml": RULE}}), "rule pack")
        self.assertRefused(self.bootstrap(analysis={"rule_pack": "v1", "files": {"a.yml": "x" * 20001}}), "rule pack")
        self.assertRefused(
            self.bootstrap(analysis={"rule_pack": "v1", "files": {"r%d.yml" % n: RULE for n in range(21)}}), "rule pack"
        )

    def test_a_bootstrap_without_a_result_url_cannot_be_reported_and_exits_1(self):
        platform, done = self.scan([], bootstrap=self.bootstrap(result_url=None))
        self.assertEqual(done.returncode, 1, done.stdout[-1500:])
        self.assertEqual(platform.requests("PUT"), [])

    def test_an_unknown_mode_is_a_run_bootstrap_as_before(self):
        platform, done = self.scan([], bootstrap=self.bootstrap(mode="verify"))
        self.assertEqual(done.returncode, 1, "a run bootstrap needs steps and files: nothing can be reported")
        self.assertEqual(platform.requests("PUT"), [])


class ScanHostedProxyTest(ScanCase):
    """The hosted shape: the runner reaches the platform through the egress proxy with its own
    credential; nothing else does, and semgrep has none."""

    def hosted(self, bootstrap_proxy):
        scenario = {
            "bootstrap": self.bootstrap(proxy=bootstrap_proxy),
            "credentials": [
                {"user": "run:r1", "password": "SIGSECRET", "kind": "bootstrap"},
                {"user": "run:r1", "password": "RUNNERCRED", "kind": "runner"},
                {"user": "run:r1", "password": "SETUPCRED", "kind": "setup"},
            ],
        }
        self.json_file.write_text(json.dumps(semgrep_json([("r", "src/app.py", 1, 1, "m")])))
        self.step_env.write_text(
            "FAKE_DIR=%s\nFAKE_RESULTS=%s\nFAKE_JSON=%s\nFAKE_MODE=json\n" % (self.fake, self.results, self.json_file)
        )
        platform = self.platform(scenario)
        plat = "127.0.0.1:%d" % platform.port
        proxy = self.proxy(platform, {"bootstrap": [plat], "runner": [plat], "setup": ["allowed.test:1"]})
        env = {
            "PATH": "%s:%s" % (self.stubs, os.environ["PATH"]),
            "VERIFIER_STEP_ENV_FILE": str(self.step_env),
            "VERIFIER_PROXY_URL": proxy.url(),
        }
        done = self.run_runner(platform, path=RUN_PATH, extra=env)
        return platform, proxy, done

    def test_the_platform_is_reached_through_the_proxy_with_the_runner_credential_only(self):
        platform, proxy, done = self.hosted({"user": "run:r1", "runner": "RUNNERCRED"})
        self.assertRunnerOk(done)
        self.assertEqual(len(self.report(platform)["findings"]), 1)
        kinds = {e["kind"] for e in proxy.seen(allowed=True)}
        self.assertEqual(kinds, {"bootstrap", "runner"})
        self.assertEqual([e["method"] for e in proxy.seen(kind="runner", allowed=True)], ["PUT", "PUT"])
        env = self.seen("env")
        self.assertNotIn("proxy", env.lower())
        for secret in ("RUNNERCRED", "SETUPCRED", "SIGSECRET"):
            self.assertNotIn(secret, env)

    def test_a_setup_secret_in_a_scan_bootstrap_is_never_used(self):
        platform, proxy, done = self.hosted({"user": "run:r1", "runner": "RUNNERCRED", "setup": "SETUPCRED"})
        self.assertRunnerOk(done)
        self.assertEqual(proxy.seen(kind="setup"), [])
        env = self.seen("env")
        self.assertNotIn("SETUPCRED", env)
        self.assertNotIn("proxy", env.lower())

    def test_a_hosted_scan_without_proxy_credentials_is_refused_before_anything_is_fetched(self):
        platform, proxy, done = self.hosted(None)
        self.assertRunnerOk(done)
        self.assertIn("no proxy credentials", self.report(platform)["runner_error"])
        self.assertIsNone(self.seen("argv"))
        self.assertEqual(platform.requests("PUT", "/setup_done"), [])
        self.assertFalse((self.work / "repo").exists())


class ScanUnprivilegedRunnerTest(ScanCase):
    def test_a_runner_that_is_not_root_and_not_allowed_to_say_so_runs_nothing(self):
        if os.geteuid() == 0:
            self.skipTest("this case needs a runner that is not root")
        self.json_file.write_text(json.dumps(semgrep_json([])))
        self.step_env.write_text("FAKE_DIR=%s\nFAKE_RESULTS=%s\nFAKE_JSON=%s\n" % (self.fake, self.results, self.json_file))
        platform = self.platform({"bootstrap": self.bootstrap()})
        env = self.runner_env(platform, RUN_PATH, {"PATH": "%s:%s" % (self.stubs, os.environ["PATH"])})
        del env["VERIFIER_ALLOW_UNPRIVILEGED"]
        done = subprocess.run(
            ["bash", str(RUNNER)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stdout[-1500:])
        result = platform.body("result.json")
        self.assertIn("not started as root", result["runner_error"])
        self.assertIsNone(self.seen("argv"))
        self.assertEqual(platform.requests("PUT", "/setup_done"), [])


if __name__ == "__main__":
    import unittest

    unittest.main()
