"""
Human-readable documentation from the operational log.

Every patch already records the worker's `rationale` — one line saying why
the change was made — captured at zero cost to the orchestrator's context.
This turns that accumulated record into a CHANGELOG, release notes, or a
commit body.

**On demand only.** Nothing here runs on a timer, on a hook, or as a side
effect of patching. It is invoked when a person or the orchestrating model
asks for it, and not otherwise: generating documentation nobody requested is
how a log becomes noise.

Reading is read-only — the log is opened in read-only mode so a report can
never disturb the record it describes.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class Entry:
    ts: str
    session: str | None
    op: str
    file: str
    symbol: str | None
    instruction: str | None
    rationale: str | None
    diff_lines: int


def read(db_path: Path, since: str | None = None, session: str | None = None,
         file_filter: str | None = None,
         include_rejected: bool = False) -> list[Entry]:
    """Pull the applied patches out of the log, oldest first."""
    if not db_path.exists():
        raise FileNotFoundError(f"no log at {db_path}")

    where, params = [], []
    if not include_rejected:
        where.append("estat = 'ok'")
    if since:
        where.append("ts >= ?")
        params.append(since)
    if session:
        where.append("session = ?")
        params.append(session)
    if file_filter:
        where.append("fitxer LIKE ?")
        params.append(f"%{file_filter}%")
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"""SELECT ts, session, op, fitxer, simbol, instruccio, rationale,
                       COALESCE(out_b, 0) - COALESCE(src_b, 0) AS delta
                FROM patches {clause} ORDER BY ts""", params).fetchall()
    finally:
        con.close()

    return [
        Entry(ts=r["ts"], session=r["session"], op=r["op"], file=r["fitxer"],
              symbol=r["simbol"], instruction=r["instruccio"],
              rationale=r["rationale"], diff_lines=r["delta"] or 0)
        for r in rows
    ]


def _shorten(path: str, root: Path | None) -> str:
    if root is None:
        return path
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return path


_VERBS = {
    "fix_symbol": "Changed",
    "fix_range": "Changed",
    "insert_symbol": "Added",
    "insert_case": "Added",
    "create_file": "Created",
}


def as_changelog(entries: list[Entry], title: str = "Changes",
                 root: Path | None = None, group: str = "file") -> str:
    """
    Markdown grouped by file (the default) or by session.

    By file reads as a changelog; by session reads as a work log. Neither
    invents anything: every line is either recorded fact or the worker's own
    stated reason.
    """
    if not entries:
        return f"# {title}\n\n_No recorded changes._\n"

    out = [f"# {title}", ""]
    first, last = entries[0].ts[:10], entries[-1].ts[:10]
    span = first if first == last else f"{first} — {last}"
    out.append(f"_{len(entries)} change(s), {span}._")
    out.append("")

    buckets: dict[str, list[Entry]] = defaultdict(list)
    for e in entries:
        key = _shorten(e.file, root) if group == "file" else (e.session or "—")
        buckets[key].append(e)

    for key in sorted(buckets):
        out.append(f"## {key}")
        out.append("")
        for e in buckets[key]:
            what = _VERBS.get(e.op, "Modified")
            target = f"`{e.symbol}`" if e.symbol else f"`{_shorten(e.file, root)}`"
            line = f"- **{what} {target}**"
            if e.diff_lines:
                sign = "+" if e.diff_lines > 0 else ""
                line += f" ({sign}{e.diff_lines} bytes)"
            out.append(line)
            if e.instruction:
                out.append(f"  - *Asked:* {e.instruction.strip()}")
            if e.rationale:
                out.append(f"  - *Why:* {e.rationale.strip()}")
        out.append("")

    return "\n".join(out)


def as_commit_message(entries: list[Entry], subject: str,
                      root: Path | None = None) -> str:
    """
    A commit body listing what changed and why.

    The subject stays the caller's: a machine summarising a batch into one
    line produces something like "Update 12 methods", which is exactly the
    commit message everyone complains about.
    """
    if not entries:
        return subject

    files = sorted({_shorten(e.file, root) for e in entries})
    body = [subject, ""]
    body.append(f"{len(entries)} change(s) across {len(files)} file(s), "
                f"applied via MCP-Bifrost.")
    body.append("")
    for e in entries:
        target = e.symbol or _shorten(e.file, root)
        body.append(f"* {target}: {e.rationale or e.instruction or e.op}")
    return "\n".join(body)


def summarise(entries: list[Entry]) -> str:
    """One-line stats, for when a full document is not wanted."""
    if not entries:
        return "no recorded changes"
    files = {e.file for e in entries}
    ops = defaultdict(int)
    for e in entries:
        ops[e.op] += 1
    breakdown = ", ".join(f"{v} {k}" for k, v in sorted(ops.items()))
    return (f"{len(entries)} change(s) across {len(files)} file(s) "
            f"({breakdown})")


# ---------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="bifrost-docs",
        description="Generate documentation from the MCP-Bifrost log. "
                    "Runs only when asked.")
    ap.add_argument("--db", default=".bifrost/history.db", type=Path)
    ap.add_argument("--since", help="ISO date, e.g. 2026-08-01")
    ap.add_argument("--session")
    ap.add_argument("--file", dest="file_filter")
    ap.add_argument("--group", choices=("file", "session"), default="file")
    ap.add_argument("--title", default="Changes")
    ap.add_argument("--root", type=Path,
                    help="Shorten paths relative to this directory.")
    ap.add_argument("-o", "--output", type=Path,
                    help="Write here instead of standard output.")
    args = ap.parse_args(argv)

    try:
        entries = read(args.db, since=args.since, session=args.session,
                       file_filter=args.file_filter)
    except FileNotFoundError as e:
        print(e)
        return 1

    text = as_changelog(entries, title=args.title, root=args.root,
                        group=args.group)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(f"{summarise(entries)} → {args.output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
