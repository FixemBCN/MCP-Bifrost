"""
Test suite for MCP-Bifrost's switch-case surface: PhpAdapter.cases/find_case
in languages/php.py (backed by the switch-case pass at the end of
extract.php), the Case dataclass in languages/__init__.py, check_case_set in
gates.py, and engine.insert_case (including inside patch_group).

stdlib unittest only. Every assertion here is meant to be able to fail: where
an assertion would hold purely by construction of the code path under test,
it is not included. In particular, "slice a case out and put the exact same
bytes back at the exact same offsets reproduces the file" is a mathematical
identity for ANY pair of offsets (concatenating contiguous slices of a string
always reconstructs the string, correct boundaries or not) -- so it carries
no information about extraction correctness and is not asserted here. What
IS asserted instead is that each case's extracted bytes match an
independently-computed expected span (found by plain bytes.index() on the
fixture, never touching the adapter or extract.php), which fails if the
extractor's start/end boundaries are wrong.

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
from mcp_bifrost.gates import check_case_set  # noqa: E402
from mcp_bifrost.languages import ExtractionError  # noqa: E402
from mcp_bifrost.languages.php import PhpAdapter  # noqa: E402
from mcp_bifrost.worker import WorkerResult  # noqa: E402


# ===================================================================== #
#  Fixtures / helpers (duplicated from test_generation.py's style; that
#  file is never imported or modified)
# ===================================================================== #

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
    )


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


class FixedWorker:
    """Always returns the same successful output. Records every payload."""

    def __init__(self, out: str) -> None:
        self.out = out
        self.calls: list[dict] = []

    def run(self, payload: dict) -> WorkerResult:
        self.calls.append(payload)
        return ok_result(self.out)


class ScriptedWorker:
    """Returns a different result per call, keyed by payload['sym']. Raises
    loudly if asked for a sym it was not scripted for."""

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
    """Fails the test immediately if the engine ever reaches the worker."""

    def run(self, payload: dict) -> WorkerResult:  # pragma: no cover
        raise AssertionError(f"worker.run() must not be called, got {payload!r}")


class MutatesAnchorWorker:
    """Simulates another process editing the file's anchor region between
    resolution and the worker's return: it writes to `path` as a side
    effect of run(), inside the bytes the anchor covers, then returns a
    normal successful result."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def run(self, payload: dict) -> WorkerResult:
        content = self.path.read_bytes()
        mutated = content.replace(b"doCreate();", b"doCreateXX();", 1)
        assert mutated != content, "mutation fixture did not land"
        self.path.write_bytes(mutated)
        return ok_result(
            "case 'newx':\n    doNewX();\n    break;"
        )


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


def expected_span(src: bytes, marker: bytes, end_marker: bytes = b"break;",
                  search_from: int = 0) -> tuple[int, int]:
    """
    Compute a case's [start, end) span independently of the adapter, by
    plain byte search on the fixture text: from `marker` (e.g. b"case 'x':")
    through the end of the following `end_marker` occurrence.

    Used as ground truth to check extraction, instead of trusting numbers
    copied out of a run of the extractor itself.
    """
    start = src.index(marker, search_from)
    end = src.index(end_marker, start) + len(end_marker)
    return start, end


def line_of(src: bytes, offset: int) -> int:
    return src.count(b"\n", 0, offset) + 1


# ===================================================================== #
#  1. Basic extraction: labels, order, line numbers
# ===================================================================== #

BASIC_SWITCH_PHP = b"""<?php

function route(string $action): string
{
    switch ($action) {
        case 'create':
            doCreate();
            break;
        case 'read':
            doRead();
            break;
        case 'update':
            doUpdate();
            break;
        case 'delete':
            doDelete();
            break;
        default:
            doNothing();
            break;
    }
    return 'done';
}
"""


class CaseExtractionBasicTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "basic.php"
        self.path.write_bytes(BASIC_SWITCH_PHP)
        self.php = PhpAdapter()

    def tearDown(self):
        self._tmp.cleanup()

    def test_labels_order_and_line_numbers(self):
        cases = self.php.cases(self.path)
        self.assertEqual(
            [c.label for c in cases],
            ["create", "read", "update", "delete", "default"],
        )

        src = BASIC_SWITCH_PHP
        expected_starts = {
            "create": src.index(b"case 'create':"),
            "read": src.index(b"case 'read':"),
            "update": src.index(b"case 'update':"),
            "delete": src.index(b"case 'delete':"),
            "default": src.index(b"default:"),
        }
        for c in cases:
            with self.subTest(label=c.label):
                self.assertEqual(c.start_byte, expected_starts[c.label])
                self.assertEqual(c.start_line, line_of(src, c.start_byte))


class CaseExtractionContentByteExactTest(unittest.TestCase):
    """
    Point 2 of the spec, done honestly: literally slicing a block out of a
    string and splicing the SAME bytes back at the SAME offsets always
    reproduces the string -- that is true for any pair of offsets, correct
    or not, so it holds by construction and is deleted. What replaces it:
    each case's extracted bytes must match a span computed independently,
    by searching the fixture text directly rather than trusting the
    adapter's own numbers. This fails if extraction picks the wrong start
    or end.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "basic.php"
        self.path.write_bytes(BASIC_SWITCH_PHP)
        self.php = PhpAdapter()

    def tearDown(self):
        self._tmp.cleanup()

    def test_every_case_block_matches_an_independently_found_span(self):
        src = BASIC_SWITCH_PHP
        cases = {c.label: c for c in self.php.cases(self.path)}

        for label, marker in [
            ("create", b"case 'create':"), ("read", b"case 'read':"),
            ("update", b"case 'update':"), ("delete", b"case 'delete':"),
            ("default", b"default:"),
        ]:
            start, end = expected_span(src, marker)
            expected = src[start:end]
            with self.subTest(label=label):
                self.assertEqual(cases[label].extract(src), expected)


# ===================================================================== #
#  3. A case's end_byte stops at its own last statement, not the next case
# ===================================================================== #

GAP_COMMENT_PHP = b"""<?php

function route(string $x): void
{
    switch ($x) {
        case 'alpha':
            doAlpha();
            break;

        // ----- BETA SECTION -----
        case 'beta':
            doBeta();
            break;
    }
}
"""


class CaseExtractionBoundaryExcludesGapCommentTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "gap.php"
        self.path.write_bytes(GAP_COMMENT_PHP)
        self.php = PhpAdapter()

    def tearDown(self):
        self._tmp.cleanup()

    def test_section_comment_belongs_to_the_case_that_follows_it(self):
        cases = self.php.cases(self.path)
        self.assertEqual([c.label for c in cases], ["alpha", "beta"])
        alpha, beta = cases

        alpha_block = alpha.extract(GAP_COMMENT_PHP)
        self.assertNotIn(b"BETA SECTION", alpha_block)
        self.assertTrue(alpha_block.rstrip().endswith(b"break;"))

        # The comment is real content sitting in the gap between the two
        # cases -- proving it wasn't simply dropped, only correctly
        # assigned to what comes after it.
        gap = GAP_COMMENT_PHP[alpha.end_byte:beta.start_byte]
        self.assertIn(b"BETA SECTION", gap)


# ===================================================================== #
#  4. Multi-byte UTF-8 before the switch must not shift byte offsets
# ===================================================================== #

UTF8_PREFIX_SWITCH_PHP = (
    "<?php\n"
    "// Comentari amb accents: àéíóú ç ñ "
    "i un guió llarg — per assegurar offsets en bytes\n"
    "function route3(string $action): string\n"
    "{\n"
    "    switch ($action) {\n"
    "        case 'x':\n"
    "            doX();\n"
    "            break;\n"
    "        case 'y':\n"
    "            doY();\n"
    "            break;\n"
    "    }\n"
    "    return 'done';\n"
    "}\n"
).encode("utf-8")


class CaseExtractionUtf8OffsetTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "utf8.php"
        self.path.write_bytes(UTF8_PREFIX_SWITCH_PHP)
        self.php = PhpAdapter()

    def tearDown(self):
        self._tmp.cleanup()

    def test_trap_is_armed_byte_length_differs_from_character_length(self):
        # If this fails the fixture no longer contains multi-byte
        # characters and the test below proves nothing about byte vs.
        # char indexing.
        as_text = UTF8_PREFIX_SWITCH_PHP.decode("utf-8")
        self.assertNotEqual(len(UTF8_PREFIX_SWITCH_PHP), len(as_text))

    def test_extraction_is_still_byte_exact_past_the_multibyte_prefix(self):
        src = UTF8_PREFIX_SWITCH_PHP
        cases = {c.label: c for c in self.php.cases(self.path)}
        self.assertEqual(sorted(cases), ["x", "y"])

        for label, marker in [("x", b"case 'x':"), ("y", b"case 'y':")]:
            start, end = expected_span(src, marker)
            with self.subTest(label=label):
                self.assertEqual(cases[label].start_byte, start)
                self.assertEqual(cases[label].extract(src), src[start:end])


# ===================================================================== #
#  5. Fall-through cases: document actual behaviour, not an assumption
# ===================================================================== #

FALLTHROUGH_PHP = b"""<?php

function route(string $x): void
{
    switch ($x) {
        case 'a':
        case 'b':
            doThing();
            break;
        case 'c':
            doOther();
            break;
    }
}
"""


class CaseExtractionFallthroughTest(unittest.TestCase):
    """
    Actual observed behaviour (verified by running extract.php on this
    fixture directly): the extractor closes the PRECEDING label as soon as
    it hits the next `case` token, and the boundary-closing search stops at
    the last non-whitespace/comment token before that -- which, for a
    fall-through label with nothing between it and the next `case`, is its
    own trailing ':'. So:
      - 'a' extracts as just "case 'a':" (empty body, no crash)
      - 'b' extracts the whole shared body: "case 'b': doThing(); break;"
      - 'c' is unaffected
    This is documented as fact, not asserted as "correct" -- a reader who
    disagrees with the behaviour should change the code, and this test
    should then be updated to match.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "fallthrough.php"
        self.path.write_bytes(FALLTHROUGH_PHP)
        self.php = PhpAdapter()

    def tearDown(self):
        self._tmp.cleanup()

    def test_does_not_crash_and_labels_are_all_found(self):
        cases = self.php.cases(self.path)
        self.assertEqual([c.label for c in cases], ["a", "b", "c"])

    def test_the_fallen_through_label_gets_an_empty_body(self):
        src = FALLTHROUGH_PHP
        cases = {c.label: c for c in self.php.cases(self.path)}
        a_block = cases["a"].extract(src)
        self.assertEqual(a_block, b"case 'a':")
        self.assertNotIn(b"doThing", a_block)

    def test_the_landing_label_gets_the_whole_shared_body(self):
        src = FALLTHROUGH_PHP
        cases = {c.label: c for c in self.php.cases(self.path)}
        b_block = cases["b"].extract(src)
        self.assertIn(b"doThing", b_block)
        self.assertTrue(b_block.rstrip().endswith(b"break;"))
        start, end = expected_span(src, b"case 'c':")
        self.assertEqual(cases["c"].extract(src), src[start:end])


# ===================================================================== #
#  6. Nested switch inside a case: document actual behaviour (a real bug)
# ===================================================================== #

DUP_LABEL_PHP = b"""<?php
switch ($action) {
    case 'a':
        doAlpha();
        break;
    case 'beta':
        doBeta();
        break;
    case 'a':
        doAlphaAgain();
        break;
}
"""

NESTED_SWITCH_PHP = b"""<?php

function route(string $x, string $y): void
{
    switch ($x) {
        case 'outer1':
            switch ($y) {
                case 'inner1':
                    doInner1();
                    break;
                case 'inner2':
                    doInner2();
                    break;
            }
            break;
        case 'outer2':
            doOuter2();
            break;
    }
}
"""


class CaseExtractionNestedSwitchTest(unittest.TestCase):
    """
    Regression: a nested switch used to corrupt the case list.

    The pass kept one stack of "currently open switch", pushed on every
    T_SWITCH but never popped when a switch's own closing brace was reached —
    popping happened once, in bulk, after the whole file had been scanned. So
    while the top of the stack still belonged to the INNER switch, every
    `case` that textually followed it, including ones lexically in the OUTER
    switch, was attributed to the inner slot. The outer branches after a
    nested switch were silently dropped.

    Silently is the operative word: nothing raised, the file parsed, and the
    only symptom was `find_case` reporting a label that plainly exists as
    unknown.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "nested.php"
        self.path.write_bytes(NESTED_SWITCH_PHP)
        self.adapter = PhpAdapter()

    def test_every_case_is_listed_once(self):
        labels = [c.label for c in self.adapter.cases(self.path)]
        self.assertEqual(labels, ["outer1", "inner1", "inner2", "outer2"])

    def test_outer_case_after_a_nested_switch_is_resolvable(self):
        case = self.adapter.find_case(self.path, "outer2")
        self.assertEqual(case.label, "outer2")
        body = case.extract(self.path.read_bytes())
        self.assertIn(b"doOuter2();", body)
        self.assertNotIn(b"case ", body[len(b"case 'outer2':"):])

    def test_outer1_contains_its_nested_switch_but_not_its_sibling(self):
        body = self.adapter.find_case(
            self.path, "outer1").extract(self.path.read_bytes())
        # The nested switch IS part of outer1's body — that is correct.
        self.assertIn(b"case 'inner1':", body)
        # Its sibling branch is not.
        self.assertNotIn(b"case 'outer2':", body)

    def test_inner_case_stops_inside_its_own_switch(self):
        body = self.adapter.find_case(
            self.path, "inner2").extract(self.path.read_bytes())
        self.assertIn(b"doInner2();", body)
        # Must not bleed past the inner switch's closing brace into the
        # outer branch's trailing `break;`.
        self.assertNotIn(b"}", body)


class FindCaseErrorsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.php = PhpAdapter()

    def tearDown(self):
        self._tmp.cleanup()

    def test_unknown_label_raises_extraction_error(self):
        path = Path(self._tmp.name) / "basic.php"
        path.write_bytes(BASIC_SWITCH_PHP)
        with self.assertRaises(ExtractionError) as ctx:
            self.php.find_case(path, "does-not-exist")
        self.assertIn("does-not-exist", str(ctx.exception))

    def test_duplicated_label_raises_and_names_both_line_numbers(self):
        path = Path(self._tmp.name) / "dup.php"
        path.write_bytes(DUP_LABEL_PHP)

        cases = [c for c in self.php.cases(path) if c.label == "a"]
        self.assertEqual(len(cases), 2)
        line_a, line_b = cases[0].start_line, cases[1].start_line
        self.assertNotEqual(line_a, line_b)

        with self.assertRaises(ExtractionError) as ctx:
            self.php.find_case(path, "a")
        msg = str(ctx.exception)
        self.assertIn(str(line_a), msg)
        self.assertIn(str(line_b), msg)


# ===================================================================== #
#  8. A file with no switch returns an empty case list
# ===================================================================== #

NO_SWITCH_PHP = b"""<?php

function plain(): int
{
    return 1;
}
"""


class CaseExtractionNoSwitchTest(unittest.TestCase):
    def test_empty_list_for_a_file_with_no_switch(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "plain.php"
            path.write_bytes(NO_SWITCH_PHP)
            self.assertEqual(PhpAdapter().cases(path), [])


# ===================================================================== #
#  9. check_case_set
# ===================================================================== #

class CheckCaseSetTest(unittest.TestCase):
    def test_passes_with_exactly_n_new_labels(self):
        before = ["create", "read", "update"]
        after_one = ["create", "read", "update", "delete"]
        self.assertTrue(check_case_set(before, after_one, expected_new=1))

        after_two = ["create", "read", "update", "delete", "list"]
        self.assertTrue(check_case_set(before, after_two, expected_new=2))

    def test_fails_when_a_label_disappears(self):
        before = ["create", "read", "update"]
        after = ["create", "read"]  # 'update' vanished, nothing gained
        result = check_case_set(before, after, expected_new=1)
        self.assertFalse(result)
        self.assertEqual(result.gate, "case_set")
        self.assertIn("update", result.detail)

    def test_fails_when_the_new_count_is_wrong(self):
        before = ["create", "read"]
        after = ["create", "read", "update", "delete"]  # 2 new, 1 expected
        result = check_case_set(before, after, expected_new=1)
        self.assertFalse(result)
        self.assertEqual(result.gate, "case_set")

    def test_fails_when_the_resulting_list_has_a_duplicate(self):
        before = ["create", "read"]
        after = ["create", "read", "read"]  # duplicate, and 0 NEW labels
        result = check_case_set(before, after, expected_new=1)
        self.assertFalse(result)
        self.assertEqual(result.gate, "case_set")
        self.assertIn("read", result.detail)


# ===================================================================== #
#  10-14. engine.insert_case
# ===================================================================== #

THREE_CASE_SWITCH_PHP = b"""<?php

function route(string $action): string
{
    switch ($action) {
        case 'create':
            doCreate();
            break;
        case 'read':
            doRead();
            break;
        case 'update':
            doUpdate();
            break;
    }
    return 'done';
}
"""


class InsertCaseOrderTest(EngineTestBase):
    def test_new_case_lands_between_anchor_and_the_next_one(self):
        path = commit_file(self.repo, "router.php", THREE_CASE_SWITCH_PHP)
        worker = FixedWorker(
            "case 'newone':\n    doNew();\n    break;"
        )
        engine = self.make_engine(worker)

        outcome = engine.insert_case(str(path), "create", "add newone")
        self.assertTrue(outcome.ok, outcome.message)

        order = [c.label for c in self.php.cases(path)]
        self.assertEqual(order, ["create", "newone", "read", "update"])

        ok, msg = self.php.validate(path.read_bytes())
        self.assertTrue(ok, msg)
        engine.close()


class InsertCaseDuplicateLabelRejectedTest(EngineTestBase):
    """
    The gate's whole purpose: a worker that returns a case whose label
    already exists produces a file that still parses -- php -l has nothing
    to say about a duplicate `case` label -- so only check_case_set catches
    it. The companion test below proves the candidate really does parse,
    so this test is known to be exercising the case_set gate and not the
    syntax gate.
    """

    def test_duplicate_label_is_rejected_and_file_restored(self):
        path = commit_file(self.repo, "router.php", THREE_CASE_SWITCH_PHP)
        original = path.read_bytes()
        worker = FixedWorker(
            "case 'read':\n    doReadAgain();\n    break;"
        )
        engine = self.make_engine(worker)

        outcome = engine.insert_case(str(path), "create", "duplicate read")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.gate, "case_set")
        self.assertEqual(path.read_bytes(), original)
        engine.close()

    def test_fixture_actually_still_parses(self):
        path = commit_file(self.repo, "router2.php", THREE_CASE_SWITCH_PHP)
        php = PhpAdapter()
        anc = php.find_case(path, "create")
        source = path.read_bytes()
        block = (
            b"\n\n        case 'read':\n"
            b"            doReadAgain();\n            break;"
        )
        candidate = source[:anc.end_byte] + block + source[anc.end_byte:]
        ok, msg = php.validate(candidate)
        self.assertTrue(ok, msg)


class InsertCaseUnknownAnchorTest(EngineTestBase):
    def test_unknown_anchor_is_rejected_before_touching_the_worker(self):
        path = commit_file(self.repo, "router.php", THREE_CASE_SWITCH_PHP)
        engine = self.make_engine(NeverCalledWorker())

        outcome = engine.insert_case(str(path), "does-not-exist", "add a case")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.gate, "resolve")
        self.assertEqual(path.read_bytes(), THREE_CASE_SWITCH_PHP)
        engine.close()


class InsertCaseStaleAnchorTest(EngineTestBase):
    def test_stale_anchor_is_rejected_and_file_left_as_mutated(self):
        path = commit_file(self.repo, "router.php", THREE_CASE_SWITCH_PHP)
        worker = MutatesAnchorWorker(path)
        engine = self.make_engine(worker)

        outcome = engine.insert_case(str(path), "create", "add a case")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.gate, "offsets")

        # The engine must not have written anything of its own on top of
        # the external mutation.
        expected = THREE_CASE_SWITCH_PHP.replace(
            b"doCreate();", b"doCreateXX();", 1
        )
        self.assertEqual(path.read_bytes(), expected)
        engine.close()


class InsertCasePatchGroupTest(EngineTestBase):
    def test_insert_case_succeeds_as_one_step_of_a_group(self):
        path = commit_file(self.repo, "router.php", THREE_CASE_SWITCH_PHP)
        worker = ScriptedWorker({
            "new switch case after 'create'": ok_result(
                "case 'newone':\n    doNew();\n    break;"
            ),
        })
        engine = self.make_engine(worker)

        outcome = engine.patch_group([
            {"op": "insert_case", "file_path": str(path),
             "after_case": "create", "instruction": "add newone"},
        ])
        self.assertTrue(outcome.ok, outcome.message)

        order = [c.label for c in self.php.cases(path)]
        self.assertEqual(order, ["create", "newone", "read", "update"])
        engine.close()

    def test_insert_case_is_rolled_back_when_a_later_step_fails(self):
        path = commit_file(self.repo, "router.php", THREE_CASE_SWITCH_PHP)
        original = path.read_bytes()
        worker = ScriptedWorker({
            "new switch case after 'create'": ok_result(
                "case 'newone':\n    doNew();\n    break;"
            ),
        })
        engine = self.make_engine(worker)

        outcome = engine.patch_group([
            {"op": "insert_case", "file_path": str(path),
             "after_case": "create", "instruction": "add newone"},
            {"op": "not_a_real_operation", "file_path": str(path)},
        ])

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.gate, "group")
        self.assertEqual(path.read_bytes(), original)

        order = [c.label for c in self.php.cases(path)]
        self.assertEqual(order, ["create", "read", "update"])
        engine.close()


if __name__ == "__main__":
    unittest.main()
