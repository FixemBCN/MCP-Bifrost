#!/usr/bin/env python3
"""
registre.py — measures the real efficacy of MCP-Bifrost.

Reads either source interchangeably:
  - `.bifrost/history.db`      operational log (SQLite, the source of truth)
  - `.bifrost/efficacy.jsonl`  derived export, lightweight

The log stores **measurements** (bytes, tokens, durations), never
conclusions. The saving estimate is derived here, so the formula can be
corrected later without invalidating anything already recorded.

Usage:
    python3 registre.py [.bifrost/history.db | .bifrost/efficacy.jsonl] [--detail]
    python3 registre.py .bifrost/history.db --export .bifrost/efficacy.jsonl

Dependencies: none, stdlib only.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

# Bytes per token, approximated for code. An order-of-magnitude estimate,
# not a measurement. Adjustable without touching any recorded data.
BYTES_PER_TOKEN = 3.5

DEFAULT_PATH = Path(".bifrost/history.db")

# Measurement columns. The only ones that reach the export: no free text,
# which is what would grow the file without helping the count.
FIELDS = ("ts", "op", "f", "sym", "ok", "src_b", "out_b", "in_b", "resp_b",
         "tin", "tout", "ms")


def read_sqlite(path: Path) -> list[dict]:
    """Operational log → measurement entries."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("""
            SELECT ts, op, fitxer AS f, simbol AS sym,
                   (estat = 'ok') AS ok,
                   src_b, out_b, in_b, resp_b, tin, tout, ms
            FROM patches ORDER BY ts
        """).fetchall()
    finally:
        con.close()
    return [{k: r[k] for k in FIELDS} for r in rows]


def read_jsonl(path: Path) -> list[dict]:
    entries = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"  ⚠ line {n} unreadable, skipped", file=sys.stderr)
    return entries


def load(path: Path) -> list[dict]:
    if path.suffix in (".db", ".sqlite", ".sqlite3"):
        return read_sqlite(path)
    return read_jsonl(path)


def saving(e: dict) -> float:
    """
    Tokens the orchestrator avoided putting into its own context.

    Baseline: what it would have cost for the orchestrator to make the edit
    itself, which is reading the block and writing it back out.

    UPPER BOUND: if the orchestrator had already read the block in order to
    formulate the instruction, `src_b` was paid for regardless and the real
    saving is smaller. The log cannot know — but it should not pretend it can.
    """
    would_have_cost = (e.get("src_b", 0) + e.get("out_b", 0)) / BYTES_PER_TOKEN
    did_cost = (e.get("in_b", 0) + e.get("resp_b", 0)) / BYTES_PER_TOKEN
    return would_have_cost - did_cost


def main() -> int:
    argv = sys.argv[1:]
    if "--export" in argv:
        i = argv.index("--export")
        del argv[i:i + 2]
    args = [a for a in argv if not a.startswith("--")]
    detail = "--detail" in sys.argv

    path = Path(args[0]) if args else DEFAULT_PATH
    if not path.exists():
        print(f"no log at {path}", file=sys.stderr)
        return 1

    entries = load(path)

    # Derived export. Never written in parallel with the operational log: it
    # is generated from it, which makes divergence impossible.
    if "--export" in sys.argv:
        i = sys.argv.index("--export")
        dest = Path(sys.argv[i + 1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps({k: e.get(k) for k in FIELDS},
                                    ensure_ascii=False) + "\n")
        print(f"  exported {len(entries)} entries → {dest} "
              f"({dest.stat().st_size:,} bytes)")

    if not entries:
        print("log is empty")
        return 0

    if detail:
        print(f"{'when':17} {'op':12} {'where':52} {'ok':3} {'saving':>9}")
        print("-" * 98)
        for e in entries:
            sym = e.get("sym")
            where = f"{Path(e.get('f','?')).name}" + (f"::{sym}" if sym else "")
            print(f"{e.get('ts','')[:16]:17} {e.get('op','?'):12} {where[:52]:52} "
                  f"{'✓' if e.get('ok') else '✗':3} {saving(e):9,.0f}")
        print()

    total = len(entries)
    ok = sum(1 for e in entries if e.get("ok"))
    total_saving = sum(saving(e) for e in entries if e.get("ok"))
    tin = sum(e.get("tin", 0) for e in entries)
    tout = sum(e.get("tout", 0) for e in entries)
    ms = [e["ms"] for e in entries if e.get("ms")]

    # Per operation.
    by_op: dict[str, int] = {}
    for e in entries:
        by_op[e.get("op", "?")] = by_op.get(e.get("op", "?"), 0) + 1

    print(f"  jobs             {total}  ({ok} applied, {total - ok} rejected)")
    for op, n in sorted(by_op.items(), key=lambda x: -x[1]):
        print(f"    {op:14} {n}")
    print(f"  files touched    {len({e.get('f') for e in entries if e.get('ok')})}")
    if ms:
        print(f"  time             {sum(ms)/1000:,.0f}s total, "
              f"{sum(ms)//len(ms):,} ms mean")
    print(f"  worker tokens    in {tin:,} / out {tout:,}")
    print(f"  estimated saving ~{total_saving:,.0f} tokens of orchestrator context")
    print(f"                   (upper bound — see the note in saving())")
    print(f"  log size         {path.stat().st_size:,} bytes")

    return 0


if __name__ == "__main__":
    sys.exit(main())
