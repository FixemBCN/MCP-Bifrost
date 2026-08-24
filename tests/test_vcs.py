"""
Tests for the VCS layer.

`vcs.py` had 19 of its 67 statements executed and none of them through its
own API: the end-to-end tests drive `git` directly from the test file, so
`state`, `create_branch`, `commit_files`, `push` and `open_pr` were never
called by anything. This is the layer that decides what goes into a commit
and, in `push` and `open_pr`, the first code in the project that leaves the
machine — so it is worth knowing exactly what it does before it does it.

Every test runs against a real repository in a temp directory. `push` gets a
real remote too, a bare repo on the same disk, because a mocked push proves
only that the mock was called.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp_bifrost import vcs  # noqa: E402
from tests.support import requires_git  # noqa: E402


def run(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout.strip()


def make_repo() -> Path:
    repo = Path(tempfile.mkdtemp())
    run(repo, "init", "-q", "-b", "main")
    run(repo, "config", "user.email", "test@example.invalid")
    run(repo, "config", "user.name", "Test")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    (repo / "b.txt").write_text("two\n", encoding="utf-8")
    run(repo, "add", "-A")
    run(repo, "commit", "-q", "-m", "initial")
    return repo


@requires_git
class StateTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()

    def test_state_reports_root_branch_and_head(self):
        st = vcs.state(self.repo)
        self.assertEqual(self.repo.resolve(), st.root.resolve())
        self.assertEqual("main", st.branch)
        self.assertEqual(run(self.repo, "rev-parse", "HEAD"), st.head)
        self.assertEqual([], st.dirty)

    def test_a_modified_file_shows_up_as_dirty(self):
        (self.repo / "a.txt").write_text("changed\n", encoding="utf-8")
        self.assertEqual(["a.txt"], vcs.state(self.repo).dirty)

    def test_state_can_be_asked_about_a_file_inside_the_repo(self):
        """The engine has a path to a file, not to a root."""
        st = vcs.state(self.repo / "a.txt")
        self.assertEqual(self.repo.resolve(), st.root.resolve())

    def test_outside_a_repository_it_refuses(self):
        with self.assertRaises(vcs.VcsError):
            vcs.state(Path(tempfile.mkdtemp()))


@requires_git
class CreateBranchTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()

    def test_it_branches_and_switches(self):
        vcs.create_branch(self.repo, "bifrost/work")
        self.assertEqual("bifrost/work", vcs.state(self.repo).branch)

    def test_uncommitted_work_carries_over_rather_than_being_stashed(self):
        """A batch is applied first and branched afterwards, so the changes
        are precisely what should end up on the new branch."""
        (self.repo / "a.txt").write_text("patched\n", encoding="utf-8")
        vcs.create_branch(self.repo, "bifrost/work")
        self.assertEqual("patched\n",
                         (self.repo / "a.txt").read_text(encoding="utf-8"))
        self.assertEqual(["a.txt"], vcs.state(self.repo).dirty)

    def test_an_existing_branch_name_is_refused(self):
        vcs.create_branch(self.repo, "bifrost/work")
        run(self.repo, "checkout", "-q", "main")
        with self.assertRaises(vcs.VcsError) as caught:
            vcs.create_branch(self.repo, "bifrost/work")
        self.assertIn("already exists", str(caught.exception))


@requires_git
class CommitFilesTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()

    def test_only_the_named_files_are_staged(self):
        """Never `git add -A`: sweeping the working tree would put someone
        else's unrelated work in a commit describing ours."""
        (self.repo / "a.txt").write_text("mine\n", encoding="utf-8")
        (self.repo / "b.txt").write_text("someone else's\n", encoding="utf-8")

        vcs.commit_files(self.repo, ["a.txt"], "only a")

        self.assertEqual(["a.txt"],
                         run(self.repo, "show", "--name-only", "--format=",
                             "HEAD").split())
        self.assertEqual(["b.txt"], vcs.state(self.repo).dirty)

    def test_the_commit_message_survives_intact(self):
        """The body carries each change's stated reason, and a rationale can
        begin with anything at all."""
        (self.repo / "a.txt").write_text("mine\n", encoding="utf-8")
        message = ("Subject line\n\n"
                   "* a.txt: # not a comment, a rationale\n"
                   "* a.txt: keeps `backticks` and \"quotes\"\n")
        sha = vcs.commit_files(self.repo, ["a.txt"], message)

        self.assertEqual(sha, run(self.repo, "rev-parse", "HEAD"))
        written = run(self.repo, "log", "-1", "--format=%B")
        self.assertIn("# not a comment, a rationale", written)
        self.assertIn('keeps `backticks` and "quotes"', written)

    def test_unchanged_files_are_not_committed(self):
        with self.assertRaises(vcs.VcsError) as caught:
            vcs.commit_files(self.repo, ["a.txt"], "nothing changed")
        self.assertIn("nothing to commit", str(caught.exception))

    def test_a_file_that_does_not_exist_is_named(self):
        with self.assertRaises(vcs.VcsError) as caught:
            vcs.commit_files(self.repo, ["ghost.txt"], "nothing there")
        self.assertIn("exist", str(caught.exception))


@requires_git
class PushTest(unittest.TestCase):
    """A real remote, because a mocked push proves only that the mock ran."""

    def setUp(self):
        self.repo = make_repo()
        self.remote = Path(tempfile.mkdtemp()) / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)],
                       check=True)
        run(self.repo, "remote", "add", "origin", str(self.remote))

    def test_push_publishes_the_current_branch_and_tracks_it(self):
        vcs.create_branch(self.repo, "bifrost/work")
        (self.repo / "a.txt").write_text("patched\n", encoding="utf-8")
        vcs.commit_files(self.repo, ["a.txt"], "patch it")

        vcs.push(self.repo)

        remote_head = subprocess.run(
            ["git", "-C", str(self.remote), "rev-parse", "bifrost/work"],
            capture_output=True, text=True)
        self.assertEqual(0, remote_head.returncode, remote_head.stderr)
        self.assertEqual(run(self.repo, "rev-parse", "HEAD"),
                         remote_head.stdout.strip())
        self.assertEqual("origin/bifrost/work",
                         run(self.repo, "rev-parse", "--abbrev-ref",
                             "--symbolic-full-name", "@{u}"))

    def test_pushing_to_a_remote_that_is_not_there_raises(self):
        with self.assertRaises(vcs.VcsError):
            vcs.push(self.repo, remote="nowhere")


@requires_git
class OpenPrTest(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()

    def test_without_gh_it_explains_the_fallback_instead_of_failing(self):
        """The branch is already pushed by this point, so opening the pull
        request by hand is a perfectly good outcome — but only if the message
        says so."""
        # git present, gh absent — emptying PATH entirely would fail one
        # step earlier, on `git`, and prove nothing about this path.
        bin_dir = Path(tempfile.mkdtemp())
        (bin_dir / "git").symlink_to(shutil.which("git"))
        original = os.environ["PATH"]
        os.environ["PATH"] = str(bin_dir)
        try:
            with self.assertRaises(vcs.VcsError) as caught:
                vcs.open_pr(self.repo, "A title")
        finally:
            os.environ["PATH"] = original
        message = str(caught.exception)
        self.assertIn("gh", message)
        self.assertIn("browser", message)


class MissingBinaryTest(unittest.TestCase):
    """No @requires_git here: this is the case where git is absent."""

    def test_a_missing_git_says_so_rather_than_raising_errno_2(self):
        empty_bin = Path(tempfile.mkdtemp())
        original = os.environ["PATH"]
        os.environ["PATH"] = str(empty_bin)
        try:
            with self.assertRaises(vcs.GitBinaryMissing) as caught:
                vcs.state(Path(tempfile.mkdtemp()))
        finally:
            os.environ["PATH"] = original
        self.assertIn("not on PATH", str(caught.exception))
