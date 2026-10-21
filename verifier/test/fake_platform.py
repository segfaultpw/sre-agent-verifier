#!/usr/bin/env python3
"""A loopback platform for the runner's tests (verifier/test/contract.sh and tests/).

Two ways to use it.

As a program, the original form: `fake_platform.py PORT BOOTSTRAP_FILE RESULT_FILE`
answers the bootstrap file on `GET .../bootstrap` once (a second claim answers 409, like
the real single-claim URL) and stores the body of `PUT .../result` to the result file.

As a scenario, for session mode: `fake_platform.py --scenario FILE --port PORT --results DIR
[--log FILE]`, or `Platform(scenario, ...)` from Python. A scenario is a JSON object:

  bootstrap          the object answered on the bootstrap `GET` (once); "@PORT@" inside any
                     string is replaced by the listening port
  next               a list of answers to `GET .../next`, in order: {"status": 200, "job": {...}}
                     or {"status": 204} (or 404, 429, 503, ...), each with an optional
                     "delay_ms"; when the list is used up the answer is `exhausted_status`
  exhausted_status   default 410
  ready_answer       the object answered to `PUT .../ready` (default {})
  setup_done_answer  the object answered to `PUT .../setup_done` (default {})
  result_statuses    {"<seq>": 410} to refuse a job result
  credentials        proxy credentials the platform lists as live: {"user", "password", "kind",
                     "from": "start" | "ready" | "setup_done", "until": "never" | "ready" |
                     "setup_done"}; `POST /authorize` answers whether a credential is live now

Every request is recorded (method, path, query, time) in `events` and, with a log file, as a
JSON line. Bodies of `PUT`s are stored in the results directory: ready.json, setup_done.json,
result.json and result-<seq>.json.
"""
import http.server
import json
import os
import re
import socketserver
import sys
import threading
import time
import urllib.parse


def _fill(value, port):
    if isinstance(value, str):
        return value.replace("@PORT@", str(port))
    if isinstance(value, list):
        return [_fill(v, port) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, port) for k, v in value.items()}
    return value


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


class Platform:
    def __init__(self, scenario=None, results_dir=None, log_path=None, port=0, legacy=None):
        self.scenario = scenario or {}
        self.results_dir = results_dir
        self.log_path = log_path
        self.legacy = legacy  # (bootstrap_file, result_file) of the original program
        self.events = []
        self.lock = threading.Lock()
        self.claimed = False
        self.ready_done = False
        self.setup_done_done = False
        self.bodies = {}
        self.script = list(self.scenario.get("next", []))
        self._server = _Server(("127.0.0.1", port), self._handler())
        self.port = self._server.server_address[1]
        self._thread = None

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def start(self):
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def serve_forever(self):
        self._server.serve_forever()

    def record(self, **event):
        event["t"] = time.time()
        with self.lock:
            self.events.append(event)
            if self.log_path:
                with open(self.log_path, "a") as handle:
                    handle.write(json.dumps(event) + "\n")

    def requests(self, method=None, suffix=None):
        with self.lock:
            found = list(self.events)
        return [
            e
            for e in found
            if "method" in e
            and (method is None or e["method"] == method)
            and (suffix is None or e["path"].endswith(suffix))
        ]

    def store(self, name, body):
        with self.lock:
            self.bodies[name] = body
        if self.results_dir:
            with open(os.path.join(self.results_dir, name), "wb") as handle:
                handle.write(body)

    def body(self, name):
        with self.lock:
            return json.loads(self.bodies[name])

    def has(self, name):
        with self.lock:
            return name in self.bodies

    def is_live(self, credential):
        started = {"start": True, "ready": self.ready_done, "setup_done": self.setup_done_done}
        ended = {"never": False, "ready": self.ready_done, "setup_done": self.setup_done_done}
        return started[credential.get("from", "start")] and not ended[credential.get("until", "never")]

    def authorize(self, request):
        with self.lock:
            for credential in self.scenario.get("credentials", []):
                if (
                    credential["user"] == request.get("user")
                    and credential["password"] == request.get("password")
                    and self.is_live(credential)
                ):
                    return {"allow": True, "kind": credential["kind"]}
        return {"allow": False}

    def _handler(self):
        platform = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, status, body=b"{}"):
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status, value):
                self._send(status, json.dumps(value).encode())

            def _body(self):
                length = int(self.headers.get("content-length", "0"))
                return self.rfile.read(length)

            def _note(self, body=b""):
                parts = urllib.parse.urlsplit(self.path)
                platform.record(
                    method=self.command,
                    path=parts.path,
                    query=parts.query,
                    bytes=len(body),
                )
                return parts

            def do_GET(self):
                parts = self._note()
                path = parts.path
                if path.endswith("/bootstrap"):
                    with platform.lock:
                        already = platform.claimed
                        platform.claimed = True
                    if already:
                        self._send(409, b'{"error":"this URL has already been used"}')
                    elif platform.legacy:
                        with open(platform.legacy[0], "rb") as handle:
                            self._send(200, handle.read())
                    else:
                        self._json(200, _fill(platform.scenario.get("bootstrap", {}), platform.port))
                elif path.endswith("/next"):
                    with platform.lock:
                        entry = platform.script.pop(0) if platform.script else None
                    if entry is None:
                        entry = {"status": platform.scenario.get("exhausted_status", 410)}
                    if entry.get("delay_ms"):
                        time.sleep(entry["delay_ms"] / 1000.0)
                    status = entry.get("status", 200)
                    if status == 200:
                        self._json(200, _fill(entry.get("job", {}), platform.port))
                    else:
                        self._json(status, {"status": status})
                else:
                    self._send(404)

            def do_PUT(self):
                body = self._body()
                parts = self._note(body)
                path = parts.path
                match = re.search(r"/jobs/(\d+)/result$", path)
                if platform.legacy and path == "/result":
                    with open(platform.legacy[1], "wb") as handle:
                        handle.write(body)
                    self._json(200, {"status": "recorded"})
                elif path.endswith("/ready"):
                    platform.store("ready.json", body)
                    with platform.lock:
                        platform.ready_done = True
                    self._json(200, platform.scenario.get("ready_answer", {}))
                elif path.endswith("/setup_done"):
                    platform.store("setup_done.json", body)
                    with platform.lock:
                        platform.setup_done_done = True
                    self._json(200, platform.scenario.get("setup_done_answer", {}))
                elif match:
                    seq = match.group(1)
                    status = platform.scenario.get("result_statuses", {}).get(seq, 200)
                    if status == 200:
                        platform.store("result-%s.json" % seq, body)
                    self._json(status, {"status": "recorded" if status == 200 else "refused"})
                elif path.endswith("/result"):
                    platform.store("result.json", body)
                    self._json(200, {"status": "recorded"})
                else:
                    self._send(404)

            def do_POST(self):
                body = self._body()
                parts = urllib.parse.urlsplit(self.path)
                if parts.path == "/authorize":
                    self._json(200, platform.authorize(json.loads(body or b"{}")))
                else:
                    self._send(404)

            def log_message(self, *_args):
                pass

        return Handler


def main(argv):
    if argv and argv[0] == "--scenario":
        options = {}
        it = iter(argv)
        for name in it:
            options[name] = next(it)
        with open(options["--scenario"]) as handle:
            scenario = json.load(handle)
        platform = Platform(
            scenario,
            results_dir=options["--results"],
            log_path=options.get("--log"),
            port=int(options["--port"]),
        )
    else:
        port, bootstrap_file, result_file = int(argv[0]), argv[1], argv[2]
        platform = Platform(port=port, legacy=(bootstrap_file, result_file))
    platform.serve_forever()


if __name__ == "__main__":
    main(sys.argv[1:])
