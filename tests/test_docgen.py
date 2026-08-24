"""
Tests for the changelog generator.

`docgen.py` had 23 of its 117 statements executed. It is the module that
turns the operational log into something a person reads, and the whole
argument for recording a rationale per patch — captured at zero cost to the
orchestrator's context — rests on it coming back out intact.

The log is built here through `PatchLog` rather than by writing SQL, so a
schema change breaks these tests instead of quietly changing what they
prove.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp_bifrost import docgen  # noqa: E402
from mcp_bifrost.log import PatchLog  # noqa: E402


class LogFixture:
    """A log with a known set of patches in a known order."""

    def __init__(self, root: Path):
        self.root = root
        self.db_path = root / "history.db"
        self.log = PatchLog(str(self.db_path), session="session-one")

    def add(self, **fields):
        defaults = dict(op="fix_symbol", fitxer=str(self.root / "src/a.py"),
                        simbol="K.a", estat="ok", instruccio="do the thing",
                        rationale="because of the thing", src_b=100, out_b=140)
        defaults.update(fields)
        return self.log.record(**defaults)

    def close(self):
        self.log.close()


class ReadTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.fixture = LogFixture(self.root)

    def tearDown(self):
        self.fixture.close()

    def test_a_missing_log_is_an_error_and_is_not_created(self):
        """sqlite would happily make an empty database and report nothing
        changed, which is indistinguishable from a session that did nothing."""
        missing = self.root / "nowhere" / "history.db"
        with self.assertRaises(FileNotFoundError):
            docgen.read(missing)
        self.assertFalse(missing.exists())

    def test_only_applied_patches_are_reported_by_default(self):
        self.fixture.add(simbol="K.applied")
        self.fixture.add(simbol="K.refused", estat="rebutjat", porta="single")
        self.fixture.add(simbol="K.failed", estat="error", porta="worker")

        symbols = [e.symbol for e in docgen.read(self.db())]
        self.assertEqual(["K.applied"], symbols)

    def test_rejections_can_be_asked_for(self):
        self.fixture.add(simbol="K.applied")
        self.fixture.add(simbol="K.refused", estat="rebutjat", porta="single")
        entries = docgen.read(self.db(), include_rejected=True)
        self.assertEqual({"K.applied", "K.refused"},
                         {e.symbol for e in entries})

    def test_entries_come_back_oldest_first(self):
        for i, ts in enumerate(["2026-01-03T00:00:00+00:00",
                                "2026-01-01T00:00:00+00:00",
                                "2026-01-02T00:00:00+00:00"]):
            self.fixture.add(simbol=f"K.m{i}", ts=ts)
        self.assertEqual(["K.m1", "K.m2", "K.m0"],
                         [e.symbol for e in docgen.read(self.db())])

    def test_the_since_filter_cuts_by_timestamp(self):
        self.fixture.add(simbol="K.old", ts="2026-01-01T00:00:00+00:00")
        self.fixture.add(simbol="K.new", ts="2026-06-01T00:00:00+00:00")
        entries = docgen.read(self.db(), since="2026-03-01")
        self.assertEqual(["K.new"], [e.symbol for e in entries])

    def test_the_session_filter_isolates_one_run(self):
        self.fixture.add(simbol="K.mine")
        self.fixture.add(simbol="K.theirs", session="session-two")
        entries = docgen.read(self.db(), session="session-one")
        self.assertEqual(["K.mine"], [e.symbol for e in entries])

    def test_the_file_filter_matches_on_a_fragment_of_the_path(self):
        self.fixture.add(simbol="K.here", fitxer=str(self.root / "src/a.py"))
        self.fixture.add(simbol="K.there", fitxer=str(self.root / "lib/b.py"))
        entries = docgen.read(self.db(), file_filter="lib/")
        self.assertEqual(["K.there"], [e.symbol for e in entries])

    def test_the_size_delta_is_carried_through(self):
        self.fixture.add(src_b=100, out_b=140)
        self.assertEqual(40, docgen.read(self.db())[0].diff_lines)

    def db(self) -> Path:
        return self.fixture.db_path


class ChangelogTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.fixture = LogFixture(self.root)

    def tearDown(self):
        self.fixture.close()

    def entries(self):
        return docgen.read(self.fixture.db_path)

    def test_an_empty_log_says_so_rather_than_producing_an_empty_document(self):
        text = docgen.as_changelog([], title="Nothing")
        self.assertIn("# Nothing", text)
        self.assertIn("No recorded changes", text)

    def test_the_rationale_reaches_the_document(self):
        self.fixture.add(instruccio="coerce both operands",
                         rationale="the API returns strings")
        text = docgen.as_changelog(self.entries(), root=self.root)
        self.assertIn("coerce both operands", text)
        self.assertIn("the API returns strings", text)

    def test_paths_are_shortened_against_the_root(self):
        self.fixture.add(fitxer=str(self.root / "src/a.py"))
        text = docgen.as_changelog(self.entries(), root=self.root)
        self.assertIn("## src/a.py", text)
        self.assertNotIn(str(self.root), text)

    def test_a_path_outside_the_root_is_left_alone(self):
        outside = Path(tempfile.mkdtemp()) / "elsewhere.py"
        self.fixture.add(fitxer=str(outside))
        text = docgen.as_changelog(self.entries(), root=self.root)
        self.assertIn(str(outside), text)

    def test_grouping_by_file_puts_each_file_under_its_own_heading(self):
        self.fixture.add(simbol="K.a", fitxer=str(self.root / "src/a.py"))
        self.fixture.add(simbol="K.b", fitxer=str(self.root / "src/b.py"))
        text = docgen.as_changelog(self.entries(), root=self.root,
                                   group="file")
        self.assertIn("## src/a.py", text)
        self.assertIn("## src/b.py", text)

    def test_grouping_by_session_puts_each_run_under_its_own_heading(self):
        self.fixture.add(simbol="K.a")
        self.fixture.add(simbol="K.b", session="session-two")
        text = docgen.as_changelog(self.entries(), root=self.root,
                                   group="session")
        self.assertIn("## session-one", text)
        self.assertIn("## session-two", text)

    def test_the_verb_follows_the_operation(self):
        for op, verb in (("fix_symbol", "Changed"), ("insert_symbol", "Added"),
                         ("create_file", "Created"), ("insert_case", "Added")):
            with self.subTest(op=op):
                fixture = LogFixture(Path(tempfile.mkdtemp()))
                fixture.add(op=op)
                text = docgen.as_changelog(docgen.read(fixture.db_path))
                fixture.close()
                self.assertIn(f"**{verb} ", text)

    def test_a_growing_change_is_signed_and_a_shrinking_one_is_not(self):
        self.fixture.add(simbol="K.grew", src_b=10, out_b=30)
        self.fixture.add(simbol="K.shrank", src_b=30, out_b=10)
        text = docgen.as_changelog(self.entries())
        self.assertIn("(+20 bytes)", text)
        self.assertIn("(-20 bytes)", text)


class CommitMessageTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.fixture = LogFixture(self.root)

    def tearDown(self):
        self.fixture.close()

    def entries(self):
        return docgen.read(self.fixture.db_path)

    def test_the_subject_stays_the_callers(self):
        """A machine summarising a batch into one line produces 'Update 12
        methods', which is the commit message everyone complains about."""
        self.fixture.add()
        message = docgen.as_commit_message(self.entries(), "Coerce operands",
                                           root=self.root)
        self.assertEqual("Coerce operands", message.splitlines()[0])

    def test_every_change_gets_a_line_carrying_its_reason(self):
        self.fixture.add(simbol="K.a", rationale="the API returns strings")
        self.fixture.add(simbol="K.b", rationale="the same, one level down")
        message = docgen.as_commit_message(self.entries(), "Subject",
                                           root=self.root)
        self.assertIn("* K.a: the API returns strings", message)
        self.assertIn("* K.b: the same, one level down", message)

    def test_it_counts_the_files_it_touched(self):
        self.fixture.add(fitxer=str(self.root / "src/a.py"))
        self.fixture.add(fitxer=str(self.root / "src/b.py"))
        message = docgen.as_commit_message(self.entries(), "Subject",
                                           root=self.root)
        self.assertIn("2 change(s) across 2 file(s)", message)

    def test_without_a_rationale_it_falls_back_to_what_was_asked(self):
        self.fixture.add(rationale=None, instruccio="coerce both operands")
        message = docgen.as_commit_message(self.entries(), "Subject",
                                           root=self.root)
        self.assertIn("coerce both operands", message)

    def test_an_empty_session_is_just_the_subject(self):
        self.assertEqual("Subject", docgen.as_commit_message([], "Subject"))


class SummariseTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.fixture = LogFixture(self.root)

    def tearDown(self):
        self.fixture.close()

    def test_nothing_recorded_says_nothing_recorded(self):
        self.assertEqual("no recorded changes", docgen.summarise([]))

    def test_it_counts_changes_files_and_operations(self):
        self.fixture.add(op="fix_symbol", fitxer=str(self.root / "a.py"))
        self.fixture.add(op="fix_symbol", fitxer=str(self.root / "a.py"))
        self.fixture.add(op="insert_symbol", fitxer=str(self.root / "b.py"))
        line = docgen.summarise(docgen.read(self.fixture.db_path))
        self.assertIn("3 change(s)", line)
        self.assertIn("2 file(s)", line)
        self.assertIn("2 fix_symbol", line)
        self.assertIn("1 insert_symbol", line)
