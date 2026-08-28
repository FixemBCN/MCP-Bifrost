"""
Protocol-layer tests for the MCP server.

`server.py` had no tests at all — 96 statements, none executed — and it is
the one part of the project every user talks to: a client speaks JSON-RPC to
this and to nothing else. These exercise `Server.handle` and `Server.serve`
directly, with a recording double in place of the engine, so the protocol is
tested apart from the patching.

The regression at the centre of the file: a request with no `id` is a
notification and JSON-RPC 2.0 forbids answering one. Only
`notifications/initialized` was recognised as such; every other notification
fell through to the unknown-method branch and came back as an error object
with `"id": null`. A client that reads one response per request then takes
that stray error as the answer to its next call, and from there every tool
result is attributed to the wrong request for the rest of the session.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp_bifrost.engine import Outcome  # noqa: E402
from mcp_bifrost.server import TOOLS, Server  # noqa: E402


class RecordingEngine:
    """
    Stands in for the engine and remembers how it was called.

    Every tool returns the same success, so a test that fails here is a
    dispatch or argument-marshalling fault and nothing else.
    """

    def __init__(self, outcome: Outcome | None = None) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.outcome = outcome or Outcome(True, "applied", patch_id="a" * 32)

    def __getattr__(self, name: str):
        def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self.outcome
        return method


def request(method: str, params: dict | None = None, rid=1) -> dict:
    req = {"jsonrpc": "2.0", "method": method}
    if rid is not None:
        req["id"] = rid
    if params is not None:
        req["params"] = params
    return req


class InitializeTest(unittest.TestCase):
    def setUp(self):
        self.server = Server(RecordingEngine())

    def test_a_supported_protocol_version_is_echoed_back(self):
        """Telling a client a version it did not ask for makes it decide
        whether to proceed, and the strict ones do not."""
        for asked in Server.PROTOCOLS:
            with self.subTest(asked=asked):
                resp = self.server.handle(
                    request("initialize", {"protocolVersion": asked}))
                self.assertEqual(asked, resp["result"]["protocolVersion"])

    def test_an_unknown_protocol_version_falls_back_to_our_own(self):
        resp = self.server.handle(
            request("initialize", {"protocolVersion": "1999-01-01"}))
        self.assertEqual(Server.PROTOCOL, resp["result"]["protocolVersion"])
        self.assertIn("tools", resp["result"]["capabilities"])


class NotificationTest(unittest.TestCase):
    """The regression this file was written for."""

    def setUp(self):
        self.server = Server(RecordingEngine())

    def test_no_notification_is_ever_answered(self):
        for method in ("notifications/initialized",
                       "notifications/cancelled",
                       "notifications/progress",
                       "notifications/roots/list_changed"):
            with self.subTest(method=method):
                self.assertIsNone(
                    self.server.handle(request(method, {}, rid=None)),
                    f"{method} was answered; a notification must not be")

    def test_a_stray_answer_would_desynchronise_the_stream(self):
        """
        The failure this prevents, written as the client sees it: one
        notification followed by one real call must produce exactly one line
        on stdout, and it must be the answer to the call.
        """
        stdin = io.StringIO(
            json.dumps(request("notifications/cancelled", {"requestId": 7},
                               rid=None)) + "\n"
            + json.dumps(request("ping", {}, rid=42)) + "\n"
        )
        stdout = io.StringIO()
        self.server.serve(stdin, stdout)

        lines = [l for l in stdout.getvalue().splitlines() if l.strip()]
        self.assertEqual(1, len(lines), f"expected one response, got {lines}")
        self.assertEqual(42, json.loads(lines[0])["id"])


class ToolDispatchTest(unittest.TestCase):
    """
    Every tool the server advertises must be reachable.

    A tool listed in TOOLS but missing from the `_call` chain is invisible
    until a user asks for it, and then it answers "unknown tool" — a
    contradiction the client cannot resolve. The reverse, a branch with no
    entry in TOOLS, is dead code no client can reach.
    """

    def setUp(self):
        self.engine = RecordingEngine()
        self.server = Server(self.engine)

    def test_every_advertised_tool_is_dispatchable(self):
        for tool in TOOLS:
            with self.subTest(tool=tool["name"]):
                result = self.server._call(tool["name"], {})
                text = result["content"][0]["text"]
                self.assertNotIn(
                    "unknown tool", text,
                    f"{tool['name']} is advertised but not dispatched")

    def test_every_tool_declares_a_description_and_a_schema(self):
        for tool in TOOLS:
            with self.subTest(tool=tool["name"]):
                self.assertTrue(tool.get("description", "").strip())
                schema = tool["inputSchema"]
                self.assertEqual("object", schema["type"])
                for name in schema.get("required", []):
                    self.assertIn(
                        name, schema["properties"],
                        f"{tool['name']} requires {name} but does not declare it")

    def test_a_missing_required_argument_names_itself(self):
        """`ERROR missing argument: 'symbol_name'` is actionable; a
        traceback is not."""
        result = self.server._call("fix_symbol", {"file_path": "x.py"})
        self.assertTrue(result["isError"])
        self.assertIn("missing argument", result["content"][0]["text"])
        self.assertIn("symbol_name", result["content"][0]["text"])

    def test_an_unknown_tool_is_an_error_not_a_crash(self):
        result = self.server._call("fix_everything", {})
        self.assertTrue(result["isError"])
        self.assertIn("unknown tool", result["content"][0]["text"])

    def test_an_engine_refusal_is_reported_as_an_error_result(self):
        self.engine.outcome = Outcome(False, "gate 2 refused it", gate="single")
        result = self.server._call("fix_symbol", {
            "file_path": "x.py", "symbol_name": "f", "instruction": "do it"})
        self.assertTrue(result["isError"])
        self.assertIn("gate 2 refused it", result["content"][0]["text"])

    def test_a_successful_patch_carries_a_short_patch_id(self):
        result = self.server._call("fix_symbol", {
            "file_path": "x.py", "symbol_name": "f", "instruction": "do it"})
        self.assertFalse(result["isError"])
        self.assertIn("[aaaaaaaa]", result["content"][0]["text"])

    def test_arguments_reach_the_engine_in_the_declared_order(self):
        """Positional marshalling: a swapped pair here would patch the
        wrong symbol with the right instruction, and nothing downstream
        would notice."""
        self.server._call("fix_symbol", {
            "file_path": "a.py", "symbol_name": "Klass.method",
            "instruction": "rewrite it", "context": ["ctx"],
            "allow_secrets": True, "verify": "pytest -q"})
        name, args, kwargs = self.engine.calls[-1]
        self.assertEqual("fix_symbol", name)
        self.assertEqual(
            ("a.py", "Klass.method", "rewrite it", ["ctx"], True, "pytest -q"),
            args)

    def test_an_omitted_verify_argument_reaches_the_engine_as_none(self):
        self.server._call("create_file", {
            "file_path": "n.py", "instruction": "write it"})
        name, args, kwargs = self.engine.calls[-1]
        self.assertEqual("create_file", name)
        self.assertEqual(("n.py", "write it", None, False, None), args)

