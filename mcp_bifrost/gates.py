"""
Validation gates.

Nothing reaches disk until these pass. They are deliberately few, because
the plan originally had three and two of them could not fail — see
docs/critical-review.md, RF-1. What survives here is only what can actually
reject something.

Explicitly NOT implemented, and not by omission:

    The perimeter gate. Comparing bytes outside the target range can never
    fail, because the patcher builds the file as
    `original[:start] + block + original[end:]` and the perimeter is
    preserved by construction. In the first calibration run that gate
    reported 9/9 while three files were left syntactically broken. A check
    that cannot fail is worse than no check: it emits a positive signal
    without inspecting anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class GateResult:
    ok: bool
    gate: str
    detail: str = ""

    def __bool__(self) -> bool:
        return self.ok


PASS = GateResult(True, "")


# --------------------------------------------------------------- gate 0

def check_offsets(current: bytes, start: int, end: int,
                  sent_src: bytes) -> GateResult:
    """
    Gate 0 — the critical one.

    The bytes now sitting at [start:end] must be exactly what we handed the
    worker. This one CAN fail, and the damage it prevents is total: splicing
    at stale offsets injects the block into the middle of another symbol and
    destroys code, without `php -l` necessarily noticing.

    Reasons it fires, all real: an earlier patch in the same batch moved
    everything below it; another process or the user's editor touched the
    file; boundaries were computed against a different revision.

    Calibration validated its worth — it would have caught all nine
    corruptions in the first run.
    """
    if end > len(current):
        return GateResult(False, "offsets",
                          f"range {start}:{end} runs past end of file "
                          f"({len(current)} bytes)")
    actual = current[start:end]
    if actual == sent_src:
        return PASS
    return GateResult(
        False, "offsets",
        f"file changed under us: expected {len(sent_src)} bytes at "
        f"{start}:{end}, found {len(actual)} that differ. "
        f"Re-resolve the symbol and retry."
    )


# --------------------------------------------------------------- gate 1

def check_syntax(adapter, candidate: bytes) -> GateResult:
    """
    Gate 1 — the reconstructed file must parse.

    Form only. `$this->methodThatDoesNotExist()` passes here and breaks in
    production; see docs/architecture.md §5 for the cheap mitigation we owe
    this gate later.
    """
    ok, msg = adapter.validate(candidate)
    return PASS if ok else GateResult(False, "syntax", msg)


# --------------------------------------------------------------- gate 2

def check_single_symbol(adapter, block: bytes) -> GateResult:
    """
    Gate 2 — the returned block defines exactly one symbol.

    This is all that survives of the old "structural integrity" gate. The
    version that compared the whole file's symbol list was pointless: with
    symbol extraction the worker never sees neighbouring code, so it cannot
    delete it. What it CAN do is helpfully add a sibling function of its own
    invention, and that is what this catches.
    """
    try:
        n = adapter.count_symbols(block)
    except Exception as e:  # noqa: BLE001 - adapter-specific failures vary
        return GateResult(False, "single_symbol", f"block not parsable: {e}")
    if n == 1:
        return PASS
    return GateResult(False, "single_symbol",
                      f"block defines {n} symbols, expected 1")


# --------------------------------------------------------------- gate 3

# Deliberately coarse. The point is to notice that something vanished, not
# to understand the code. False positives cost a rejection the orchestrator
# can override; false negatives cost silent loss of logic.
_CALL = re.compile(rb"(?:->|::)?\b([A-Za-z_]\w*)\s*\(")
_VAR = re.compile(rb"\$([A-Za-z_]\w*)")
_KEYWORD = re.compile(
    rb"\b(if|else|elseif|foreach|for|while|switch|case|try|catch|finally|"
    rb"throw|return|break|continue|yield|match)\b"
)


def _substance(block: bytes) -> dict[str, set[bytes]]:
    return {
        "calls": set(_CALL.findall(block)),
        "vars": set(_VAR.findall(block)),
        "keywords": set(_KEYWORD.findall(block)),
    }


def check_substance(before: bytes, after: bytes,
                    allow_removal: bool = False) -> GateResult:
    """
    Gate 3 — nothing disappeared that we did not ask to remove.

    This attacks the real risk (RF-2): code that compiles but has quietly
    lost logic from inside the method — a dropped `if` branch, a forgotten
    call, a `try/catch` simplified away. No other gate sees it, because the
    result is perfectly valid code.

    Calibration did not observe it (3/3 additive tasks lost nothing), which
    is why this is not blocking on day one. Nine cases of 5-28 lines do not
    rule it out, and the risk grows with block size.

    Set `allow_removal` when the instruction is explicitly about deleting
    something.
    """
    if allow_removal:
        return PASS
    b, a = _substance(before), _substance(after)
    lost = {k: sorted(x.decode("utf-8", "replace") for x in b[k] - a[k])
            for k in b}
    lost = {k: v for k, v in lost.items() if v}
    if not lost:
        return PASS
    parts = [f"{k}: {', '.join(v[:6])}" for k, v in lost.items()]
    return GateResult(False, "substance",
                      "the block lost " + "; ".join(parts))


# --------------------------------------------------------------- size limit

def check_size(n_lines: int, limit: int = 150) -> GateResult:
    """
    Not a gate on the worker's output but on whether to call it at all.

    Symbol addressing assumes symbols are small. The target codebase holds a
    1,380-line method: for it, extraction yields nearly the whole file — no
    saving, and a block too large for a cheap model to return intact, which
    is precisely the failure gate 3 exists for.

    Refusing loudly beats degrading quietly.
    """
    if n_lines <= limit:
        return PASS
    return GateResult(
        False, "size",
        f"symbol is {n_lines} lines, over the {limit}-line limit. "
        f"Use a narrower range, or edit it directly."
    )


# --------------------------------------------------------------- insertion

def check_symbol_set(before: list[str], after: list[str],
                     expected_new: int = 1) -> GateResult:
    """
    Gate for insertions — every symbol that existed still exists, plus
    exactly `expected_new` more.

    NOT a split-perimeter check. An insertion is built as
    `original[:at] + block + original[at:]`, so both halves are copied
    verbatim and comparing them can no more fail than the perimeter gate
    RF-1 deleted. This gate looks at something that genuinely can go wrong:
    a block carrying one brace too many closes the enclosing class early,
    which re-parents or drops every symbol after it — and still parses, so
    `php -l` stays silent.
    """
    b, a = set(before), set(after)
    lost = sorted(b - a)
    if lost:
        return GateResult(False, "symbol_set",
                          f"insertion removed {len(lost)} existing symbol(s): "
                          + ", ".join(lost[:5]))
    gained = sorted(a - b)

    # Count roots, not names. A class is one symbol that brings its methods
    # with it: inserting `class Foo` with eight methods adds nine names to
    # the map and exactly one root. Counting names refused every class
    # insertion the tool description promises, and rolled the write back
    # afterwards, so the refusal looked like a gate catching something.
    #
    # Nothing is given up by counting this way. A block that closes its
    # enclosing class early re-parents the symbols below it — `Foo.bar`
    # becomes `bar` — which registers as a loss above, and two sibling
    # functions where one was asked for are two roots here.
    roots = [n for n in gained
             if not any(n.startswith(other + sep)
                        for other in gained for sep in (".", "::"))]
    if len(roots) != expected_new:
        return GateResult(
            False, "symbol_set",
            f"expected {expected_new} new symbol(s), got {len(roots)}"
            + (f": {', '.join(roots[:5])}" if roots else "")
        )
    return PASS


def check_absent(exists: bool, path: str) -> GateResult:
    """create_file never overwrites. Replacing a file is a different
    operation with different consequences, and it should have to be asked
    for by name."""
    if exists:
        return GateResult(False, "absent",
                          f"{path} already exists. create_file never "
                          f"overwrites; use fix_symbol or delete it first.")
    return PASS


def check_nonempty(adapter, content: bytes, min_symbols: int = 1) -> GateResult:
    """
    A created file must actually contain something.

    An empty PHP file passes `php -l` cleanly — emptiness is valid syntax.
    The worker returned an empty `out` once and this was written to disk as a
    one-byte file, reported OK, and counted as a successful transaction. The
    syntax gate cannot catch this; nothing structural is wrong, there is just
    nothing there.
    """
    if not content.strip() or content.strip() in (b"<?php", b"<?php\n"):
        return GateResult(False, "nonempty", "the worker returned nothing")
    try:
        n = adapter.count_symbols(content)
    except Exception:  # noqa: BLE001 - a whole file, not a bare block
        n = -1
    if n == 0:
        return GateResult(False, "nonempty",
                          "the generated file defines no symbols")
    return PASS


def check_case_set(before: list[str], after: list[str],
                   expected_new: int = 1) -> GateResult:
    """
    Gate for `insert_case` — every branch that existed still exists, plus
    exactly `expected_new` more, and no label is duplicated.

    Duplication is the failure worth naming. PHP takes the first matching
    `case` and ignores the rest without a word, so a second branch with an
    existing label is dead code that looks alive: the file parses, the symbol
    set is untouched, tests that exercise the old branch still pass, and the
    new endpoint simply never runs.
    """
    b, a = set(before), set(after)
    lost = sorted(b - a)
    if lost:
        return GateResult(False, "case_set",
                          "insertion removed case(s): " + ", ".join(lost[:5]))
    if len(after) != len(set(after)):
        dupes = sorted({x for x in after if after.count(x) > 1})
        return GateResult(
            False, "case_set",
            "duplicate case label(s): " + ", ".join(dupes[:5])
            + " — PHP would silently ignore all but the first."
        )
    gained = sorted(a - b)
    if len(gained) != expected_new:
        return GateResult(False, "case_set",
                          f"expected {expected_new} new case(s), got "
                          f"{len(gained)}"
                          + (f": {', '.join(gained[:5])}" if gained else ""))
    return PASS
