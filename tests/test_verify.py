"""
Tests for the optional post-write `verify` command on fix_symbol and
create_file (RF-13, docs/critical-review.md).

The gap this closes: every gate before this one checks the WRITE is
syntactically sound, not that it is CORRECT. A worker can produce a
`create_file` result that parses fine and asserts the opposite of the
specified contract — nothing upstream of `verify` can see that, only running
the caller's own check can. `verify` is a real subprocess, run against a real
git repo, exactly the way an orchestrator would run its own test suite by
hand; nothing here is stubbed.

stdlib unittest only. Every assertion here is meant to be able to fail: where
an assertion would hold purely by construction, it is not included.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.support import requires_git  # noqa: E402

from mcp_bifrost.engine import Engine  # noqa: E402
from mcp_bifrost.worker import WorkerResult  # noqa: E402


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True)


def make_git_repo(root: Path) -> Path:
    proc = _git(root, "init", "-q")
    assert proc.returncode == 0, proc.stderr
    _git(root, "config", "user.email", "bifrost-tests@example.com")
    _git(root, "config", "user.name", "Bifrost Tests")
    return root


def commit_file(repo: Path, rel_path: str, content: bytes) -> Path:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    assert _git(repo, "add", rel_path).returncode == 0
    assert _git(repo, "commit", "-q", "-m", "fixture").returncode == 0
    return path


class FixedWorker:
    """Always returns the same successful output."""

    def __init__(self, out: str) -> None:
        self.out = out

    def run(self, payload: dict) -> WorkerResult:
        return WorkerResult(
            ok=True, out=self.out, why="stub", diff_stat="+0/-0", error=None,
            ms=1, tokens_in=5, tokens_out=5, cache_hit=0, request_bytes=0,
            response_bytes=0,
        )


MODULE = "def add(a, b):\n    return a + b\n"


@requires_git
class EngineVerifyTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = make_git_repo(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def make_engine(self, worker) -> Engine:
        db_path = Path(self._tmp.name) / "log.sqlite3"
        return Engine(worker=worker, db_path=db_path, entropy_scan=False)


class FixSymbolVerifyTest(EngineVerifyTestBase):
    def setUp(self):
        super().setUp()
        self.target = commit_file(self.repo, "calc.py", MODULE.encode())

    def test_passing_command_reports_success(self):
        engine = self.make_engine(
            FixedWorker("def add(a, b):\n    return int(a) + int(b)"))
        outcome = engine.fix_symbol(str(self.target), "add",
                                    "coerce operands", verify="true")
        self.assertTrue(outcome.ok, outcome.message)
        self.assertIn("int(a) + int(b)", self.target.read_text())
        engine.close()

    def test_failing_command_reverts_the_write_and_returns_its_output(self):
        engine = self.make_engine(
            FixedWorker("def add(a, b):\n    return int(a) + int(b)"))
        outcome = engine.fix_symbol(
            str(self.target), "add", "coerce operands",
            verify="echo VERIFY_MARKER_XYZ 1>&2; exit 1")

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.gate, "verify")
        self.assertIn("VERIFY_MARKER_XYZ", outcome.message)
        # The whole point: a failed check must leave the file as it was
        # BEFORE this patch, not as the (syntactically fine, worker-produced)
        # candidate that failed the check.
        self.assertEqual(MODULE, self.target.read_text())
        engine.close()

    def test_command_runs_with_cwd_at_the_repo_root(self):
        """A relative path in the command only resolves if cwd is the repo
        root — proves `verify` is not run from some other directory."""
        engine = self.make_engine(FixedWorker(MODULE.rstrip("\n")))
        outcome = engine.fix_symbol(str(self.target), "add", "no-op",
                                    verify="test -f calc.py")
        self.assertTrue(outcome.ok, outcome.message)
        engine.close()


class CreateFileVerifyTest(EngineVerifyTestBase):
    def test_passing_command_reports_success(self):
        target = self.repo / "fresh.py"
        engine = self.make_engine(FixedWorker("def hi():\n    return 'hi'"))
        outcome = engine.create_file(str(target), "make a hi function",
                                     verify="true")
        self.assertTrue(outcome.ok, outcome.message)
        self.assertTrue(target.is_file())
        engine.close()

    def test_failing_command_deletes_the_file_and_returns_its_output(self):
        target = self.repo / "fresh.py"
        engine = self.make_engine(FixedWorker("def hi():\n    return 'hi'"))
        outcome = engine.create_file(
            str(target), "make a hi function",
            verify="echo VERIFY_MARKER_ABC 1>&2; exit 1")

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.gate, "verify")
        self.assertIn("VERIFY_MARKER_ABC", outcome.message)
        self.assertFalse(target.exists(),
                         "a failed verify must not leave the file behind")
        engine.close()


if __name__ == "__main__":
    unittest.main()
