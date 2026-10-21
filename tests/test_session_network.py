"""The network rule and the per-run proxy credentials, through a local proxy.

The proxy (verifier/test/fake_proxy.py) admits a request only with a credential the fake
platform lists as live AND for a host that credential's kind may reach, exactly the shape of
the hosted proxy. The 'allowlisted host' is a loopback service that only the proxy can name
(`allowed.test`), so a command that does not go through the proxy cannot reach it. Standard
library only."""

import json
import subprocess
import unittest

from session_support import (
    RUN_PATH,
    GitHttpServer,
    SessionCase,
    git,
    have_git_http_backend,
    job,
    runner_tools,
    step,
)

SETUP_CRED = "SETUPCRED"
RUNNER_CRED = "RUNNERCRED"
CHECKS_CRED = "CHECKSCRED"
SIGNATURE = "SIGSECRET"

SECRETS = "SETUPCRED|RUNNERCRED|CHECKSCRED|SIGSECRET"

UNIT = step(1, "unit", "test", "sh test_old.sh")
LINT = step(2, "lint", "check", "echo lint")


def reach(var='"$HTTP_PROXY"', extra=""):
    return "curl -sS -f --max-time 10 %s -x %s http://allowed.test:@ALLOWED@/ | grep -q allowed-ok" % (extra, var)


class NetworkCase(SessionCase):
    def hosted(self, scenario_for):
        """Starts the allowed service, the platform and the proxy; returns
        (platform, proxy, allowed_authority, runner env). `scenario_for(allowed_port)` builds the
        scenario; "@ALLOWED@" in any string becomes the allowed service's port."""
        allowed = self.allowed_service()
        text = json.dumps(scenario_for(allowed.port)).replace("@ALLOWED@", str(allowed.port))
        platform = self.platform(json.loads(text))
        mapping = {"allowed.test": "127.0.0.1", "repo.test": "127.0.0.1"}
        plat = "127.0.0.1:%d" % platform.port
        hosts = {
            "bootstrap": [plat],
            "runner": [plat],
            "setup": ["allowed.test:%d" % allowed.port],
            "checks": ["allowed.test:%d" % allowed.port],
        }
        proxy = self.proxy(platform, hosts, mapping)
        env = {"VERIFIER_PROXY_URL": proxy.url()}
        return platform, proxy, "allowed.test:%d" % allowed.port, env

    def credentials(self, ready=False, user="session:s1", setup_until="setup_done", checks_from="setup_done"):
        creds = [
            {"user": user, "password": SIGNATURE, "kind": "bootstrap"},
            {"user": user, "password": RUNNER_CRED, "kind": "runner"},
            {"user": user, "password": SETUP_CRED, "kind": "setup", "until": setup_until},
        ]
        if ready:
            creds.append({"user": user, "password": CHECKS_CRED, "kind": "checks", "from": checks_from})
        return creds

    def session(self, network, steps, jobs, checks=None, **extra):
        """A hosted session scenario. `checks` defaults to `network`: whether the answer to setup
        done carries the checks credential. The setup credential is live until setup done."""
        checks = network if checks is None else checks
        session = {"setup_done_url": "http://127.0.0.1:@PORT@/sessions/s1/setup_done"}
        session.update(extra.pop("session", {}))

        def build(_allowed):
            return self.session_scenario(
                steps,
                jobs,
                bootstrap={
                    "proxy": {"user": "session:s1", "runner": RUNNER_CRED, "setup": SETUP_CRED},
                    "network": network,
                },
                session=session,
                credentials=self.credentials(ready=checks),
                setup_done_answer={"proxy": {"checks": CHECKS_CRED}} if checks else {},
                **extra,
            )

        return build


@runner_tools
class SessionNetworkTest(NetworkCase):
    def test_job_cannot_reach_allowlisted_host_when_off(self):
        reaching = step(4, "reach", "check", reach())
        platform, proxy, allowed, env = self.hosted(self.session(False, [UNIT, LINT, reaching], [job(1, [reaching])]))
        done = self.run_runner(platform, extra=env)
        self.assertRunnerOk(done)
        self.assertEqual(self.status_of(self.result(1), "reach"), ["fail"])
        self.assertEqual(proxy.seen(host=allowed, kind="checks"), [])
        self.assertEqual(proxy.seen(host=allowed, allowed=True), [])

    def test_job_reaches_it_when_on(self):
        reaching = step(4, "reach", "check", reach())
        tunnel = step(5, "tunnel", "check", reach(extra="--proxytunnel"))
        platform, proxy, allowed, env = self.hosted(
            self.session(True, [UNIT, LINT, reaching, tunnel], [job(1, [reaching, tunnel])])
        )
        done = self.run_runner(platform, extra=env)
        self.assertRunnerOk(done)
        self.assertAllPass(self.result(1))
        admitted = proxy.seen(host=allowed, kind="checks", allowed=True)
        self.assertEqual(sorted(e["method"] for e in admitted), ["CONNECT", "GET"])

    def test_checks_credential_never_reaches_the_platform_host(self):
        probe = step(
            4,
            "platform",
            "check",
            'curl -sS -f --max-time 10 --noproxy x.invalid -x "$HTTP_PROXY" http://127.0.0.1:@PORT@/sessions/s1/next',
        )
        platform, proxy, allowed, env = self.hosted(self.session(True, [UNIT, LINT, probe], [job(1, [probe])]))
        done = self.run_runner(platform, extra=env)
        self.assertRunnerOk(done)
        self.assertEqual(self.status_of(self.result(1), "platform"), ["fail"])
        refused = proxy.seen(kind="checks", host="127.0.0.1:%d" % platform.port)
        self.assertTrue(refused and not refused[0]["allowed"])
        # only the runner's own requests reached the platform's `next`: the job and the 410
        self.assertEqual(len(platform.requests("GET", "/next")), 2)

    def _saved_setup_credential(self, network):
        save = step(4, "save", "setup", 'printf %s "$HTTPS_PROXY" >"$HOME/cred" && ' + reach())
        dead = step(
            5,
            "dead",
            "check",
            'test -s "$HOME/cred" && ! curl -sS -f --max-time 10 -x "$(cat "$HOME/cred")" http://allowed.test:@ALLOWED@/',
        )
        platform, proxy, allowed, env = self.hosted(self.session(network, [save, UNIT, LINT, dead], [job(1, [dead])]))
        done = self.run_runner(platform, extra=env)
        self.assertRunnerOk(done)
        self.assertAllPass(self.ready())
        self.assertAllPass(self.result(1))
        self.assertTrue(proxy.seen(kind="setup", host=allowed, allowed=True), "the setup credential worked during setup")
        refused = [e for e in proxy.seen(host=allowed, allowed=False)]
        self.assertTrue(refused, "and was refused after the ready report")

    def test_saved_setup_credential_is_dead_after_ready(self):
        self._saved_setup_credential(network=False)

    def test_saved_setup_credential_is_dead_after_ready_even_when_checks_may_reach_the_network(self):
        self._saved_setup_credential(network=True)

    def _baseline_with_saved_setup_credential(self, network):
        save = step(4, "save", "setup", 'printf %s "$HTTPS_PROXY" >"$HOME/cred" && ' + reach())
        # A `test` command is a baseline command: it runs after setup and before the ready report.
        stolen = step(
            5,
            "stolen",
            "test",
            'test -s "$HOME/cred" && ! curl -sS -f --max-time 10 -x "$(cat "$HOME/cred")" http://allowed.test:@ALLOWED@/',
        )
        platform, proxy, allowed, env = self.hosted(self.session(network, [save, UNIT, LINT, stolen], []))
        done = self.run_runner(platform, extra=env)
        self.assertRunnerOk(done)
        ready = self.ready()
        self.assertEqual(self.status_of(ready, "baseline", index=5), ["pass"], "the saved credential did not work")
        self.assertTrue(ready["baseline_green"])
        moment = platform.requests("PUT", "/setup_done")[0]["t"]
        used = [e["t"] for e in proxy.seen(host=allowed, kind="setup", allowed=True)]
        self.assertTrue(used and max(used) <= moment, "the setup credential worked during setup only")
        self.assertTrue(proxy.seen(host=allowed, allowed=False), "and was refused when the baseline tried it")

    def test_saved_setup_credential_is_dead_for_the_baseline(self):
        self._baseline_with_saved_setup_credential(network=False)

    def test_saved_setup_credential_is_dead_for_the_baseline_when_checks_may_reach_the_network(self):
        self._baseline_with_saved_setup_credential(network=True)

    def test_baseline_runs_under_the_checks_credential(self):
        online = step(4, "online", "test", reach())
        platform, proxy, allowed, env = self.hosted(self.session(True, [UNIT, LINT, online], []))
        done = self.run_runner(platform, extra=env)
        self.assertRunnerOk(done)
        self.assertEqual(self.status_of(self.ready(), "baseline", index=4), ["pass"])
        moment = platform.requests("PUT", "/setup_done")[0]["t"]
        admitted = proxy.seen(host=allowed, kind="checks", allowed=True)
        self.assertTrue(admitted and min(e["t"] for e in admitted) >= moment)
        self.assertEqual([e for e in proxy.seen(host=allowed, kind="setup") if e["t"] > moment], [])

    def test_baseline_gets_no_proxy_when_off(self):
        probe = step(4, "bare", "test", 'test -z "$HTTP_PROXY$HTTPS_PROXY$http_proxy$https_proxy$NO_PROXY"')
        platform, proxy, allowed, env = self.hosted(self.session(False, [UNIT, LINT, probe], []))
        done = self.run_runner(platform, extra=env)
        self.assertRunnerOk(done)
        self.assertEqual(self.status_of(self.ready(), "baseline", index=4), ["pass"])

    def test_setup_done_comes_after_setup_and_before_the_baseline(self):
        marker = step(0, "mark", "setup", 'echo setup >"$HOME/order"')
        late = step(4, "late", "test", 'echo baseline >>"$HOME/order"')
        platform, proxy, allowed, env = self.hosted(self.session(False, [marker, UNIT, LINT, late], []))
        done = self.run_runner(platform, extra=env)
        self.assertRunnerOk(done)
        self.assertEqual((self.work / "home" / "order").read_text(), "setup\nbaseline\n")
        puts = [e["path"].rsplit("/", 1)[-1] for e in platform.requests("PUT")]
        self.assertEqual(puts[:2], ["setup_done", "ready"])

    def test_a_platform_that_does_not_take_setup_done_gets_no_further_command(self):
        late = step(4, "late", "test", 'echo ran >"$HOME/ran"')
        platform, proxy, allowed, env = self.hosted(
            self.session(False, [UNIT, LINT, late], [job(1, [LINT])], setup_done_status=500)
        )
        done = self.run_runner(platform, extra=env)
        self.assertRunnerOk(done)
        ready = self.ready()
        self.assertIn("end of setup", ready["runner_error"])
        self.assertFalse(ready["baseline_green"])
        self.assertEqual(self.status_of(ready, "baseline"), [])
        self.assertFalse((self.work / "home" / "ran").exists())
        self.assertEqual(platform.requests("GET", "/next"), [])

    def test_hosted_session_without_setup_done_url_is_refused_before_anything_runs(self):
        late = step(4, "late", "test", 'echo ran >"$HOME/ran"')
        scenario = self.session(False, [UNIT, LINT, late], [], session={"setup_done_url": ""})
        platform, proxy, allowed, env = self.hosted(scenario)
        done = self.run_runner(platform, extra=env)
        self.assertRunnerOk(done)
        self.assertIn("setup_done_url", self.ready()["runner_error"])
        self.assertEqual(self.status_of(self.ready(), "clone"), [])
        self.assertFalse((self.work / "home" / "ran").exists())

    def test_no_credential_in_job_environment_when_off(self):
        env_probe = step(
            4,
            "env_off",
            "check",
            'test -z "$(env | grep -i -E \'proxy|%s\')"' % SECRETS,
        )
        platform, proxy, allowed, env = self.hosted(self.session(False, [UNIT, LINT, env_probe], [job(1, [env_probe])]))
        done = self.run_runner(platform, extra=env)
        self.assertRunnerOk(done)
        self.assertAllPass(self.result(1))

    def test_job_environment_when_on_holds_only_the_checks_credential(self):
        env_probe = step(
            4,
            "env_on",
            "check",
            'test -n "$HTTPS_PROXY" && test -z "$(env | grep -E \'SETUPCRED|RUNNERCRED|SIGSECRET\')"'
            ' && env | grep -q CHECKSCRED',
        )
        platform, proxy, allowed, env = self.hosted(self.session(True, [UNIT, LINT, env_probe], [job(1, [env_probe])]))
        done = self.run_runner(platform, extra=env)
        self.assertRunnerOk(done)
        self.assertAllPass(self.result(1))

    def test_setup_commands_get_the_setup_credential_and_loopback_is_not_proxied(self):
        probe = step(
            4,
            "setup_env",
            "setup",
            'test "$HTTPS_PROXY" = "$HTTP_PROXY" && echo "$HTTPS_PROXY" | grep -q "session%3As1:SETUPCRED@127.0.0.1:"'
            ' && test "$NO_PROXY" = "localhost,127.0.0.1,::1" && test "$no_proxy" = "$NO_PROXY"'
            ' && test "$https_proxy" = "$HTTPS_PROXY" && test "$http_proxy" = "$HTTPS_PROXY"',
        )
        platform, proxy, allowed, env = self.hosted(self.session(False, [probe, UNIT, LINT], []))
        done = self.run_runner(platform, extra=env)
        self.assertRunnerOk(done)
        self.assertAllPass(self.ready())

    def test_runner_requests_use_runner_credential(self):
        if not have_git_http_backend():
            self.skipTest("needs git http-backend")

        bare = self.dir / "bare" / "repo.git"
        bare.parent.mkdir()
        subprocess.run(["git", "clone", "-q", "--bare", str(self.origin), str(bare)], check=True)
        git(bare, "config", "uploadpack.allowAnySHA1InWant", "true")
        server = GitHttpServer(bare.parent)
        self.servers.append(server)

        allowed = self.allowed_service()
        scenario = self.session(False, [UNIT, LINT], [job(1, [LINT])])(allowed.port)
        scenario["bootstrap"]["repo_url"] = "http://repo.test:%d/repo.git" % server.port
        platform = self.platform(scenario)
        plat = "127.0.0.1:%d" % platform.port
        proxy = self.proxy(
            platform,
            {
                "bootstrap": [plat],
                "runner": [plat, "repo.test:%d" % server.port],
                "setup": ["allowed.test:%d" % allowed.port],
                "checks": ["allowed.test:%d" % allowed.port],
            },
            {"allowed.test": "127.0.0.1", "repo.test": "127.0.0.1"},
        )
        done = self.run_runner(platform, extra={"VERIFIER_PROXY_URL": proxy.url()})
        self.assertRunnerOk(done)
        self.assertAllPass(self.ready())
        self.assertAllPass(self.result(1))

        bootstrap = proxy.seen(kind="bootstrap")
        self.assertEqual(len(bootstrap), 1)
        self.assertEqual(bootstrap[0]["user"], "session:s1", "the user is derived from the bootstrap URL")
        self.assertEqual(bootstrap[0]["host"], plat)

        clone = proxy.seen(kind="runner", host="repo.test:%d" % server.port)
        self.assertTrue(clone and all(e["allowed"] for e in clone), "the clone went through the proxy")
        on_platform = proxy.seen(kind="runner", host=plat)
        methods = sorted({e["method"] for e in on_platform})
        self.assertEqual(methods, ["GET", "PUT"], "the ready PUT, the next GETs and the result PUT")
        self.assertGreaterEqual(len(on_platform), 4)
        self.assertEqual([e for e in proxy.events if not e["allowed"]], [])
        for event in proxy.events:
            self.assertEqual(event["user"], "session:s1")

    def test_no_secret_or_url_is_an_argument_of_anything_the_runner_starts(self):
        save = step(0, "deps", "setup", "true")
        platform, proxy, allowed, env = self.hosted(self.session(True, [save, UNIT, LINT], [job(1, [LINT])]))
        stubs, log = self.record_argv("env", "curl", "git", "jq", "timeout", "setsid", "base64", "bash")
        env.update(stubs)
        done = self.run_runner(platform, extra=env)
        self.assertRunnerOk(done)
        self.assertAllPass(self.ready())
        self.assertAllPass(self.result(1))
        arguments = log.read_text()
        self.assertIn("-i", arguments.split("\n"), "the stand-ins were used")
        for secret in (SETUP_CRED, RUNNER_CRED, CHECKS_CRED, SIGNATURE, "ghs_session_token", "/sessions/", "/jobs/", "after="):
            self.assertNotIn(secret, arguments)
        self.assertTrue(proxy.seen(kind="runner"), "and the runner did use its secret")

    def test_a_runner_without_a_live_credential_gets_nothing(self):
        platform, proxy, allowed, env = self.hosted(self.session(False, [UNIT, LINT], []))
        platform.scenario["credentials"] = []
        done = self.run_runner(platform, extra=env)
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(platform.requests("GET", "/bootstrap"), [], "the proxy refused the bootstrap fetch")
        self.assertFalse((self.results / "ready.json").exists())


@runner_tools
class RunNetworkTest(NetworkCase):
    def run_scenario_for(self, answer, network=True, with_setup_done=True, dead_after=True):
        save = step(0, "deps", "setup", 'printf %s "$HTTPS_PROXY" >"$HOME/cred" && ' + reach())
        post = step(2, "after", "check", reach() if answer else 'test -z "$HTTP_PROXY$HTTPS_PROXY$http_proxy$https_proxy"')
        dead = step(3, "dead", "check", 'test -s "$HOME/cred" && ! curl -sS -f --max-time 10 -x "$(cat "$HOME/cred")" http://allowed.test:@ALLOWED@/')

        def build(_allowed):
            bootstrap = {
                "repo_url": self.repo_url(),
                "repo_token": "ghs_run_token",
                "base_sha": self.sha,
                "result_url": "http://127.0.0.1:@PORT@/result",
                "wall_clock_seconds": 300,
                "max_output_bytes": 100000,
                "steps": [save, UNIT, post] + ([dead] if dead_after else []),
                "files": [{"path": "calc.sh", "content": "echo 4\n", "test": False}],
                "proxy": {"user": "run:r1", "runner": RUNNER_CRED, "setup": SETUP_CRED},
                "network": network,
            }
            if with_setup_done:
                bootstrap["setup_done_url"] = "http://127.0.0.1:@PORT@/runs/r1/setup_done"
            return {
                "bootstrap": bootstrap,
                "credentials": self.credentials(
                    ready=answer, user="run:r1", setup_until="setup_done", checks_from="setup_done"
                ),
                "setup_done_answer": {"proxy": {"checks": CHECKS_CRED}} if answer else {},
            }

        return build

    def test_run_setup_done_then_checks(self):
        platform, proxy, allowed, env = self.hosted(self.run_scenario_for(answer=True))
        done = self.run_runner(platform, path=RUN_PATH, extra=env)
        self.assertRunnerOk(done)
        result = platform.body("result.json")
        self.assertAllPass(result)
        setup_done = platform.requests("PUT", "/setup_done")
        self.assertEqual(len(setup_done), 1)
        moment = setup_done[0]["t"]
        by_kind = lambda kind: [e["t"] for e in proxy.seen(kind=kind, host=allowed, allowed=True)]
        self.assertTrue(by_kind("setup") and max(by_kind("setup")) <= moment, "setup commands ran before setup_done")
        self.assertTrue(by_kind("checks") and min(by_kind("checks")) >= moment, "post-setup commands after it")
        self.assertTrue(proxy.seen(kind="runner", method="PUT"), "the result went out on the runner credential")
        self.assertEqual(proxy.seen(kind="bootstrap")[0]["user"], "run:r1")

    def test_run_gets_no_proxy_after_setup_when_the_answer_carries_none(self):
        platform, proxy, allowed, env = self.hosted(self.run_scenario_for(answer=False, network=False))
        done = self.run_runner(platform, path=RUN_PATH, extra=env)
        self.assertRunnerOk(done)
        self.assertAllPass(platform.body("result.json"))
        self.assertEqual(proxy.seen(kind="checks"), [])

    def test_run_without_setup_done_url_gets_no_proxy_after_setup(self):
        platform, proxy, allowed, env = self.hosted(
            self.run_scenario_for(answer=False, network=False, with_setup_done=False, dead_after=False)
        )
        done = self.run_runner(platform, path=RUN_PATH, extra=env)
        self.assertRunnerOk(done)
        self.assertAllPass(platform.body("result.json"))
        self.assertEqual(platform.requests("PUT", "/setup_done"), [])

    def test_run_whose_setup_done_is_not_taken_runs_no_further_command(self):
        build = self.run_scenario_for(answer=False, network=False)
        platform, proxy, allowed, env = self.hosted(lambda allowed: {**build(allowed), "setup_done_status": 500})
        done = self.run_runner(platform, path=RUN_PATH, extra=env)
        self.assertRunnerOk(done)
        result = platform.body("result.json")
        self.assertIn("end of setup", result["runner_error"])
        self.assertEqual(self.status_of(result, "baseline"), [])
        self.assertEqual(self.status_of(result, "after"), [])


@runner_tools
class CustomerNetworkTest(NetworkCase):
    PROXY = "http://customer-proxy.invalid:3128"

    def customer(self, network):
        env_is = 'test "$HTTPS_PROXY" = %s && test "$https_proxy" = %s' % (self.PROXY, self.PROXY)
        env_empty = 'test -z "$HTTPS_PROXY$https_proxy$HTTP_PROXY$http_proxy"'
        expected = env_is if network else env_empty
        setup = step(0, "deps", "setup", env_is)
        unit = step(1, "unit", "test", expected)
        check = step(2, "lint", "check", expected)
        platform = self.platform(
            self.session_scenario(
                [setup, unit, check],
                [job(1, [check, unit])],
                bootstrap={"network": network},
            )
        )
        done = self.run_runner(platform, extra={"HTTPS_PROXY": self.PROXY, "https_proxy": self.PROXY})
        self.assertRunnerOk(done)
        self.assertAllPass(self.ready())
        self.assertAllPass(self.result(1))

    def test_customer_mode_network_off(self):
        self.customer(False)

    def test_customer_mode_network_on(self):
        self.customer(True)

    def test_a_session_bootstrap_without_a_network_key_means_off(self):
        env_empty = 'test -z "$HTTPS_PROXY$https_proxy"'
        check = step(2, "lint", "check", env_empty)
        platform = self.platform(self.session_scenario([step(0, "deps", "setup", "true"), UNIT, check], [job(1, [check])]))
        done = self.run_runner(platform, extra={"HTTPS_PROXY": self.PROXY, "https_proxy": self.PROXY})
        self.assertRunnerOk(done)
        self.assertAllPass(self.result(1))

    def _run(self, network_key):
        everywhere = 'test "$HTTPS_PROXY" = %s' % self.PROXY
        nowhere = 'test -z "$HTTPS_PROXY"'
        expected = nowhere if network_key is False else everywhere
        steps = [step(0, "deps", "setup", everywhere), UNIT, step(2, "lint", "check", expected)]
        bootstrap = {
            "repo_url": self.repo_url(),
            "repo_token": "ghs_run_token",
            "base_sha": self.sha,
            "result_url": "http://127.0.0.1:@PORT@/result",
            "wall_clock_seconds": 300,
            "max_output_bytes": 100000,
            "steps": steps,
            "files": [{"path": "calc.sh", "content": "echo 4\n", "test": False}],
        }
        if network_key is not None:
            bootstrap["network"] = network_key
        platform = self.platform({"bootstrap": bootstrap})
        done = self.run_runner(platform, path=RUN_PATH, extra={"HTTPS_PROXY": self.PROXY})
        self.assertRunnerOk(done)
        self.assertAllPass(platform.body("result.json"))

    def test_a_version_2_run_bootstrap_keeps_the_proxy_everywhere(self):
        self._run(None)

    def test_a_run_bootstrap_with_network_false_withholds_it_after_setup(self):
        self._run(False)

    def test_a_run_bootstrap_with_network_true_keeps_it(self):
        self._run(True)


if __name__ == "__main__":
    unittest.main()
