"""
Applying a patch to disk, and undoing it.

Everything here works in `bytes`. There is no `read_text()` in this module
and there must never be one: PHP reports byte offsets, Python `str` is
indexed by character, and mixing them silently shifts every cut by one
position per multi-byte character preceding the symbol. That bug cost 6 of 9
cases in calibration. See docs/calibration.md, Finding 1.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .gates import GateResult, check_offsets, check_syntax


class PatchError(RuntimeError):
    pass


@dataclass(frozen=True)
class Applied:
    path: Path
    blob_before: str
    bytes_before: int
    bytes_after: int
    diff_stat: str


# ------------------------------------------------------------------- git

def _git(repo: Path, *args: str, stdin: bytes | None = None) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=stdin, capture_output=True,
    )
    if proc.returncode != 0:
        raise PatchError(
            f"git {' '.join(args)}: {proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout.decode("utf-8", "replace").strip()


def repo_root(path: Path) -> Path | None:
    """The git repository containing `path`, or None."""
    try:
        return Path(_git(path.parent, "rev-parse", "--show-toplevel"))
    except PatchError:
        return None


def stash_blob(repo: Path, content: bytes) -> str:
    """
    Write `content` into git's object store and return its SHA.

    Git is already a content-addressed database, so we use it as one rather
    than duplicating snapshots. Real added disk cost is near zero: git
    compresses and deduplicates, and if this content was ever committed
    nothing new is written at all.

    Caveat: these blobs are unreachable until something references them, so
    an aggressive `git gc --prune=now` could collect them. The default
    two-week expiry is far beyond a rollback's useful window; anchor them
    under refs/bifrost/ if that ever stops being true.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input=content, capture_output=True,
    )
    if proc.returncode != 0:
        raise PatchError(
            f"git hash-object: {proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout.decode().strip()


def read_blob(repo: Path, sha: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", sha],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise PatchError(f"blob {sha} is gone: "
                         f"{proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


def head_sha(repo: Path) -> str | None:
    try:
        return _git(repo, "rev-parse", "HEAD")
    except PatchError:
        return None  # a repo with no commits yet


# ------------------------------------------------------------------- write

def atomic_write(path: Path, content: bytes) -> None:
    """
    Replace the file's contents without ever leaving it half-written.

    Write a sibling temp file, fsync it, then rename over the target. Rename
    within a filesystem is atomic, so a crash leaves either the old file or
    the new one — never a truncated source file, which is the one outcome
    that would be worse than not patching at all.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".bifrost-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        # Carry the original's permissions across; mkstemp creates 0600.
        os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _diff_stat(before: bytes, after: bytes) -> str:
    b = before.count(b"\n") + 1
    a = after.count(b"\n") + 1
    return f"+{max(a - b, 0)}/-{max(b - a, 0)}"


# ------------------------------------------------------------------- splice

def apply_block(path: Path, start: int, end: int, sent_src: bytes,
                new_block: bytes, adapter) -> Applied | GateResult:
    """
    Replace [start:end] with `new_block`, or refuse.

    Returns `Applied` on success, or the `GateResult` that rejected it. The
    file is untouched on any rejection.

    Order matters: offsets first, because it is the only gate whose failure
    means our own view of the file is wrong rather than the worker's output
    being bad. Checking syntax first would waste a parse on a file we are
    about to refuse anyway, and worse, could pass — a valid file spliced at
    the wrong place is still a valid file.
    """
    current = path.read_bytes()

    gate = check_offsets(current, start, end, sent_src)
    if not gate:
        return gate

    candidate = current[:start] + new_block + current[end:]

    gate = check_syntax(adapter, candidate)
    if not gate:
        return gate

    repo = repo_root(path)
    if repo is None:
        raise PatchError(
            f"{path} is not inside a git repository. Rollback depends on "
            f"git's object store, so patching outside one is refused."
        )
    blob = stash_blob(repo, current)

    atomic_write(path, candidate)

    return Applied(
        path=path,
        blob_before=blob,
        bytes_before=len(current),
        bytes_after=len(candidate),
        diff_stat=_diff_stat(sent_src, new_block),
    )


# ------------------------------------------------------------------- revert

def revert(path: Path, blob_sha: str) -> None:
    """Restore a file from the blob taken before its patch."""
    repo = repo_root(path)
    if repo is None:
        raise PatchError(f"{path} is not inside a git repository")
    atomic_write(path, read_blob(repo, blob_sha))


# ------------------------------------------------------------------- insert

def insert_at(path: Path, at: int, block: bytes, anchor_src: bytes,
              anchor_start: int, anchor_end: int, adapter) -> Applied | GateResult:
    """
    Insert `block` at byte offset `at`, or refuse.

    The offset gate here checks the ANCHOR, not the insertion point. An
    insertion point is a position, and a position on its own says nothing
    about whether the file still looks the way we think it does; the anchor
    symbol's bytes do.
    """
    current = path.read_bytes()

    gate = check_offsets(current, anchor_start, anchor_end, anchor_src)
    if not gate:
        return gate

    candidate = current[:at] + block + current[at:]

    gate = check_syntax(adapter, candidate)
    if not gate:
        return gate

    repo = repo_root(path)
    if repo is None:
        raise PatchError(f"{path} is not inside a git repository")
    blob = stash_blob(repo, current)
    atomic_write(path, candidate)

    # The count is computed outside the f-string: a backslash inside an
    # f-string expression is a SyntaxError before Python 3.12, and this
    # package declares 3.11 as its minimum. It cost an import failure on
    # every 3.11 install — not a failing test, a module that will not load.
    added = block.count(b"\n") + 1
    return Applied(path=path, blob_before=blob, bytes_before=len(current),
                   bytes_after=len(candidate),
                   diff_stat=f"+{added}/-0")


def create(path: Path, content: bytes, adapter) -> Applied | GateResult:
    """
    Write a new file. Never overwrites — the caller checks that first.

    There is no blob to save, because there was nothing there. Rollback for
    a creation is deletion, handled by the engine.
    """
    gate = check_syntax(adapter, content)
    if not gate:
        return gate

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".bifrost-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

    added = content.count(b"\n") + 1
    return Applied(path=path, blob_before="", bytes_before=0,
                   bytes_after=len(content),
                   diff_stat=f"+{added}/-0")
