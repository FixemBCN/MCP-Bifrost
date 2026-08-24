"""
The two tools nothing exercised: `fix_range` and `revert_patch`.

`fix_range` is the escape hatch — the way to reach code that lives outside
any symbol, which is where a parser cannot help and where the offsets are
computed by hand from line numbers. It had no test at all, and hand-computed
offsets with no test is the shape of the bug this whole project exists to
avoid.

Writing these found two: the range began at the start of its first line,
which includes that line's indentation, and a symbol's does not. `apply_indent`
pads every line but the first, precisely because the first one's padding is
already in the file — so the round trip did not hold, `normalise` gave up,
and the worker was handed an indented block to reproduce by hand. Calibration
put that error at 1 in 9. Separately, overshooting the end of the file was
told the file had one line more than it has, because splitting on the newline
leaves a phantom element after a trailing one.

`revert_patch` undoes exactly one patch. `revert_session` undoes a whole
batch and had an end-to-end test; the single-patch case did not, including
the part that distinguishes undoing a change from undoing a creation.
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

from mcp_bifrost.engine import Engine  # noqa: E402
from mcp_bifrost.worker import WorkerResult  # noqa: E402
from tests.support import requires_git  # noqa: E402


def result(out: str | None, ok: bool = True, error: str | None = None):
    return WorkerResult(ok=ok, out=out, why="stub", diff_stat="+0/-0",
                        error=error, ms=1, tokens_in=5, tokens_out=5,
                        cache_hit=0, request_bytes=0, response_bytes=0)


class EchoWorker:
    """Returns the block it was given. Records every payload."""

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def run(self, payload: dict) -> WorkerResult:
        self.payloads.append(payload)
        return result(payload["src"])


class FixedWorker:
    def __init__(self, out: str) -> None:
        self.out = out
        self.payloads: list[dict] = []

    def run(self, payload: dict) -> WorkerResult:
        self.payloads.append(payload)
        return result(self.out)


class RepoTestBase(unittest.TestCase):
    SOURCE = ("def compute(values):\n"
              "    # a comment with accents: àèíòú\n"
              "    total = 0\n"
              "    for value in values:\n"
              "        total += value\n"
              "    return total\n")

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.path = self.repo / "subject.py"
        self.path.write_text(self.SOURCE, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "initial")

    def git(self, *args: str) -> str:
        proc = subprocess.run(["git", "-C", str(self.repo), *args],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
        return proc.stdout.strip()

    def engine(self, worker) -> Engine:
        return Engine(worker=worker, db_path=self.repo / "log.db",
                      entropy_scan=False)


@requires_git
class FixRangeTest(RepoTestBase):
    def test_a_range_is_replaced_and_its_neighbours_are_not(self):
        engine = self.engine(FixedWorker("total = sum(values)"))
        outcome = engine.fix_range(str(self.path), 3, 5, "use sum()")
        engine.close()

        self.assertTrue(outcome.ok, outcome.message)
        self.assertEqual(
            "def compute(values):\n"
            "    # a comment with accents: àèíòú\n"
            "    total = sum(values)\n"
            "    return total\n",
            self.path.read_text(encoding="utf-8"))

    def test_the_block_is_sent_dedented_with_its_indent_declared(self):
        """The regression. The worker must never have to reproduce
        indentation: calibration had it wrong in 1 block of 9, and in Python
        that is not cosmetic."""
        worker = EchoWorker()
        engine = self.engine(worker)
        outcome = engine.fix_range(str(self.path), 3, 6, "leave it alone")
        engine.close()

        self.assertTrue(outcome.ok, outcome.message)
        payload = worker.payloads[0]
        self.assertEqual("    ", payload["indent"])
        self.assertTrue(payload["src"].startswith("total = 0"),
                        f"the block arrived indented: {payload['src'][:30]!r}")

    def test_an_echoed_range_leaves_the_file_byte_for_byte_identical(self):
        """Strip and re-apply must be exact inverses, over a file with
        multi-byte characters above the range."""
        before = self.path.read_bytes()
        engine = self.engine(EchoWorker())
        outcome = engine.fix_range(str(self.path), 3, 6, "leave it alone")
        engine.close()

        self.assertTrue(outcome.ok, outcome.message)
        self.assertEqual(before, self.path.read_bytes())

    def test_a_single_line_range_works(self):
        engine = self.engine(FixedWorker("total = 100"))
        outcome = engine.fix_range(str(self.path), 3, 3, "start at 100")
        engine.close()

        self.assertTrue(outcome.ok, outcome.message)
        self.assertIn("    total = 100\n", self.path.read_text(encoding="utf-8"))

    def test_code_outside_any_symbol_is_reachable(self):
        """The reason this tool exists: a parser offers no address here."""
        path = self.repo / "script.py"
        path.write_text("import os\n\nCONFIG = {'a': 1}\n\nprint(CONFIG)\n",
                        encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "script")

        engine = self.engine(FixedWorker("CONFIG = {'a': 1, 'b': 2}"))
        outcome = engine.fix_range(str(path), 3, 3, "add b")
        engine.close()

        self.assertTrue(outcome.ok, outcome.message)
        self.assertIn("CONFIG = {'a': 1, 'b': 2}",
                      path.read_text(encoding="utf-8"))

    def test_the_log_records_the_range_as_the_symbol(self):
        engine = self.engine(FixedWorker("total = 100"))
        engine.fix_range(str(self.path), 3, 3, "start at 100")
        rows = engine.log.session_patches(None, estat="ok")
        engine.close()

        self.assertEqual(1, len(rows))
        self.assertEqual("fix_range", rows[0]["op"])
        self.assertEqual("subject.py:3-3", rows[0]["simbol"])

    def test_an_inverted_or_zero_range_is_refused_before_the_worker(self):
        for start, end in ((0, 1), (3, 2), (-1, 4)):
            with self.subTest(start=start, end=end):
                worker = EchoWorker()
                engine = self.engine(worker)
                outcome = engine.fix_range(str(self.path), start, end, "x")
                engine.close()
                self.assertFalse(outcome.ok)
                self.assertEqual("input", outcome.gate)
                self.assertEqual([], worker.payloads)

    def test_overshooting_the_file_reports_its_real_length(self):
        """Splitting on the newline leaves a phantom element after a
        trailing one. It is not a line."""
        worker = EchoWorker()
        engine = self.engine(worker)
        outcome = engine.fix_range(str(self.path), 1, 99, "x")
        engine.close()

        self.assertFalse(outcome.ok)
        self.assertIn("file has 6 lines", outcome.message)
        self.assertEqual([], worker.payloads)

    def test_the_last_line_is_reachable(self):
        """The boundary the phantom element sat on."""
        engine = self.engine(FixedWorker("return total * 2"))
        outcome = engine.fix_range(str(self.path), 6, 6, "double it")
        engine.close()

        self.assertTrue(outcome.ok, outcome.message)
        self.assertTrue(self.path.read_text(encoding="utf-8")
                        .endswith("    return total * 2\n"))

    def test_a_range_larger_than_the_limit_is_refused(self):
        engine = self.engine(EchoWorker())
        engine.size_limit = 2
        outcome = engine.fix_range(str(self.path), 1, 6, "x")
        engine.close()

        self.assertFalse(outcome.ok)
        self.assertEqual("size", outcome.gate)

    def test_a_missing_file_is_refused(self):
        engine = self.engine(EchoWorker())
        outcome = engine.fix_range(str(self.repo / "ghost.py"), 1, 1, "x")
        engine.close()

        self.assertFalse(outcome.ok)
        self.assertEqual("input", outcome.gate)


@requires_git
class RevertPatchTest(RepoTestBase):
    def test_one_patch_is_undone_to_the_byte(self):
        before = self.path.read_bytes()
        engine = self.engine(FixedWorker(
            "def compute(values):\n    return sum(values)"))
        applied = engine.fix_symbol(str(self.path), "compute", "use sum()")
        self.assertTrue(applied.ok, applied.message)
        self.assertNotEqual(before, self.path.read_bytes())

        reverted = engine.revert_patch(applied.patch_id)
        engine.close()

        self.assertTrue(reverted.ok, reverted.message)
        self.assertEqual(before, self.path.read_bytes())

    def test_only_the_named_patch_is_undone(self):
        engine = self.engine(FixedWorker(
            "def compute(values):\n    return sum(values)"))
        first = engine.fix_symbol(str(self.path), "compute", "use sum()")

        other = self.repo / "second.py"
        other.write_text("def helper():\n    return 1\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-qm", "second")
        engine.worker = FixedWorker("def helper():\n    return 2")
        second = engine.fix_symbol(str(other), "helper", "return 2")
        self.assertTrue(second.ok, second.message)

        undone = engine.revert_patch(first.patch_id)
        engine.close()

        self.assertTrue(undone.ok, undone.message)
        self.assertIn("    return total", self.path.read_text(encoding="utf-8"),
                      "the named patch was not undone")
        self.assertIn("return 2", other.read_text(encoding="utf-8"),
                      "a patch nobody asked about was undone too")

    def test_undoing_a_creation_deletes_the_file(self):
        """There was no previous content to put back, so restoration is not
        what undo means here."""
        created = self.repo / "generated.py"
        engine = self.engine(FixedWorker("def made():\n    return 1"))
        applied = engine.create_file(str(created), "write a function")
        self.assertTrue(applied.ok, applied.message)
        self.assertTrue(created.is_file())

        reverted = engine.revert_patch(applied.patch_id)
        engine.close()

        self.assertTrue(reverted.ok, reverted.message)
        self.assertFalse(created.exists())

    def test_an_unknown_patch_id_is_refused(self):
        engine = self.engine(EchoWorker())
        outcome = engine.revert_patch("0" * 32)
        engine.close()

        self.assertFalse(outcome.ok)
        self.assertEqual("revert", outcome.gate)
        self.assertIn("unknown patch", outcome.message)

    def test_a_rejected_patch_has_nothing_to_undo(self):
        """It never reached disk, so there is no blob — and saying 'reverted'
        would be a lie about work that was never done."""
        engine = self.engine(FixedWorker("def a():\n    pass\ndef b():\n    pass"))
        refused = engine.fix_symbol(str(self.path), "compute", "two functions")
        self.assertFalse(refused.ok)

        rows = engine.log.session_patches(None, estat="rebutjat")
        self.assertTrue(rows, "the refusal was not logged")
        outcome = engine._undo(rows[0])
        engine.close()

        self.assertFalse(outcome.ok)
        self.assertIn("never applied", outcome.message)
