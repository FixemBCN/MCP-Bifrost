"""
Language adapters.

Every language is analysed with its own official tooling rather than a
common third-party grammar: PHP through `token_get_all()`, Python through
`ast`. That is less uniform and more faithful, and fidelity is what matters
when the answer decides whether we overwrite a file.

THE RULE THAT OVERRIDES EVERYTHING: adapters speak `bytes`, never `str`.

PHP's `token_get_all()` reports byte offsets. Python's `str` is indexed by
character. Mixing them shifts every cut by one position per multi-byte
character preceding the symbol, silently, and the result still looks like
code. This cost 6 of 9 cases in calibration before it was found. See
docs/calibration.md, Finding 1.

Text appears in exactly one place: the payload handed to the worker. It is
decoded there and re-encoded on return.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Symbol:
    """One addressable symbol, located by byte offsets."""

    name: str
    fqn: str
    cls: str | None
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    n_lines: int
    indent: str
    abstract: bool = False
    doc_start_byte: int | None = None
    # What was declared: "function" for a function or method, and the
    # declaration keyword for anything that contains other symbols —
    # "class", "interface", "trait", "enum". The engine needs the
    # distinction: `end_of_class` means the end of a container, and only a
    # container has one.
    kind: str = "function"

    @property
    def is_container(self) -> bool:
        """Does this symbol hold other symbols?"""
        return self.kind != "function"

    def extract(self, source: bytes) -> bytes:
        """The symbol's own bytes, sliced out of the whole file."""
        return source[self.start_byte:self.end_byte]


@dataclass(frozen=True)
class Case:
    """
    One branch of a `switch`.

    Not a symbol — no parser hands you one — but in a router-style file it is
    the unit every new endpoint is added to, so it needs addressing of its own.

    `end_byte` sits just after the branch's last real statement, not at the
    start of the next `case`. The gap between them usually holds a blank line
    and a section comment introducing what follows; ending early leaves those
    with the branch they belong to.
    """

    label: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    indent: str
    # A bare label with no statements of its own, dropping into the branch
    # below it. Not an error, but not a safe anchor either: inserting after it
    # cuts the chain, and the result still parses.
    fallthrough: bool = False

    def extract(self, source: bytes) -> bytes:
        return source[self.start_byte:self.end_byte]


class ExtractionError(RuntimeError):
    """The file could not be parsed, or its symbol map is untrustworthy."""


@runtime_checkable
class LanguageAdapter(Protocol):
    """
    What a language must provide. Implementations are thin: the hard work
    belongs to the language's own parser, not to us.
    """

    name: str
    extensions: tuple[str, ...]

    # One level of indentation in this language. Consulted only when a
    # container has no member whose indentation could be copied instead.
    indent_unit: str

    def symbols(self, path: Path) -> list[Symbol]:
        """Every addressable symbol in the file. Raises ExtractionError."""
        ...

    def count_symbols(self, block: bytes) -> int:
        """
        How many symbols a standalone block declares at its own level.

        A container counts as one, whatever it holds: a nested helper
        belongs to its parent. Gate 2 depends on this reading.
        """
        ...

    def blank_lines(self, indent: str) -> int:
        """
        Blank lines between a new symbol and its neighbour, given where it
        will sit. PEP 8 wants two at the top level and one between methods;
        PSR-12 wants one everywhere.
        """
        ...

    def validate(self, source: bytes) -> tuple[bool, str]:
        """
        Is this a syntactically valid file? Returns (ok, message).

        Form only. It says nothing about whether the code is correct, or
        whether the methods it calls exist. See docs/architecture.md §5.
        """
        ...


# --------------------------------------------------------------- indentation

def strip_indent(block: bytes, indent: str) -> bytes:
    """
    Remove `indent` from the front of every line that carries it.

    The worker must never have to guess indentation, so it is taken off
    before sending and put back on return. In calibration 1 of 9 blocks came
    back at the wrong indentation while being otherwise correct; in Python,
    where indentation is semantic, the same slip is not cosmetic but broken
    code.
    """
    if not indent:
        return block
    pad = indent.encode("utf-8")
    out = []
    for line in block.split(b"\n"):
        out.append(line[len(pad):] if line.startswith(pad) else line)
    return b"\n".join(out)


def apply_indent(block: bytes, indent: str) -> bytes:
    """
    Re-apply `indent`, the exact inverse of strip_indent().

    THE FIRST LINE IS NEVER PADDED. A symbol's start_byte points at the
    `public`/`def` keyword, not at the start of its line, so the file already
    holds that indentation immediately before the splice point. Padding line
    zero would double it — and since the result still parses, nothing
    downstream would catch it.

    Blank lines stay blank: padding them introduces trailing whitespace the
    original did not have, which shows up as noise in every diff.
    """
    if not indent:
        return block
    pad = indent.encode("utf-8")
    lines = block.split(b"\n")
    out = [lines[0]]
    for line in lines[1:]:
        out.append(pad + line if line.strip() else line)
    return b"\n".join(out)


def normalise(block: bytes, indent: str) -> tuple[bytes, str]:
    """
    Strip the indentation ONLY when doing so is losslessly reversible.

    Returns the block to send and the indent to re-apply on return; an empty
    indent means "send it as it stands, put nothing back".

    Why this needs checking rather than assuming: `apply_indent` pads every
    non-blank line, while `strip_indent` only removes padding from lines that
    carry it. Those are inverses right up until a line inside the block sits
    at a shallower indentation than the block itself — which in Python is
    ordinary, because the body of a triple-quoted string can start at column
    zero:

        def test(self):
            src = '''
        class Alpha
        '''

    Re-indenting that injects whitespace INTO the string literal, changing
    the program's data rather than its layout. The file still parses and no
    gate can see it, because nothing structural is wrong.

    Rather than guess, the round trip is performed and compared. When it does
    not hold, normalisation is skipped and the worker is handed the block
    with its real indentation — which the system prompt already covers.
    """
    if not indent:
        return block, ""
    stripped = strip_indent(block, indent)
    if apply_indent(stripped, indent) == block:
        return stripped, indent
    return block, ""
