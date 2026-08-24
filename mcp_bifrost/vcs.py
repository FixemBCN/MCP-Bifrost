"""
Branches, commits and pull requests.

The reasoning behind this is not convenience. With no meaningful test suite
under the target codebase, **human review is the only safety net there is** —
nothing else will notice a semantic regression a cheap model introduced. And
thirty patches scattered through a working tree are close to unreviewable,
while the same thirty on a branch, grouped into one commit whose body carries
each change's stated reason, are readable in minutes.

So this exists to make the one remaining safety net usable.

Everything here is **explicit**. Nothing branches, commits, pushes or opens a
pull request as a side effect of patching. Each is a tool the caller invokes
deliberately, because these are the operations that leave the machine.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class VcsError(RuntimeError):
    pass


class GitBinaryMissing(VcsError):
    """`git` is not on PATH. Same shape of problem as PhpBinaryMissing."""


def _run_git(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """
    A missing `git` should say so, not surface as errno 2 from whichever
    call site got there first.
    """
    try:
        return subprocess.run(cmd, **kwargs)
    except FileNotFoundError as e:
        raise GitBinaryMissing(
            "`git` is not on PATH: the branch, commit and push tools need it. "
            "Patching itself does not."
        ) from e


def _git(repo: Path, *args: str, check: bool = True,
         raw: bool = False) -> str:
    """
    `raw` keeps the output exactly as git wrote it.

    Stripping is the right default for the one-line answers this is mostly
    used for, and wrong for `status --porcelain`, whose first two columns are
    the status code and may legitimately begin with a space. Stripping ate
    that space, and the fixed-width slice that follows then removed the first
    character of the filename.
    """
    proc = _run_git(["git", "-C", str(repo), *args],
                    capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise VcsError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout if raw else proc.stdout.strip()


@dataclass(frozen=True)
class RepoState:
    root: Path
    branch: str
    head: str
    dirty: list[str]


def state(path: Path) -> RepoState:
    root = Path(_git(path.parent if path.is_file() else path,
                     "rev-parse", "--show-toplevel"))
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    head = _git(root, "rev-parse", "HEAD", check=False)
    porcelain = _git(root, "status", "--porcelain", check=False, raw=True)
    dirty = [l[3:] for l in porcelain.splitlines() if l.strip()]
    return RepoState(root=root, branch=branch, head=head, dirty=dirty)


def create_branch(repo: Path, name: str, base: str | None = None) -> str:
    """
    Branch and switch to it.

    Uncommitted work carries over rather than being stashed or discarded: a
    batch is normally applied first and branched afterwards, so the changes
    are precisely what should end up on the new branch.
    """
    st = state(repo)
    if name in _git(st.root, "branch", "--format=%(refname:short)").split():
        raise VcsError(f"branch {name!r} already exists")
    args = ["checkout", "-b", name]
    if base:
        args.append(base)
    _git(st.root, *args)
    return name


def commit_files(repo: Path, files: list[str], message: str) -> str:
    """
    Stage exactly these files and commit them.

    Named files only, never `git add -A`. Bifrost knows precisely which files
    it touched, and sweeping up whatever else happens to be in the working
    tree would put someone else's unrelated work into a commit describing
    ours.
    """
    st = state(repo)
    existing = [f for f in files if (st.root / f).exists()
                or _git(st.root, "ls-files", "--", f, check=False)]
    if not existing:
        raise VcsError("none of the given files exist or are tracked")

    _git(st.root, "add", "--", *existing)
    staged = _git(st.root, "diff", "--cached", "--name-only", check=False)
    if not staged.strip():
        raise VcsError("nothing to commit — the files are unchanged")

    proc = _run_git(
        ["git", "-C", str(st.root), "commit", "-F", "-"],
        input=message, capture_output=True, text=True)
    if proc.returncode != 0:
        raise VcsError(f"git commit: {proc.stderr.strip() or proc.stdout.strip()}")
    return _git(st.root, "rev-parse", "HEAD")


def push(repo: Path, branch: str | None = None, remote: str = "origin") -> str:
    """Publish the branch. This is the first step that leaves the machine."""
    st = state(repo)
    branch = branch or st.branch
    return _git(st.root, "push", "-u", remote, branch)


def open_pr(repo: Path, title: str, body: str = "",
            base: str | None = None, draft: bool = False) -> str:
    """
    Open a pull request through the `gh` CLI.

    Requires `gh` installed and authenticated. Degrades to a clear message
    rather than a stack trace when it is not, because the fallback — the
    branch is already pushed, open the PR by hand — is perfectly workable.
    """
    st = state(repo)
    # `shutil.which`, not a third binary: shelling out to `which` to find
    # out whether a binary exists fails with errno 2 on any machine that
    # does not have `which` either, which is the same class of bug this is
    # trying to report.
    if shutil.which("gh") is None:
        raise VcsError(
            "the `gh` CLI is not installed. The branch is pushed; open the "
            "pull request in the browser instead.")

    args = ["gh", "pr", "create", "--title", title, "--body", body or title]
    if base:
        args += ["--base", base]
    if draft:
        args.append("--draft")
    proc = subprocess.run(args, cwd=str(st.root), capture_output=True,
                          text=True)
    if proc.returncode != 0:
        raise VcsError(f"gh pr create: {proc.stderr.strip()}")
    return proc.stdout.strip()
