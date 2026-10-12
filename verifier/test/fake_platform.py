#!/usr/bin/env python3
"""A loopback platform for verifier/test/contract.sh.

GET /bootstrap answers the bootstrap file, once (a second claim answers 409, like the
real single-claim URL), and PUT /result stores the body to the result file and answers
200. Nothing else. Usage: fake_platform.py PORT BOOTSTRAP_FILE RESULT_FILE
"""
import http.server
import sys

port, bootstrap_file, result_file = int(sys.argv[1]), sys.argv[2], sys.argv[3]
state = {"claimed": False}


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, status, body):
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/bootstrap":
            if state["claimed"]:
                self._send(409, b'{"error":"this URL has already been used"}')
                return
            state["claimed"] = True
            with open(bootstrap_file, "rb") as handle:
                self._send(200, handle.read())
        else:
            self._send(404, b"{}")

    def do_PUT(self):
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        if self.path == "/result":
            with open(result_file, "wb") as handle:
                handle.write(body)
            self._send(200, b'{"status":"recorded"}')
        else:
            self._send(404, b"{}")

    def log_message(self, *_args):
        pass


http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
