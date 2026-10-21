"""What the session and network tests share: a fixture repository, the loopback platform and
proxy of verifier/test, a tiny git-over-HTTP server and a way to run verifier/run.sh as an
unprivileged process against them. Standard library only."""

import datetime
import http.server
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import unittest
import urllib.parse

from support import VERIFIER, TempDirCase, missing_tools, requires_tools

sys.path.insert(0, str(VERIFIER / "test"))
from fake_platform import Platform  # noqa: E402
from fake_proxy import Proxy  # noqa: E402

RUNNER = VERIFIER / "run.sh"
TOOLS = ("bash", "jq", "curl", "git", "setsid", "mkfifo", "realpath", "base64")
runner_tools = requires_tools(*TOOLS)

SESSION_PATH = "/webhooks/verifier/sessions/s1/SIGSECRET/bootstrap"
RUN_PATH = "/webhooks/verifier/r1/SIGSECRET/bootstrap"

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def git(repo, *args, check=True):
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        env={**os.environ, **GIT_ENV},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if check and done.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), done.stdout))
    return done.stdout.strip()


def make_origin(path):
    """A repository with one commit: calc.sh (a bug), a test that does not notice it, a README
    and an ignored cache directory."""
    path.mkdir(parents=True)
    (path / "calc.sh").write_text("echo 5\n")
    (path / "test_old.sh").write_text("test -f calc.sh\n")
    (path / "README").write_text("original\n")
    (path / ".gitignore").write_text("cache/\n")
    git(path, "init", "-q", "-b", "main")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "base")
    return path, git(path, "rev-parse", "HEAD")


def step(index, name, kind, cmd):
    return {"index": index, "name": name, "kind": kind, "cmd": cmd}


def job(seq, steps, files=(), result_path=None):
    return {
        "seq": seq,
        "files": [{"path": p, "content": c, "test": t} for p, c, t in files],
        "steps": steps,
        "result_url": "http://127.0.0.1:@PORT@%s" % (result_path or "/jobs/%d/result" % seq),
    }


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AllowedService:
    """The 'allowlisted host': answers `allowed-ok` to anything."""

    def __init__(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"allowed-ok"
                self.send_response(200)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self._server = Server(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


class GitHttpServer:
    """git over HTTP for one directory of bare repositories, through `git http-backend`."""

    def __init__(self, root):
        env_root = str(root)

        class Handler(http.server.BaseHTTPRequestHandler):
            def _serve(self):
                parts = urllib.parse.urlsplit(self.path)
                length = int(self.headers.get("content-length", "0"))
                body = self.rfile.read(length) if length else b""
                env = {
                    "PATH": os.environ["PATH"],
                    "GIT_PROJECT_ROOT": env_root,
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "PATH_INFO": parts.path,
                    "QUERY_STRING": parts.query,
                    "REQUEST_METHOD": self.command,
                    "CONTENT_TYPE": self.headers.get("content-type", ""),
                    "CONTENT_LENGTH": str(len(body)),
                    "REMOTE_ADDR": "127.0.0.1",
                    **GIT_ENV,
                }
                done = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True)
                head, _, payload = done.stdout.partition(b"\r\n\r\n")
                status, headers = 200, []
                for line in head.decode("latin-1").split("\r\n"):
                    key, _, value = line.partition(":")
                    if key.lower() == "status":
                        status = int(value.strip().split()[0])
                    elif key:
                        headers.append((key, value.strip()))
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _serve

            def log_message(self, *_args):
                pass

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        self._server = Server(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


class HangingServer:
    """Accepts connections and never answers: a repository server that has hung."""

    def __init__(self):
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self._held = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self._held.append(conn)

    def stop(self):
        self._sock.close()
        for conn in self._held:
            conn.close()


def have_git_http_backend():
    if shutil.which("git") is None:
        return False
    exec_path = subprocess.run(["git", "--exec-path"], capture_output=True, text=True).stdout.strip()
    return os.path.exists(os.path.join(exec_path, "git-http-backend"))


class SessionCase(TempDirCase):
    """One fixture repository, a results directory and a way to start the runner."""

    def setUp(self):
        super().setUp()
        self.origin, self.sha = make_origin(self.dir / "origin")
        self.results = self.dir / "results"
        self.results.mkdir()
        self.work = self.dir / "work"
        self.home = self.dir / "home"
        self.home.mkdir()
        self.events_log = self.dir / "events.jsonl"
        self.servers = []
        self.pidfiles = []
        self.addCleanup(self._stop)

    def _stop(self):
        # A daemon a test's setup command started (the runner is not root here, so nothing
        # reaps a process with a session of its own): ended by the pid it wrote, and only
        # while that pid is still the process that wrote it.
        for pidfile in self.pidfiles:
            try:
                pid = int(pidfile.read_text().strip())
                with open("/proc/%d/cmdline" % pid, "rb") as handle:
                    if b"scanner.pid" in handle.read():
                        os.kill(pid, 15)
            except (OSError, ValueError):
                pass
        for server in reversed(self.servers):
            server.stop()

    def repo_url(self):
        return "file://%s" % self.origin

    def platform(self, scenario):
        platform = Platform(scenario, results_dir=str(self.results), log_path=str(self.events_log)).start()
        self.servers.append(platform)
        return platform

    def proxy(self, platform, hosts, mapping=None):
        proxy = Proxy(platform.url("/authorize"), hosts, mapping or {}).start()
        self.servers.append(proxy)
        return proxy

    def allowed_service(self):
        service = AllowedService()
        self.servers.append(service)
        return service

    def session_scenario(self, steps, jobs, **extra):
        session = {
            "ready_url": "http://127.0.0.1:@PORT@/sessions/s1/ready",
            "next_url": "http://127.0.0.1:@PORT@/sessions/s1/next",
            "end_at": iso(self.now() + 120),
            "idle_seconds": 30,
            "job_seconds": 20,
            "setup_seconds": 60,
        }
        session.update(extra.pop("session", {}))
        bootstrap = {
            "session": session,
            "repo_url": self.repo_url(),
            "repo_token": "ghs_session_token",
            "base_sha": self.sha,
            "steps": steps,
        }
        bootstrap.update(extra.pop("bootstrap", {}))
        scenario = {"bootstrap": bootstrap, "next": [{"status": 200, "job": j} for j in jobs]}
        scenario.update(extra)
        return scenario

    def run_scenario(self, scenario, **kwargs):
        platform = self.platform(scenario)
        return platform, self.run_runner(platform, **kwargs)

    @staticmethod
    def now():
        import time

        return int(time.time())

    def runner_env(self, platform, path=SESSION_PATH, extra=None):
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "BOOTSTRAP_URL": platform.url(path),
            "VERIFIER_ALLOW_UNPRIVILEGED": "1",
            "VERIFIER_WORKDIR": str(self.work),
            "VERIFIER_STEP_ENV_FILE": str(self.dir / "no-step.env"),
            "VERIFIER_RUNTIME": "node",
            **GIT_ENV,
        }
        env.update(extra or {})
        return env

    def run_runner(self, platform, path=SESSION_PATH, extra=None, timeout=120):
        done = subprocess.run(
            ["bash", str(RUNNER)],
            env=self.runner_env(platform, path, extra),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        self.last_output = done.stdout
        return done

    def stub_sleep(self, backoff_only=True):
        """A `sleep` in front of the real one that records (and skips) the runner's back-off
        waits of 2, 4, 8 and 20 seconds into the events log and passes everything else on."""
        real = shutil.which("sleep")
        bin_dir = self.dir / "stub-bin"
        bin_dir.mkdir()
        script = bin_dir / "sleep"
        script.write_text(
            "#!/bin/sh\n"
            'case "$1" in 2|4|8|20) printf \'{"sleep": %%s}\\n\' "$1" >>"%s"; exit 0;; esac\n'
            'exec "%s" "$@"\n' % (self.events_log, real)
        )
        script.chmod(0o755)
        return {"PATH": "%s:%s" % (bin_dir, os.environ["PATH"])}

    def record_argv(self, *names):
        """Puts a recording stand-in for each named program in front of the real one: every
        argument of every call is appended, a line each, to the file this returns. Programs the
        runner starts get no URL or secret, so the log is searched for them. Returns (env with
        the new PATH, the log path)."""
        log = self.dir / "argv.log"
        log.write_text("")
        bin_dir = self.dir / "argv-bin"
        bin_dir.mkdir(exist_ok=True)
        for name in names:
            real = shutil.which(name)
            script = bin_dir / name
            script.write_text(
                "#!/bin/sh\nfor a in \"$@\"; do printf '%%s\\n' \"$a\"; done >>\"%s\"\nexec \"%s\" \"$@\"\n" % (log, real)
            )
            script.chmod(0o755)
        return {"PATH": "%s:%s" % (bin_dir, os.environ["PATH"])}, log

    def events(self):
        lines = [json.loads(line) for line in self.events_log.read_text().splitlines() if line.strip()]
        return lines

    def result(self, seq):
        return json.loads((self.results / ("result-%d.json" % seq)).read_text())

    def ready(self):
        return json.loads((self.results / "ready.json").read_text())

    @staticmethod
    def status_of(result, name=None, index=None):
        found = [
            s
            for s in result["steps"]
            if (name is None or s["name"] == name) and (index is None or s.get("index") == index)
        ]
        return [s["status"] for s in found]

    def assertAllPass(self, result, msg=""):
        runner_only = ("new_test_fails_before", "new_test_passes_after")
        bad = [
            (s["name"], s["status"], s.get("excerpt", "")[-300:])
            for s in result["steps"]
            if s["status"] != "pass" and not (s["status"] == "skipped" and s["name"] in runner_only)
        ]
        self.assertEqual(bad, [], msg or json.dumps(result)[:2000])

    def assertRunnerOk(self, done):
        self.assertEqual(done.returncode, 0, done.stdout[-3000:])


__all__ = [
    "HangingServer",
    "RUN_PATH",
    "SESSION_PATH",
    "SessionCase",
    "have_git_http_backend",
    "iso",
    "job",
    "make_origin",
    "missing_tools",
    "runner_tools",
    "step",
    "unittest",
]
