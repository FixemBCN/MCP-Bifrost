"""
Python adapter.

Both jobs go through `ast`: stdlib, official, and exact. No third-party
grammar, mirroring the PHP adapter's use of `token_get_all()`.

Two things differ from PHP and both are traps:

1. **Indentation is semantic.** A block returned at the wrong indentation is
   not untidy, it is broken — or worse, silently valid with the wrong
   nesting. The payload's `indent` normalisation handles this, and it is not
   optional here.

2. **`ast` mixes units.** `lineno` is a 1-based line number, while
   `col_offset` is a UTF-8 *byte* offset within that line. Converting the
   pair to an absolute position therefore has to be done against the file's
   bytes, never its characters — the same rule as everywhere else in this
   codebase, arrived at from the opposite direction.
"""

from __future__ import annotations

import ast
from pathlib import Path

from . import ExtractionError, Symbol

_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


class PythonAdapter:
    name = "python"
    extensions = (".py",)

    # ------------------------------------------------------------ extraction

    @staticmethod
    def _line_starts(source: bytes) -> list[int]:
        """Byte offset of the first byte of each line, 0-indexed by line-1."""
        starts, pos = [0], 0
        for line in source.split(b"\n")[:-1]:
            pos += len(line) + 1
            starts.append(pos)
        return starts

    def symbols(self, path: Path) -> list[Symbol]:
        source = path.read_bytes()
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as e:
            raise ExtractionError(f"{path}: {e}") from e

        starts = self._line_starts(source)
        out: list[Symbol] = []

        def walk(node, cls: str | None) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, _DEFS):
                    out.append(self._to_symbol(child, cls, source, starts))
                    walk(child,
                         child.name if isinstance(child, ast.ClassDef) else cls)
                elif isinstance(child, (ast.If, ast.Try, ast.With)):
                    # Conditional definitions are real and addressable.
                    walk(child, cls)

        walk(tree, None)
        out.sort(key=lambda s: s.start_byte)
        return out

    def _to_symbol(self, node, cls: str | None, source: bytes,
                   starts: list[int]) -> Symbol:
        # Decorators belong to the symbol. `node.lineno` points at `def`, so
        # taking it alone would splice a replacement between a decorator and
        # the function it decorates — the Python twin of detaching a docblock,
        # and just as invisible: the file still parses and the decorator
        # silently applies to whatever now follows it.
        first = node.decorator_list[0] if node.decorator_list else node
        start_line = first.lineno
        # Decorators start at `@`, one column before the AST node.
        start = starts[start_line - 1] + (
            first.col_offset - 1 if node.decorator_list else node.col_offset
        )
        end = starts[node.end_lineno - 1] + node.end_col_offset

        line_start = starts[start_line - 1]
        prefix = source[line_start:start]
        indent = prefix.decode("utf-8") if not prefix.strip() else ""

        # A comment block sitting immediately above, by analogy with a PHP
        # docblock. Whether it belongs to the symbol is the caller's call.
        doc_start = None
        probe = start_line - 1
        while probe >= 1:
            line = source[starts[probe - 1]:
                          starts[probe] if probe < len(starts) else len(source)]
            if line.strip().startswith(b"#"):
                doc_start = starts[probe - 1] + (
                    len(line) - len(line.lstrip()))
                probe -= 1
            else:
                break

        name = node.name
        return Symbol(
            name=name,
            fqn=f"{cls}.{name}" if cls else name,
            cls=cls,
            start_byte=start,
            end_byte=end,
            start_line=start_line,
            end_line=node.end_lineno,
            n_lines=node.end_lineno - start_line + 1,
            indent=indent,
            abstract=False,
            doc_start_byte=doc_start,
        )

    def find(self, path: Path, name: str) -> Symbol:
        syms = self.symbols(path)
        matches = [s for s in syms if s.fqn == name] or \
                  [s for s in syms if s.name == name]
        if not matches:
            raise ExtractionError(
                f"{path}: no symbol named {name!r}. "
                f"Known: {', '.join(sorted(s.fqn for s in syms)[:20])}"
            )
        if len(matches) > 1:
            raise ExtractionError(
                f"{path}: {name!r} is ambiguous, qualify it: "
                f"{', '.join(m.fqn for m in matches)}"
            )
        return matches[0]

    # ------------------------------------------------------------ validation

    def validate(self, source: bytes) -> tuple[bool, str]:
        """
        `ast.parse` — cheaper and more faithful than shelling out, and the
        same authority the interpreter uses.

        Syntax only, like every gate 1: a call to a name that does not exist
        parses perfectly well.
        """
        try:
            ast.parse(source)
            return True, ""
        except SyntaxError as e:
            where = f" on line {e.lineno}" if e.lineno else ""
            return False, f"SyntaxError: {e.msg}{where}"
        except ValueError as e:
            # Null bytes, and other things ast refuses before parsing.
            return False, f"ValueError: {e}"

    def count_symbols(self, block: bytes) -> int:
        """
        How many symbols a standalone block defines.

        The block arrives at zero indentation (the payload normalises it), so
        a method parses as a module-level function. Only top-level
        definitions count: a nested helper belongs to its parent.
        """
        try:
            tree = ast.parse(block)
        except SyntaxError as e:
            raise ExtractionError(f"block is not parsable on its own: {e}") from e
        return sum(1 for n in tree.body if isinstance(n, _DEFS))
