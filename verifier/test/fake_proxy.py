#!/usr/bin/env python3
"""A stdlib CONNECT/HTTP proxy for the runner's network tests.

It admits a request only when its `Proxy-Authorization: Basic` credential is one the platform
lists as live (it asks the fake platform's `POST /authorize`, as the real proxy asks the
platform) AND the destination is a host that credential's kind may reach. Everything else is
refused: 407 without a credential, 403 with a dead one or a forbidden host. Admitted requests
are forwarded to the destination, after mapping a name to an address (`allowed.test` to
127.0.0.1), so a test can have a host that only the proxy can reach.

The Basic credential decodes to `<kind>:<id>:<secret>`; the proxy splits it at the LAST colon
into the user (`run:<id>` or `session:<id>`) and the secret, the way the contract says.

As a program: `fake_proxy.py --port N --authorize URL [--hosts KIND=HOST:PORT,HOST:PORT]...
[--map NAME=ADDRESS]... [--log FILE]`.
"""
import base64
import http.client
import http.server
import json
import select
import socket
import socketserver
import sys
import threading
import time
import urllib.parse


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


class Proxy:
    def __init__(self, authorize_url, hosts, mapping=None, log_path=None, port=0):
        self.authorize = urllib.parse.urlsplit(authorize_url)
        self.hosts = {kind: set(h) for kind, h in hosts.items()}
        self.mapping = mapping or {}
        self.log_path = log_path
        self.events = []
        self.lock = threading.Lock()
        self._server = _Server(("127.0.0.1", port), self._handler())
        self.port = self._server.server_address[1]

    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def start(self):
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
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

    def seen(self, **match):
        with self.lock:
            return [e for e in self.events if all(e.get(k) == v for k, v in match.items())]

    def decide(self, header, authority):
        """Returns (status, kind, user): status 0 means admitted."""
        if not header or not header.lower().startswith("basic "):
            return 407, None, None
        try:
            decoded = base64.b64decode(header.split(None, 1)[1]).decode()
        except Exception:
            return 407, None, None
        user, _, secret = decoded.rpartition(":")
        connection = http.client.HTTPConnection(self.authorize.hostname, self.authorize.port, timeout=5)
        try:
            connection.request(
                "POST",
                self.authorize.path,
                json.dumps({"user": user, "password": secret, "host": authority}),
                {"content-type": "application/json"},
            )
            answer = json.loads(connection.getresponse().read())
        except Exception:
            return 403, None, user
        finally:
            connection.close()
        if not answer.get("allow"):
            return 403, None, user
        kind = answer.get("kind")
        if authority not in self.hosts.get(kind, set()):
            return 403, kind, user
        return 0, kind, user

    def _handler(self):
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def _refuse(self, status):
                body = b"refused"
                self.send_response(status)
                if status == 407:
                    self.send_header("Proxy-Authenticate", 'Basic realm="verifier"')
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _address(self, host):
                return proxy.mapping.get(host, host)

            def do_CONNECT(self):
                host, _, port = self.path.rpartition(":")
                authority = "%s:%s" % (host, port)
                status, kind, user = proxy.decide(self.headers.get("Proxy-Authorization"), authority)
                proxy.record(method="CONNECT", user=user, kind=kind, host=authority, allowed=status == 0)
                if status:
                    self._refuse(status)
                    return
                try:
                    upstream = socket.create_connection((self._address(host), int(port)), timeout=10)
                except OSError:
                    self._refuse(502)
                    return
                self.send_response(200, "Connection Established")
                self.end_headers()
                sockets = [self.connection, upstream]
                try:
                    while True:
                        readable, _, _ = select.select(sockets, [], [], 30)
                        if not readable:
                            break
                        done = False
                        for source in readable:
                            data = source.recv(65536)
                            if not data:
                                done = True
                                break
                            (upstream if source is self.connection else self.connection).sendall(data)
                        if done:
                            break
                finally:
                    upstream.close()

            def _forward(self):
                target = urllib.parse.urlsplit(self.path)
                if not target.hostname:
                    self._refuse(400)
                    return
                authority = "%s:%d" % (target.hostname, target.port or 80)
                status, kind, user = proxy.decide(self.headers.get("Proxy-Authorization"), authority)
                proxy.record(method=self.command, user=user, kind=kind, host=authority, allowed=status == 0)
                if status:
                    self.rfile.read(int(self.headers.get("content-length", "0")))
                    self._refuse(status)
                    return
                body = self.rfile.read(int(self.headers.get("content-length", "0")))
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in ("proxy-authorization", "proxy-connection", "connection")
                }
                connection = http.client.HTTPConnection(
                    self._address(target.hostname), target.port or 80, timeout=60
                )
                try:
                    path = target.path or "/"
                    if target.query:
                        path += "?" + target.query
                    connection.request(self.command, path, body or None, headers)
                    answer = connection.getresponse()
                    payload = answer.read()
                except OSError:
                    self._refuse(502)
                    return
                finally:
                    connection.close()
                self.send_response(answer.status)
                for key, value in answer.getheaders():
                    if key.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(key, value)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_PUT = do_POST = do_HEAD = do_DELETE = _forward

            def log_message(self, *_args):
                pass

        return Handler


def main(argv):
    options = {"--hosts": [], "--map": []}
    it = iter(argv)
    for name in it:
        value = next(it)
        if name in ("--hosts", "--map"):
            options[name].append(value)
        else:
            options[name] = value
    hosts = {}
    for spec in options["--hosts"]:
        kind, _, list_ = spec.partition("=")
        hosts[kind] = [h for h in list_.split(",") if h]
    mapping = dict(spec.split("=", 1) for spec in options["--map"])
    Proxy(
        options["--authorize"],
        hosts,
        mapping,
        log_path=options.get("--log"),
        port=int(options["--port"]),
    ).serve_forever()


if __name__ == "__main__":
    main(sys.argv[1:])
