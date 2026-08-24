"""
Guards for the external binaries this suite shells out to.

Two of them are not Python: `php`, which IS the PHP parser (there is no
pure-Python substitute — see mcp_bifrost/languages/extract.php), and `git`,
which the VCS layer drives for real in the end-to-end tests. Neither is a
Python dependency, so neither can be declared in pyproject.toml, and a fresh
clone on a machine without them used to produce 53 failures and a wall of
`FileNotFoundError` that read like broken code rather than a missing binary.

Skipping is the honest signal: without `php` the PHP half of the suite is
not proven, and saying so is better than claiming a pass it did not earn.
"""

from __future__ import annotations

import shutil
import unittest

HAS_PHP = shutil.which("php") is not None
HAS_GIT = shutil.which("git") is not None

requires_php = unittest.skipUnless(
    HAS_PHP, "requires the php CLI binary (apt install php-cli / brew install php)"
)
requires_git = unittest.skipUnless(
    HAS_GIT, "requires the git binary"
)


# --------------------------------------------------------------- stub worker

class StubWorkerServer:
    """
    An OpenAI-compatible endpoint that answers from a script.

    The engine-level tests inject a fake worker object, which skips
    `worker.py` entirely — the HTTP client, the response parsing, the fence
    stripping and the token accounting were all unexercised as a result. This
    is the same idea one layer down: a real socket, real JSON over the wire,
    and `BIFROST_WORKER_BASE_URL` pointed at it, so everything from
    `urllib.request` inwards runs for real. No network leaves the machine and
    no API key is spent.

    It also records what it was sent, which makes the payload contract
    testable in the other direction: the block Bifrost hands a worker is
    supposed to arrive at zero indentation, and nothing asserted that.

    Usage:

        with StubWorkerServer([{"out": "def f():\\n    return 2"}]) as stub:
            os.environ["BIFROST_WORKER_BASE_URL"] = stub.base_url
            ...
        stub.payloads   # what Bifrost actually sent
    """

    def __init__(self, script, status=200):
        # script: list of dicts (the worker JSON), or a callable taking the
        # decoded payload and returning one.
        self.script = script
        self.status = status
        self.payloads: list[dict] = []
        self.requests: list[dict] = []
        self._i = 0
        self._httpd = None
        self._thread = None

    # -- the handler needs to reach the instance; a closure is the least
    #    ceremony that does it.
    def _make_handler(self):
        import json
        from http.server import BaseHTTPRequestHandler
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # keep the test output clean
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                body = json.loads(raw.decode("utf-8"))
                outer.requests.append({
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": body,
                })
                # The payload Bifrost built is the user message, itself JSON.
                try:
                    outer.payloads.append(
                        json.loads(body["messages"][-1]["content"]))
                except Exception:  # noqa: BLE001 - a malformed call is a finding
                    outer.payloads.append({"__unparsable__": body})

                if callable(outer.script):
                    answer = outer.script(outer.payloads[-1])
                else:
                    i = min(outer._i, len(outer.script) - 1)
                    answer = outer.script[i]
                    outer._i += 1

                if isinstance(answer, str):          # raw body, malformed on purpose
                    payload = answer.encode("utf-8")
                    status = outer.status
                else:
                    status = answer.pop("__status__", outer.status)
                    content = answer.pop("__content__", None)
                    if content is None:
                        content = json.dumps(answer)
                    payload = json.dumps({
                        "choices": [{"message": {"content": content}}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                                  "prompt_cache_hit_tokens": 0},
                    }).encode("utf-8")

                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return Handler

    def __enter__(self):
        import threading
        from http.server import HTTPServer

        self._httpd = HTTPServer(("127.0.0.1", 0), self._make_handler())
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
        return False

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"
