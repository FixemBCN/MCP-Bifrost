"""
Test suite for MCP-Bifrost's generation surface: insert_symbol, create_file,
and patch_group in engine.py, plus the gates that exist specifically to
police them (check_symbol_set, check_absent, check_nonempty).

stdlib unittest only. Every assertion here is meant to be able to fail: where
an assertion would hold purely by construction of the code path under test,
it is not included.

NEVER makes a real network call: every worker used here is a local stub
returning WorkerResult directly.

Run: python3 -m unittest discover tests -v   (from the repo root)
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

from mcp_bifrost.engine import Engine, Outcome  # noqa: E402
from mcp_bifrost.gates import (  # noqa: E402
    check_absent,
    check_nonempty,
    check_symbol_set,
)
from mcp_bifrost.languages.php import PhpAdapter  # noqa: E402
from mcp_bifrost.worker import WorkerResult  # noqa: E402


# ===================================================================== #
#  Fixtures / helpers shared by the whole file
# ===================================================================== #

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
    )


def make_git_repo(root: Path) -> Path:
    """Init a git repo at `root` with a local identity."""
    proc = _git(root, "init", "-q")
    assert proc.returncode == 0, proc.stderr
    _git(root, "config", "user.email", "bifrost-tests@example.com")
    _git(root, "config", "user.name", "Bifrost Tests")
    return root


def commit_file(repo: Path, rel_path: str, content: bytes) -> Path:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    proc = _git(repo, "add", rel_path)
    assert proc.returncode == 0, proc.stderr
    proc = _git(repo, "commit", "-q", "-m", "fixture")
    assert proc.returncode == 0, proc.stderr
    return path


def ok_result(out: str) -> WorkerResult:
    return WorkerResult(
        ok=True, out=out, why="stub", diff_stat="+0/-0", error=None, ms=1,
        tokens_in=5, tokens_out=5, cache_hit=0, request_bytes=0,
        response_bytes=0,
    )


def error_result(error: str) -> WorkerResult:
    return WorkerResult(
        ok=False, out=None, why=None, diff_stat=None, error=error, ms=1,
        tokens_in=0, tokens_out=0, cache_hit=0, request_bytes=0,
        response_bytes=0,
    )


class FixedWorker:
    """Always returns the same successful output. Records every payload."""

    def __init__(self, out: str) -> None:
        self.out = out
        self.calls: list[dict] = []

    def run(self, payload: dict) -> WorkerResult:
        self.calls.append(payload)
        return ok_result(self.out)


class EchoWorker:
    """Returns payload['src'] verbatim. Used for no-op fix_symbol legs of a
    patch_group, so the operation is guaranteed to keep exactly one valid
    symbol without hand-crafting a transform."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, payload: dict) -> WorkerResult:
        self.calls.append(payload)
        return ok_result(payload["src"])


class ScriptedWorker:
    """Returns a different result per call, keyed by payload['sym']. Raises
    loudly if asked for a sym it was not scripted for, so a mis-keyed test
    fails with a clear message instead of silently reusing the wrong
    fixture."""

    def __init__(self, by_sym: dict[str, WorkerResult]) -> None:
        self.by_sym = by_sym
        self.calls: list[dict] = []

    def run(self, payload: dict) -> WorkerResult:
        self.calls.append(payload)
        sym = payload["sym"]
        if sym not in self.by_sym:
            raise AssertionError(
                f"ScriptedWorker got unscripted sym {sym!r}; "
                f"known: {sorted(self.by_sym)}"
            )
        return self.by_sym[sym]


class NeverCalledWorker:
    """Fails the test immediately if the engine ever reaches the worker.
    Used to prove an input-validation gate rejects before any send."""

    def run(self, payload: dict) -> WorkerResult:  # pragma: no cover
        raise AssertionError(f"worker.run() must not be called, got {payload!r}")


class EngineTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = make_git_repo(Path(self._tmp.name))
        self.php = PhpAdapter()

    def tearDown(self):
        self._tmp.cleanup()

    def make_engine(self, worker, **kwargs) -> Engine:
        db_path = Path(self._tmp.name) / "log.sqlite3"
        kwargs.setdefault("entropy_scan", False)
        return Engine(worker=worker, db_path=db_path, **kwargs)


# ===================================================================== #
#  1. check_symbol_set
# ===================================================================== #

class CheckSymbolSetTest(unittest.TestCase):
    def test_passes_when_exactly_n_new_symbols_appear(self):
        before = ["A::a", "A::b"]
        after = ["A::a", "A::b", "A::c"]
        self.assertTrue(check_symbol_set(before, after, expected_new=1))

        after_two = ["A::a", "A::b", "A::c", "A::d"]
        self.assertTrue(check_symbol_set(before, after_two, expected_new=2))

    def test_fails_when_an_existing_symbol_disappears(self):
        before = ["A::a", "A::b"]
        after = ["A::a"]  # A::b vanished, and nothing was gained
        result = check_symbol_set(before, after, expected_new=1)
        self.assertFalse(result)
        self.assertEqual(result.gate, "symbol_set")
        self.assertIn("A::b", result.detail)

    def test_fails_when_zero_new_symbols_appear_but_one_expected(self):
        before = ["A::a"]
        after = ["A::a"]
        result = check_symbol_set(before, after, expected_new=1)
        self.assertFalse(result)
        self.assertEqual(result.gate, "symbol_set")

    def test_fails_when_two_new_symbols_appear_but_one_expected(self):
        before = ["A::a"]
        after = ["A::a", "A::b", "A::c"]
        result = check_symbol_set(before, after, expected_new=1)
        self.assertFalse(result)
        self.assertEqual(result.gate, "symbol_set")


# ===================================================================== #
#  2. check_absent
# ===================================================================== #

class CheckAbsentTest(unittest.TestCase):
    def test_passes_for_a_missing_path(self):
        with tempfile.TemporaryDirectory() as d:
            missing = Path(d) / "does-not-exist.php"
            self.assertTrue(check_absent(missing.exists(), str(missing)))

    def test_fails_for_an_existing_path(self):
        with tempfile.TemporaryDirectory() as d:
            present = Path(d) / "already-here.php"
            present.write_bytes(b"<?php\n")
            result = check_absent(present.exists(), str(present))
            self.assertFalse(result)
            self.assertEqual(result.gate, "absent")


# ===================================================================== #
#  3. check_nonempty
# ===================================================================== #

class CheckNonemptyTest(unittest.TestCase):
    def setUp(self):
        self.php = PhpAdapter()

    def test_fails_for_empty_and_php_opening_tag_only_variants(self):
        bad = [b"", b"\n", b"<?php", b"<?php\n"]
        for content in bad:
            with self.subTest(content=content):
                result = check_nonempty(self.php, content)
                self.assertFalse(result)
                self.assertEqual(result.gate, "nonempty")

    def test_passes_for_a_real_class(self):
        content = (
            b"<?php\n\nclass Greeter\n{\n"
            b"    public function greet(): string\n    {\n"
            b"        return 'hi';\n    }\n}\n"
        )
        result = check_nonempty(self.php, content)
        self.assertTrue(result)

    def test_php_l_itself_accepts_the_empty_file_this_gate_exists_for(self):
        # The reason check_nonempty must exist as a SEPARATE gate: syntax
        # validation alone waves an empty file through.
        ok, msg = self.php.validate(b"<?php\n")
        self.assertTrue(ok, msg)


# ===================================================================== #
#  4-8. insert_symbol
# ===================================================================== #

THREE_METHOD_PHP = b"""<?php

class Widget
{
    public function methodOne(): int
    {
        return 1;
    }

    public function methodTwo(): int
    {
        return 2;
    }

    public function methodThree(): int
    {
        return 3;
    }
}
"""

TWO_METHOD_PHP = b"""<?php

class Foo
{
    public function methodOne()
    {
        return 1;
    }

    function methodTwo()
    {
        return 2;
    }
}
"""


class InsertSymbolOrderTest(EngineTestBase):
    def test_position_after_lands_between_anchor_and_next_symbol(self):
        path = commit_file(self.repo, "widget.php", THREE_METHOD_PHP)
        worker = FixedWorker(
            "public function newAfter(): int\n{\n    return 42;\n}"
        )
        engine = self.make_engine(worker)

        outcome = engine.insert_symbol(
            str(path), "Widget::methodTwo", "after", "add a helper"
        )
        self.assertTrue(outcome.ok, outcome.message)

        order = [s.fqn for s in self.php.symbols(path)]
        self.assertEqual(
            order,
            ["Widget::methodOne", "Widget::methodTwo", "Widget::newAfter",
             "Widget::methodThree"],
        )
        engine.close()

    def test_position_before_lands_before_the_anchor(self):
        path = commit_file(self.repo, "widget.php", THREE_METHOD_PHP)
        worker = FixedWorker(
            "public function newBefore(): int\n{\n    return 42;\n}"
        )
        engine = self.make_engine(worker)

        outcome = engine.insert_symbol(
            str(path), "Widget::methodTwo", "before", "add a helper"
        )
        self.assertTrue(outcome.ok, outcome.message)

        order = [s.fqn for s in self.php.symbols(path)]
        self.assertEqual(
            order,
            ["Widget::methodOne", "Widget::newBefore", "Widget::methodTwo",
             "Widget::methodThree"],
        )
        engine.close()


class InsertSymbolValidityTest(EngineTestBase):
    def test_result_parses_and_every_preexisting_symbol_survives(self):
        path = commit_file(self.repo, "widget.php", THREE_METHOD_PHP)
        before = {s.fqn for s in self.php.symbols(path)}
        worker = FixedWorker(
            "public function newAfter(): int\n{\n    return 42;\n}"
        )
        engine = self.make_engine(worker)

        outcome = engine.insert_symbol(
            str(path), "Widget::methodTwo", "after", "add a helper"
        )
        self.assertTrue(outcome.ok, outcome.message)

        ok, msg = self.php.validate(path.read_bytes())
        self.assertTrue(ok, msg)

        after = {s.fqn for s in self.php.symbols(path)}
        self.assertTrue(before.issubset(after), before - after)
        engine.close()


class InsertSymbolStaleAnchorTest(EngineTestBase):
    class _MutatesUnderneathWorker:
        """Simulates another process editing the file's anchor region
        between resolution and the worker's return: it writes to `path`
        as a side effect of run(), inside the very bytes the anchor
        covers, then returns a normal successful result."""

        def __init__(self, path: Path) -> None:
            self.path = path

        def run(self, payload: dict) -> WorkerResult:
            content = self.path.read_bytes()
            mutated = content.replace(b"return 2;", b"return 22;", 1)
            assert mutated != content, "mutation fixture did not land"
            self.path.write_bytes(mutated)
            return ok_result("public function newX(): int\n{\n    return 1;\n}")

    def test_stale_anchor_is_rejected_and_file_left_as_mutated(self):
        path = commit_file(self.repo, "widget.php", THREE_METHOD_PHP)
        worker = self._MutatesUnderneathWorker(path)
        engine = self.make_engine(worker)

        outcome = engine.insert_symbol(
            str(path), "Widget::methodTwo", "after", "add a helper"
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.gate, "offsets")

        # The engine must not have written anything of its own on top of
        # the external mutation: the file is exactly what the "other
        # process" left it as.
        expected = THREE_METHOD_PHP.replace(b"return 2;", b"return 22;", 1)
        self.assertEqual(path.read_bytes(), expected)
        engine.close()


class InsertSymbolClosesClassEarlyTest(EngineTestBase):
    """
    The gate's whole purpose: a block that closes the enclosing class one
    brace early re-parents everything after it into a class of the worker's
    own invention. The reconstructed file still parses -- php -l has nothing
    to say about it -- so only check_symbol_set catches it, by noticing that
    Foo::methodTwo no longer exists under that name.
    """

    def test_extra_closing_brace_is_rejected_and_file_restored(self):
        path = commit_file(self.repo, "foo.php", TWO_METHOD_PHP)
        original = path.read_bytes()

        # Closes class Foo one brace early, then opens a brand-new class
        # "Ghost" that swallows the rest of the original file (methodTwo,
        # and the file's own final brace). Verified by hand: the resulting
        # candidate parses cleanly under php -l, and Foo::methodTwo is
        # gone from the post-insertion symbol map (it reappears as
        # Ghost::methodTwo instead).
        worker = FixedWorker(
            "}\n\nclass Ghost\n{\n"
            "    public function extraFn()\n    {\n        return 99;\n    }"
        )
        engine = self.make_engine(worker)

        outcome = engine.insert_symbol(
            str(path), "Foo::methodOne", "after", "add a helper"
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.gate, "symbol_set")
        self.assertEqual(path.read_bytes(), original)
        engine.close()

    def test_fixture_actually_still_parses(self):
        # Arms the test above: if this candidate did NOT parse, the syntax
        # gate would reject it first and the symbol_set test would prove
        # nothing about the gate it claims to exercise.
        path = commit_file(self.repo, "foo2.php", TWO_METHOD_PHP)
        php = PhpAdapter()
        anc = php.find(path, "Foo::methodOne")
        source = path.read_bytes()
        block = (
            b"}\n\nclass Ghost\n{\n"
            b"    public function extraFn()\n    {\n        return 99;\n    }\n"
        )
        candidate = source[:anc.end_byte] + block + source[anc.end_byte:]
        ok, msg = php.validate(candidate)
        self.assertTrue(ok, msg)


class InsertSymbolInvalidPositionTest(EngineTestBase):
    def test_bad_position_is_rejected_before_touching_the_worker(self):
        path = commit_file(self.repo, "widget.php", THREE_METHOD_PHP)
        engine = self.make_engine(NeverCalledWorker())

        outcome = engine.insert_symbol(
            str(path), "Widget::methodTwo", "sideways", "add a helper"
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.gate, "input")
        self.assertEqual(path.read_bytes(), THREE_METHOD_PHP)
        engine.close()


# ===================================================================== #
#  9-12. create_file
# ===================================================================== #

class CreateFileSuccessTest(EngineTestBase):
    def test_creates_file_with_worker_content_and_trailing_newline(self):
        target = Path(self.repo) / "fresh.php"
        worker = FixedWorker(
            "<?php\n\nclass Fresh\n{\n"
            "    public function hi(): string\n    {\n        return 'hi';\n    }\n}"
        )
        engine = self.make_engine(worker)

        outcome = engine.create_file(str(target), "create a Fresh class")
        self.assertTrue(outcome.ok, outcome.message)
        self.assertTrue(target.is_file())

        content = target.read_bytes()
        self.assertTrue(content.endswith(b"\n"))
        self.assertIn(b"class Fresh", content)
        engine.close()


class CreateFileRefusesExistingTest(EngineTestBase):
    def test_refuses_when_target_exists_and_leaves_it_untouched(self):
        original = b"<?php\n\nclass AlreadyHere {}\n"
        target = commit_file(self.repo, "already.php", original)
        worker = NeverCalledWorker()
        engine = self.make_engine(worker)

        outcome = engine.create_file(str(target), "overwrite it")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.gate, "absent")
        self.assertEqual(target.read_bytes(), original)
        engine.close()


class CreateFileRefusesEmptyTest(EngineTestBase):
    def test_refuses_empty_worker_response_and_creates_no_file(self):
        target = Path(self.repo) / "empty.php"
        worker = FixedWorker("")
        engine = self.make_engine(worker)

        outcome = engine.create_file(str(target), "create something")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.gate, "nonempty")
        self.assertFalse(target.exists())
        engine.close()


class CreateFileModelFromTest(EngineTestBase):
    def test_model_from_puts_full_exemplar_text_in_the_payload(self):
        exemplar_content = (
            b"<?php\n\nclass ExemplarSample\n{\n"
            b"    public function distinctiveMarkerXyz(): int\n    {\n"
            b"        return 7;\n    }\n}\n"
        )
        exemplar = commit_file(self.repo, "exemplar.php", exemplar_content)
        target = Path(self.repo) / "new_from_model.php"

        worker = FixedWorker(
            "<?php\n\nclass NewFromModel\n{\n"
            "    public function distinctiveMarkerXyz(): int\n    {\n"
            "        return 7;\n    }\n}"
        )
        engine = self.make_engine(worker)

        outcome = engine.create_file(
            str(target), "follow the exemplar", model_from=str(exemplar)
        )
        self.assertTrue(outcome.ok, outcome.message)
        self.assertEqual(len(worker.calls), 1)

        sent_ctx = "\n".join(worker.calls[0]["ctx"])
        self.assertIn(exemplar_content.decode("utf-8"), sent_ctx)
        engine.close()

    def test_missing_exemplar_path_is_rejected_as_input_error(self):
        target = Path(self.repo) / "new_from_missing.php"
        worker = NeverCalledWorker()
        engine = self.make_engine(worker)

        outcome = engine.create_file(
            str(target), "follow the exemplar",
            model_from=str(Path(self.repo) / "no-such-exemplar.php"),
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.gate, "input")
        self.assertFalse(target.exists())
        engine.close()


# ===================================================================== #
#  13-16. patch_group
# ===================================================================== #

TWO_FILE_A_PHP = b"""<?php

class Alpha
{
    public function foo(): int
    {
        return 1;
    }
}
"""


class PatchGroupAllSucceedTest(EngineTestBase):
    def test_all_operations_succeed_and_every_effect_is_present(self):
        path_a = commit_file(self.repo, "alpha.php", TWO_FILE_A_PHP)
        path_c = Path(self.repo) / "created.php"

        worker = ScriptedWorker({
            # op1: fix_symbol, no-op transform (echoed back verbatim).
            "Alpha::foo": ok_result(
                "public function foo(): int\n{\n    return 1;\n}"
            ),
            # op2: create_file.
            "created.php": ok_result(
                "<?php\n\nclass Created\n{\n"
                "    public function bar(): int\n    {\n        return 2;\n    }\n}"
            ),
            # op3: insert_symbol, adds a sibling to Alpha::foo.
            "new symbol after Alpha::foo": ok_result(
                "public function newSibling(): int\n{\n    return 3;\n}"
            ),
        })
        engine = self.make_engine(worker)

        outcome = engine.patch_group([
            {"op": "fix_symbol", "file_path": str(path_a),
             "symbol_name": "Alpha::foo", "instruction": "no-op"},
            {"op": "create_file", "file_path": str(path_c),
             "instruction": "create Created class"},
            {"op": "insert_symbol", "file_path": str(path_a),
             "anchor": "Alpha::foo", "position": "after",
             "instruction": "add a sibling"},
        ])

        self.assertTrue(outcome.ok, outcome.message)

        order = [s.fqn for s in self.php.symbols(path_a)]
        self.assertEqual(order, ["Alpha::foo", "Alpha::newSibling"])
        self.assertTrue(path_c.is_file())
        self.assertIn(b"class Created", path_c.read_bytes())
        engine.close()


class PatchGroupMidFailureRollsBackTest(EngineTestBase):
    def test_modified_file_restored_and_created_file_deleted(self):
        path_a = commit_file(self.repo, "alpha.php", TWO_FILE_A_PHP)
        original_a = path_a.read_bytes()
        path_c = Path(self.repo) / "created.php"
        path_b = commit_file(self.repo, "beta.php", TWO_FILE_A_PHP.replace(
            b"Alpha", b"Beta"
        ))

        worker = ScriptedWorker({
            # op1: fix_symbol succeeds, modifies alpha.php.
            "Alpha::foo": ok_result(
                "public function foo(): int\n{\n    // touched\n    return 1;\n}"
            ),
            # op2: create_file succeeds.
            "created.php": ok_result(
                "<?php\n\nclass Created\n{\n"
                "    public function bar(): int\n    {\n        return 2;\n    }\n}"
            ),
            # op3: fix_symbol fails gate 2 (returns two symbols instead of
            # one), so it never touches beta.php's contents.
            "Beta::foo": ok_result(
                "public function foo(): int\n{\n    return 1;\n}\n"
                "public function intruder(): int\n{\n    return 99;\n}"
            ),
        })
        engine = self.make_engine(worker)

        outcome = engine.patch_group([
            {"op": "fix_symbol", "file_path": str(path_a),
             "symbol_name": "Alpha::foo", "instruction": "touch it"},
            {"op": "create_file", "file_path": str(path_c),
             "instruction": "create Created class"},
            {"op": "fix_symbol", "file_path": str(path_b),
             "symbol_name": "Beta::foo", "instruction": "add a sibling"},
        ])

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.gate, "group")

        self.assertEqual(path_a.read_bytes(), original_a)
        self.assertFalse(path_c.exists())
        engine.close()


class PatchGroupAbortReasonsTest(EngineTestBase):
    def test_unknown_op_name_aborts_and_rolls_back(self):
        path_a = commit_file(self.repo, "alpha.php", TWO_FILE_A_PHP)
        original_a = path_a.read_bytes()
        worker = ScriptedWorker({
            "Alpha::foo": ok_result(
                "public function foo(): int\n{\n    // touched\n    return 1;\n}"
            ),
        })
        engine = self.make_engine(worker)

        outcome = engine.patch_group([
            {"op": "fix_symbol", "file_path": str(path_a),
             "symbol_name": "Alpha::foo", "instruction": "touch it"},
            {"op": "not_a_real_operation", "file_path": str(path_a)},
        ])

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.gate, "group")
        self.assertEqual(path_a.read_bytes(), original_a)
        engine.close()

    def test_missing_required_argument_aborts_and_rolls_back(self):
        path_a = commit_file(self.repo, "alpha.php", TWO_FILE_A_PHP)
        original_a = path_a.read_bytes()
        worker = ScriptedWorker({
            "Alpha::foo": ok_result(
                "public function foo(): int\n{\n    // touched\n    return 1;\n}"
            ),
        })
        engine = self.make_engine(worker)

        outcome = engine.patch_group([
            {"op": "fix_symbol", "file_path": str(path_a),
             "symbol_name": "Alpha::foo", "instruction": "touch it"},
            # missing "symbol_name": KeyError inside the dispatch lambda.
            {"op": "fix_symbol", "file_path": str(path_a),
             "instruction": "touch it again"},
        ])

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.gate, "group")
        self.assertEqual(path_a.read_bytes(), original_a)
        engine.close()


class PatchGroupReverseOrderRollbackTest(EngineTestBase):
    def test_two_sequential_inserts_into_the_same_file_roll_back_in_reverse(self):
        path = commit_file(self.repo, "widget.php", THREE_METHOD_PHP)
        original = path.read_bytes()

        worker = ScriptedWorker({
            "new symbol after Widget::methodOne": ok_result(
                "public function newA(): int\n{\n    return 10;\n}"
            ),
            "new symbol after Widget::methodTwo": ok_result(
                "public function newB(): int\n{\n    return 20;\n}"
            ),
        })
        engine = self.make_engine(worker)

        outcome = engine.patch_group([
            {"op": "insert_symbol", "file_path": str(path),
             "anchor": "Widget::methodOne", "position": "after",
             "instruction": "add newA"},
            {"op": "insert_symbol", "file_path": str(path),
             "anchor": "Widget::methodTwo", "position": "after",
             "instruction": "add newB"},
            {"op": "not_a_real_operation", "file_path": str(path)},
        ])

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.gate, "group")

        # If rollback ran in the WRONG order (oldest first), undoing op1
        # first would blow away op2's blob reference point, and undoing
        # op2 second would restore the post-op1 state (still carrying
        # newA) instead of the true original. Byte-identity here is only
        # achievable with correct reverse-order rollback.
        self.assertEqual(path.read_bytes(), original)
        engine.close()


if __name__ == "__main__":
    unittest.main()
