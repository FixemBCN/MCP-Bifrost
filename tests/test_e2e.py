"""
End-to-end tests: a real server process, a real socket, a real git repo.

Everything else in this suite injects a fake worker into the engine, which
means `server.py` and `worker.py` were never executed by a test — the two
modules a user cannot avoid. These start the server the way a client does,
`python3 -m mcp_bifrost.server` over stdio, and point it at an
OpenAI-compatible endpoint served from the test process itself. Nothing
leaves the machine and no API key is spent, but from `urllib.request`
inwards the code path is the production one.

The blocks the stub returns are written here rather than by a model, so what
these prove is the machinery: protocol framing, argument marshalling, the
worker's HTTP client, the gates, atomic application, the log, and rollback.
Whether a model returns good code is what `calibratge/` measures, and it is
a different question.

The batch test is the one that matters most. `fix_symbols` runs worker calls
in parallel and writes in series, re-resolving each symbol by name just
before its write because earlier patches have moved the later offsets. That
is the most dangerous code in the project and it had no test at all.
"""

from __future__ import annotations

import json
import os
import select
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.support import StubWorkerServer, requires_git  # noqa: E402

TIMEOUT = 60


class Client:
    """A minimal MCP client: one JSON object per line, in and out."""

    def __init__(self, cwd: Path, base_url: str, db_path: Path):
        env = dict(os.environ)
        env.update({
            "BIFROST_WORKER_BASE_URL": base_url,
            "BIFROST_WORKER_MODEL": "stub",
            "BIFROST_DB": str(db_path),
            "PYTHONPATH": str(_REPO_ROOT),
        })
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "mcp_bifrost.server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, cwd=str(cwd), text=True,
            bufsize=1)
        self._id = 0

    def __enter__(self):
        self.call("initialize", {"protocolVersion": "2025-06-18",
                                 "capabilities": {},
                                 "clientInfo": {"name": "test", "version": "0"}})
        self.notify("notifications/initialized")
        return self

    def __exit__(self, *exc):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            self.proc.kill()
        finally:
            for pipe in (self.proc.stdout, self.proc.stderr):
                try:
                    pipe.close()
                except Exception:  # noqa: BLE001
                    pass
        return False

    def _write(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self._write({"jsonrpc": "2.0", "id": self._id, "method": method,
                     "params": params or {}})
        ready, _, _ = select.select([self.proc.stdout], [], [], TIMEOUT)
        if not ready:
            raise AssertionError(
                f"no response to {method} in {TIMEOUT}s; "
                f"stderr: {self.proc.stderr.read()}")
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError(f"server exited; stderr: {self.proc.stderr.read()}")
        resp = json.loads(line)
        if resp.get("id") != self._id:
            raise AssertionError(
                f"response id {resp.get('id')} does not match request "
                f"{self._id}: the stream is out of step. {resp}")
        return resp

    def tool(self, name: str, **arguments) -> tuple[bool, str]:
        result = self.call("tools/call",
                           {"name": name, "arguments": arguments})["result"]
        return result.get("isError", False), result["content"][0]["text"]


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout.strip()


MODULE = '''"""A module to patch."""


class Calculator:
    def add(self, a, b):
        return a + b

    def subtract(self, a, b):
        return a - b

    def multiply(self, a, b):
        return a * b
'''


@requires_git
class EndToEndTest(unittest.TestCase):
    """One instruction, one server process, one file on disk."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        self.target = self.repo / "calc.py"
        self.target.write_text(MODULE, encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "initial")
        self.db = self.repo / "history.db"

    def client(self, stub):
        return Client(self.repo, stub.base_url, self.db)

    def rows(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(
                "SELECT op, fitxer, simbol, estat, porta, rationale "
                "FROM patches ORDER BY ts").fetchall()
        finally:
            conn.close()

    # -------------------------------------------------------------- one patch

    def test_a_patch_travels_the_whole_way_and_lands_on_disk(self):
        block = "def add(self, a, b):\n    return int(a) + int(b)"
        with StubWorkerServer([{"out": block, "why": "coerce the operands",
                                "diff_stat": "+1/-1"}]) as stub:
            with self.client(stub) as client:
                tools = client.call("tools/list")["result"]["tools"]
                self.assertIn("fix_symbol", [t["name"] for t in tools])

                is_error, text = client.tool(
                    "fix_symbol", file_path=str(self.target),
                    symbol_name="Calculator.add",
                    instruction="coerce both operands with int()")

        self.assertFalse(is_error, text)
        self.assertIn("OK", text)
        source = self.target.read_text(encoding="utf-8")
        self.assertIn("return int(a) + int(b)", source)
        self.assertIn("return a - b", source, "a neighbour was disturbed")
        self.assertTrue(source.startswith('"""A module to patch."""'))

        rows = self.rows()
        self.assertEqual(1, len(rows))
        op, fitxer, simbol, estat, porta, rationale = rows[0]
        self.assertEqual(("fix_symbol", "Calculator.add", "ok"),
                         (op, simbol, estat))
        self.assertEqual("coerce the operands", rationale)

    def test_the_worker_is_handed_the_block_at_zero_indentation(self):
        """The contract in the other direction: what Bifrost sends. A method
        arrives dedented, with its real indentation in `indent`, and the
        worker is told not to reproduce it."""
        block = "def subtract(self, a, b):\n    return a - b"
        with StubWorkerServer([{"out": block, "why": "unchanged",
                                "diff_stat": "+0/-0"}]) as stub:
            with self.client(stub) as client:
                client.tool("fix_symbol", file_path=str(self.target),
                            symbol_name="Calculator.subtract",
                            instruction="leave it as it is")

            payload = stub.payloads[0]
            self.assertEqual("python", payload["lang"])
            self.assertEqual("Calculator.subtract", payload["sym"])
            self.assertEqual("    ", payload["indent"])
            self.assertTrue(payload["src"].startswith("def subtract"),
                            f"src was indented: {payload['src'][:40]!r}")

    def test_a_worker_failure_leaves_the_file_exactly_as_it_was(self):
        before = self.target.read_bytes()
        with StubWorkerServer(["not json at all"], status=500) as stub:
            with self.client(stub) as client:
                is_error, text = client.tool(
                    "fix_symbol", file_path=str(self.target),
                    symbol_name="Calculator.add", instruction="anything")

        self.assertTrue(is_error, text)
        self.assertIn("500", text)
        self.assertEqual(before, self.target.read_bytes())
        self.assertEqual([("fix_symbol", "error", "worker")],
                         [(r[0], r[3], r[4]) for r in self.rows()])

    # ------------------------------------------------------------------ batch

    def test_a_batch_lands_every_symbol_although_each_write_moves_the_next(self):
        """
        Three symbols in one file, each replacement a different length from
        the original, so every write shifts the offsets of the ones after it.
        Re-resolution by name is what makes this safe, and nothing tested it.
        """
        replacements = {
            "Calculator.add": (
                "def add(self, a, b):\n"
                "    # a deliberately longer body, to move what follows\n"
                "    total = a + b\n"
                "    return total"),
            "Calculator.subtract": "def subtract(self, a, b):\n    return a - b",
            "Calculator.multiply": (
                "def multiply(self, a, b):\n"
                "    # and a longer one again\n"
                "    product = a * b\n"
                "    return product"),
        }

        def answer(payload):
            return {"out": replacements[payload["sym"]],
                    "why": f"rewrote {payload['sym']}", "diff_stat": "+1/-1"}

        with StubWorkerServer(answer) as stub:
            with self.client(stub) as client:
                is_error, text = client.tool(
                    "fix_symbols",
                    targets=[{"file_path": str(self.target),
                              "symbol_name": name} for name in replacements],
                    instruction="rewrite each body")

        self.assertFalse(is_error, text)
        # A successful batch renders its diff_stat, not its message: the
        # "3/3 applied" count the engine composes is dropped by `render()`.
        self.assertIn("3 ok", text)

        source = self.target.read_text(encoding="utf-8")
        for name, block in replacements.items():
            for line in block.splitlines()[1:]:
                self.assertIn(line.strip(), source,
                              f"{name} did not land intact")
        self.assertTrue(source.startswith('"""A module to patch."""'))
        self.assertEqual(1, source.count("class Calculator:"))

        # The file must still be the file, not three blocks in a trench coat.
        import ast
        tree = ast.parse(source)
        methods = [n.name for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)]
        self.assertEqual(["add", "subtract", "multiply"], methods)

        self.assertEqual(3, len([r for r in self.rows() if r[3] == "ok"]))

    def test_a_batch_reports_the_target_it_could_not_resolve(self):
        block = "def add(self, a, b):\n    return a + b"
        with StubWorkerServer([{"out": block, "why": "ok", "diff_stat": "+0/-0"}]) as stub:
            with self.client(stub) as client:
                is_error, text = client.tool(
                    "fix_symbols",
                    targets=[{"file_path": str(self.target),
                              "symbol_name": "Calculator.add"},
                             {"file_path": str(self.target),
                              "symbol_name": "Calculator.divide"}],
                    instruction="rewrite each body")

        self.assertTrue(is_error, text)
        self.assertIn("1/2 applied", text)
        self.assertIn("divide", text)

    # ----------------------------------------------------------- the log side

    def test_publish_session_puts_the_work_on_a_branch_with_its_reasons(self):
        block = "def add(self, a, b):\n    return int(a) + int(b)"
        with StubWorkerServer([{"out": block, "why": "coerce the operands",
                                "diff_stat": "+1/-1"}]) as stub:
            with self.client(stub) as client:
                client.tool("fix_symbol", file_path=str(self.target),
                            symbol_name="Calculator.add",
                            instruction="coerce both operands")
                is_error, text = client.tool(
                    "publish_session", branch="bifrost/coercion",
                    subject="Coerce the operands")

        self.assertFalse(is_error, text)
        self.assertIn("bifrost/coercion", git(self.repo, "branch", "--list",
                                              "bifrost/coercion"))
        message = git(self.repo, "log", "-1", "--format=%B", "bifrost/coercion")
        self.assertIn("Coerce the operands", message)
        self.assertIn("coerce the operands", message,
                      "the rationale did not reach the commit body")

    def test_export_docs_turns_the_log_into_a_changelog(self):
        block = "def add(self, a, b):\n    return int(a) + int(b)"
        out_path = self.repo / "CHANGES.md"
        with StubWorkerServer([{"out": block, "why": "coerce the operands",
                                "diff_stat": "+1/-1"}]) as stub:
            with self.client(stub) as client:
                client.tool("fix_symbol", file_path=str(self.target),
                            symbol_name="Calculator.add",
                            instruction="coerce both operands")
                is_error, text = client.tool(
                    "export_docs", output_path=str(out_path),
                    title="What changed", session_only=True)

        self.assertFalse(is_error, text)
        self.assertTrue(out_path.is_file(), text)
        written = out_path.read_text(encoding="utf-8")
        self.assertIn("What changed", written)
        self.assertIn("coerce the operands", written)
        self.assertIn("calc.py", written)

    def test_revert_session_puts_every_file_back(self):
        before = self.target.read_bytes()
        block = "def add(self, a, b):\n    return int(a) + int(b)"
        with StubWorkerServer([{"out": block, "why": "coerce", "diff_stat": "+1/-1"}]) as stub:
            with self.client(stub) as client:
                client.tool("fix_symbol", file_path=str(self.target),
                            symbol_name="Calculator.add",
                            instruction="coerce both operands")
                self.assertNotEqual(before, self.target.read_bytes())
                is_error, text = client.tool("revert_session")

        self.assertFalse(is_error, text)
        self.assertEqual(before, self.target.read_bytes())


@requires_git
class InsertionSpacingTest(unittest.TestCase):
    """
    What an insertion looks like on disk, not just whether it parses.

    Every insertion used one newline on each side regardless of language or
    nesting, so a new top-level class arrived welded to the line above it:
    valid Python, and flagged by every linter that will ever read the file.
    The gap is now the language's own — PEP 8's two lines between top-level
    definitions, one between methods — and it is measured against what is
    already at the seam, because `end_of_file` sits just past a newline
    while `after` sits at the end of a line's text.
    """

    MODULE = ('"""Module."""\n\n\nclass K:\n    def a(self):\n        return 1\n'
              "\n    def b(self):\n        return 2\n\n\ndef top():\n    return 3\n")

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        self.target = self.repo / "m.py"
        self.target.write_text(self.MODULE, encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "initial")
        self.db = self.repo / "history.db"

    def _insert(self, anchor: str, position: str, block: str) -> str:
        with StubWorkerServer([{"out": block, "why": "added",
                                "diff_stat": "+1/-0"}]) as stub:
            with Client(self.repo, stub.base_url, self.db) as client:
                is_error, text = client.tool(
                    "insert_symbol", file_path=str(self.target),
                    anchor=anchor, position=position,
                    instruction="add the symbol")
        self.assertFalse(is_error, text)
        return self.target.read_text(encoding="utf-8")

    def test_a_new_method_is_separated_by_one_blank_line(self):
        source = self._insert("K.a", "after",
                              "def mid(self):\n    return 9")
        self.assertIn("        return 1\n\n    def mid(self):\n", source)
        self.assertIn("        return 9\n\n    def b(self):\n", source)

    def test_a_new_top_level_class_is_separated_by_two(self):
        source = self._insert("top", "after",
                              "class New:\n    def m(self):\n        return 6")
        self.assertIn("    return 3\n\n\nclass New:\n", source)
        self.assertNotIn("    return 3\nclass New:", source)

    def test_an_insertion_before_the_anchor_keeps_the_gap_on_the_right_side(self):
        source = self._insert("K.b", "before",
                              "def pre(self):\n    return 8")
        self.assertIn("        return 1\n\n    def pre(self):\n", source)
        self.assertIn("        return 8\n\n    def b(self):\n", source)

    def test_end_of_file_does_not_stack_blank_lines(self):
        source = self._insert("top", "end_of_file",
                              "def tail():\n    return 7")
        self.assertIn("    return 3\n\n\ndef tail():\n", source)
        self.assertTrue(source.endswith("    return 7\n"), repr(source[-30:]))
        self.assertNotIn("\n\n\n\n", source)
