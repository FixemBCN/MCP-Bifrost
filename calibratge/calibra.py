#!/usr/bin/env python3
"""
calibra.py — measures whether the MCP-Bifrost premise holds.

This builds none of the final product. It answers one question before a
single line of the MCP server gets written:

    Given a real method from the target codebase, packed with the compact
    schema from the spec, does DeepSeek return code that can be applied
    without breaking anything?

Per case it measures:
  json_ok       the worker returns valid JSON with the expected keys
  offsets_ok    the block on disk is byte-identical to what we sent
  php_ok        the resulting file passes `php -l`
  sig_ok        the method signature is unchanged
  perimeter_ok  everything outside the range is byte-identical
                (kept only to demonstrate that this can never fail — see
                 docs/critical-review.md, RF-1)
  indent_ok     the original indentation was respected
  identity_ok   (identity task only) the block comes back byte for byte
  no_loss       (additive tasks) no original line disappeared

Dependencies: none. Python stdlib plus the `php` binary.

Usage:
    export DEEPSEEK_API_KEY=sk-...
    python3 calibra.py                    # full run
    python3 calibra.py --dry-run          # list cases, no API calls
    python3 calibra.py --cases 5          # limit the number of cases
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path

# The codebase to calibrate against. Point it at your own: the harness picks
# real methods out of it, so a synthetic fixture would measure nothing useful.
TARGET_ROOT = Path(os.environ.get("BIFROST_TARGET", ".")).resolve()
TARGET_GLOB = os.environ.get("BIFROST_TARGET_GLOB", "**/*.php")

EXTRACT_PHP = Path(__file__).parent.parent / "mcp_bifrost" / "languages" / "extract.php"
RESULTS_DIR = Path(__file__).parent / "results"

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"

# Static system prompt. In production this is what DeepSeek's prompt caching
# keys off; here it is held identical across calls precisely so the cache hit
# rate can be measured.
SYSTEM_PROMPT = """You are a code transformation worker. You receive a JSON payload and return a JSON object. You never explain, never apologize, never add prose.

INPUT KEYS:
  lang   language of the code
  sym    the symbol being modified
  intent the modification to perform
  ctx    signatures of related code, for reference only
  src    the exact code block to transform

OUTPUT KEYS (JSON object, nothing else):
  out       the complete transformed code block, raw, no markdown fences
  why       one short technical sentence
  diff_stat "+N/-M"

HARD RULES:
1. `out` replaces `src` verbatim in the source file. It must be a drop-in replacement.
2. Preserve the original indentation of every line exactly as given in `src`.
3. Change ONLY what `intent` asks. Touch nothing else.
4. Never add, remove, or rename anything outside the scope of `intent`.
5. Never wrap `out` in markdown code fences.
6. Keep the language and wording of existing comments; do not translate them."""


# ---------------------------------------------------------------- helpers

def php_symbols(path: Path) -> dict:
    """Exact symbol map via token_get_all (extract.php)."""
    r = subprocess.run(
        ["php", str(EXTRACT_PHP), str(path)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"extract.php ha fallat a {path}: {r.stderr.strip()}")
    return json.loads(r.stdout)


def php_lint(source: bytes) -> tuple[bool, str]:
    """`php -l` against in-memory content. Works in bytes: the offsets
    token_get_all() reports are byte offsets, and mixing them with character
    indices corrupts any file containing accented characters."""
    with tempfile.NamedTemporaryFile("wb", suffix=".php", delete=False) as fh:
        fh.write(source)
        tmp = fh.name
    try:
        r = subprocess.run(["php", "-l", tmp], capture_output=True, text=True)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    finally:
        os.unlink(tmp)


def signature(block: str) -> str | None:
    """Normalised method signature, used to detect alterations."""
    m = re.search(r"function\s+&?\s*(\w+)\s*\((.*?)\)", block, re.S)
    if not m:
        return None
    params = re.sub(r"\s+", " ", m.group(2)).strip()
    return f"{m.group(1)}({params})"


def indentation(block: str) -> str:
    """Indentation of the first non-empty line."""
    for line in block.splitlines():
        if line.strip():
            return line[: len(line) - len(line.lstrip())]
    return ""


def strip_fences(text: str) -> str:
    """Strip markdown fences if the worker added them despite instructions."""
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines[0].lstrip("`").strip() in ("", "php", "python", "py"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines)
    return text


# ---------------------------------------------------------------- cases

TASKS = {
    # The single most important test. If the worker cannot return a block
    # untouched when explicitly asked to, no other guarantee survives.
    "identity": (
        "Return `src` completely unchanged, byte for byte. Make no "
        "modification of any kind."
    ),
    # Additive and checkable: no original line may disappear.
    "phpdoc": (
        "Add a PHPDoc block immediately above the function declaration, "
        "documenting @param for each parameter and @return. Keep the "
        "function body and every existing line untouched."
    ),
    # Behavioural: the actual use case.
    "guard": (
        "Add a guard clause at the very start of the function body that "
        "returns null if the first parameter is empty. If the function has "
        "no parameters, add no guard and return the code unchanged."
    ),
}


@dataclass
class Case:
    file_path: str
    fqn: str
    task: str
    start_byte: int
    end_byte: int
    n_lines: int
    src: str
    ctx: list[str] = field(default_factory=list)


def pick_cases(limit: int) -> list[Case]:
    """Pick real methods, spread across size buckets and task types."""
    candidates = []
    for f in sorted(TARGET_ROOT.glob(TARGET_GLOB)):
        if "vendor" in f.parts or "node_modules" in f.parts:
            continue
        data = php_symbols(f)
        whole = f.read_bytes()   # PHP offsets are byte offsets
        for s in data["symbols"]:
            if s["abstract"] or s["n_lines"] < 4 or s["n_lines"] > 60:
                continue
            candidates.append((f, s, whole, data["symbols"]))

    if not candidates:
        return []

    # Size buckets: small (<10), medium (10-25), large (>25).
    def bucket(s):
        n = s["n_lines"]
        return 0 if n < 10 else (1 if n <= 25 else 2)

    by_bucket: dict[int, list] = {0: [], 1: [], 2: []}
    for c in candidates:
        by_bucket[bucket(c[1])].append(c)

    cases: list[Case] = []
    tasks = list(TASKS)
    i = 0
    while len(cases) < limit:
        b = by_bucket[i % 3]
        if not b:
            i += 1
            if i > 30:
                break
            continue
        f, s, whole, siblings = b.pop(len(b) // 2)  # from the middle, not the extremes
        task = tasks[len(cases) % len(tasks)]
        # ctx: sibling signatures, as the spec prescribes.
        ctx = [
            f"{o['fqn']}()" for o in siblings
            if o["fqn"] != s["fqn"]
        ][:8]
        cases.append(Case(
            file_path=str(f),
            fqn=s["fqn"],
            task=task,
            start_byte=s["start_byte"],
            end_byte=s["end_byte"],
            n_lines=s["n_lines"],
            src=whole[s["start_byte"]:s["end_byte"]].decode("utf-8"),
            ctx=ctx,
        ))
        i += 1
    return cases


# ---------------------------------------------------------------- worker

def call_deepseek(payload: dict, api_key: str) -> tuple[dict | None, dict]:
    """Crida l'API. Retorna (json_parsejat_o_None, metadades)."""
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        "max_tokens": 4096,
    }).encode("utf-8")

    req = urllib.request.Request(
        API_URL, data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return None, {"error": f"HTTP {e.code}: {e.read().decode('utf-8')[:400]}",
                      "ms": int((time.time() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        return None, {"error": repr(e), "ms": int((time.time() - t0) * 1000)}

    ms = int((time.time() - t0) * 1000)
    us = data.get("usage", {})
    meta = {
        "ms": ms,
        "tokens_in": us.get("prompt_tokens"),
        "tokens_out": us.get("completion_tokens"),
        "cache_hit": us.get("prompt_cache_hit_tokens"),
    }
    try:
        content = data["choices"][0]["message"]["content"]
        return json.loads(content), meta
    except Exception as e:  # noqa: BLE001
        meta["error"] = f"response no parsejable: {e!r}"
        return None, meta


# --------------------------------------------------------------- evaluation

def evaluate(case: Case, response: dict | None, meta: dict) -> dict:
    r = {
        "file_path": Path(case.file_path).name,
        "fqn": case.fqn,
        "task": case.task,
        "n_lines": case.n_lines,
        **{k: v for k, v in meta.items() if k != "error"},
    }
    if "error" in meta:
        r["error"] = meta["error"]

    r["json_ok"] = bool(response and isinstance(response, dict)
                        and "out" in response)
    if not r["json_ok"]:
        return r

    r["te_why"] = "why" in response
    r["te_diff_stat"] = "diff_stat" in response

    raw = response["out"]
    if not isinstance(raw, str):
        r["json_ok"] = False
        r["error"] = f"`out` is not a string but {type(raw).__name__}"
        return r

    clean = strip_fences(raw)
    r["fences"] = clean != raw

    original = Path(case.file_path).read_bytes()
    clean_b = net.encode("utf-8")
    nou = original[:case.start_byte] + clean_b + original[case.end_byte:]

    # Offset guard: the block currently in the file must be exactly
    # what we sent. Unlike the perimeter gate (which can never fail), this
    # one can, and it is what catches corrupted offsets.
    r["offsets_ok"] = original[case.start_byte:case.end_byte] == case.src.encode("utf-8")

    # --- gate 1: syntax
    r["php_ok"], message = php_lint(nou)
    if not r["php_ok"]:
        r["php_error"] = message.splitlines()[0] if message else ""

    # --- gate 2: signature unchanged
    sig_before, sig_after = signature(case.src), signature(clean)
    r["sig_ok"] = sig_before is not None and sig_before == sig_after
    if not r["sig_ok"]:
        r["sig_before"], r["sig_after"] = sig_before, sig_after

    # --- perimeter: kept only to demonstrate RF-1 empirically. It passes
    # every time, including when the resulting file is broken.
    r["perimeter_ok"] = (nou[:case.start_byte] == original[:case.start_byte]
                         and nou[len(nou) - (len(original) - case.end_byte):]
                         == original[case.end_byte:])

    # --- indentation respected
    r["indent_ok"] = indentation(clean) == indentation(case.src)

    # --- identity (only for the matching task)
    if case.task == "identity":
        r["identity_ok"] = clean == case.src
        if not r["identity_ok"]:
            r["identity_delta"] = f"{len(case.src)}B → {len(clean)}B"

    # --- additive: no original line may have disappeared
    if case.task == "phpdoc":
        orig_lines = [l.strip() for l in case.src.splitlines() if l.strip()]
        new_lines = set(l.strip() for l in net.splitlines())
        lost = [l for l in orig_lines if l not in new_lines]
        r["no_loss"] = not lost
        if lost:
            r["lost_lines"] = lost[:3]

    # --- actual context reduction
    r["bytes_src"] = len(case.src)
    r["bytes_file"] = len(original)
    r["reduction_pct"] = round(100 * (1 - len(case.src) / len(original)), 1)

    return r


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=9)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not TARGET_ROOT.is_dir():
        print(f"BIFROST_TARGET is not a directory: {TARGET_ROOT}",
              file=sys.stderr)
        return 2
    if not EXTRACT_PHP.exists():
        print(f"missing {EXTRACT_PHP}", file=sys.stderr)
        return 2

    print(f"selecting {args.cases} cases from {TARGET_ROOT}…")
    cases = pick_cases(args.cases)
    if not cases:
        print("no cases found", file=sys.stderr)
        return 2

    for c in cases:
        print(f"  {c.task:10s} {Path(c.file_path).name:28s} "
              f"{c.fqn:45s} {c.n_lines:3d} lines")

    if args.dry_run:
        print("\n--dry-run: the API is not called.")
        return 0

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("\nDEEPSEEK_API_KEY is not set.", file=sys.stderr)
        return 3

    results = []
    print()
    for i, c in enumerate(cases, 1):
        payload = {
            "lang": "php",
            "sym": c.fqn,
            "intent": TASKS[c.task],
            "ctx": c.ctx,
            "src": c.src,
        }
        print(f"[{i}/{len(cases)}] {c.task:10s} {c.fqn}…", end=" ", flush=True)
        response, meta = call_deepseek(payload, api_key)
        r = evaluate(c, response, meta)
        results.append(r)

        gates = [k for k in ("json_ok", "offsets_ok", "php_ok", "sig_ok",
                              "perimeter_ok", "indent_ok", "identity_ok",
                              "no_loss")
                  if k in r]
        failures = [k for k in gates if not r[k]]
        print("OK" if not failures else f"FAIL → {', '.join(failures)}")

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = RESULTS_DIR / f"calibratge-{stamp}.json"
    dest.write_text(json.dumps(results, indent=2, ensure_ascii=False),
                     encoding="utf-8")

    # --- summary
    print("\n" + "=" * 62)
    total = len(results)
    for porta in ("json_ok", "offsets_ok", "php_ok", "sig_ok", "perimeter_ok",
                  "indent_ok", "identity_ok", "no_loss"):
        aplicables = [r for r in results if porta in r]
        if not aplicables:
            continue
        passen = sum(1 for r in aplicables if r[porta])
        print(f"  {porta:14s} {passen}/{len(aplicables)}")

    lat = [r["ms"] for r in results if r.get("ms")]
    red = [r["reduction_pct"] for r in results if "reduction_pct" in r]
    t_in = [r["tokens_in"] for r in results if r.get("tokens_in")]
    t_out = [r["tokens_out"] for r in results if r.get("tokens_out")]
    if lat:
        print(f"  latency        mean {sum(lat)//len(lat)} ms, "
              f"max {max(lat)} ms")
    if red:
        print(f"  reduction      mean {sum(red)/len(red):.1f}% "
              f"vs whole file")
    if t_in:
        print(f"  tokens         in {sum(t_in)} / out {sum(t_out)} "
              f"({total} calls)")

    print(f"\n  results → {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
