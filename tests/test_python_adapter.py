"""
Tests for the Python adapter.

Python is half of what the README advertises and `languages/python.py` had
14 of its 69 statements executed — the imports, essentially. The PHP adapter
is tested to the byte; this is the other half of that pair.

Three of these are regressions for defects found by driving the adapter over
real files rather than by reading it:

* a decorator written `@ deco`, with the space Python allows, left the `@`
  outside the symbol, so the block handed to the worker began with a stray
  space and the decorator stayed behind on disk;
* a method of a nested class was addressed as `Inner.method`, which is the
  same address a top-level `Inner` would produce in the same file;
* `ast` reports `col_offset` in *bytes* while `lineno` counts lines, so any
  file with a non-ASCII character above a symbol will mis-slice unless the
  conversion is done against the file's bytes. That is the same trap the PHP
  adapter turns on, arrived at from the opposite direction.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp_bifrost.languages import ExtractionError  # noqa: E402
from mcp_bifrost.languages.python import PythonAdapter  # noqa: E402


def write(source: str) -> Path:
    """A .py file in its own temp directory, written as UTF-8 bytes."""
    path = Path(tempfile.mkdtemp()) / "subject.py"
    path.write_bytes(source.encode("utf-8"))
    return path


class ByteOffsetTest(unittest.TestCase):
    """`ast.col_offset` is a byte offset. Slice characters and you lose."""

    SOURCE = (
        '"""Mòdul amb accents: àèíòú, ñ, ç — i una em-dash."""\n'
        "\n"
        "def primer():\n"
        '    return "ó"\n'
        "\n"
        "def segon():\n"
        '    return "ü"\n'
    )

    def setUp(self):
        self.path = write(self.SOURCE)
        self.adapter = PythonAdapter()

    def test_the_trap_is_armed(self):
        """If this file were pure ASCII the test below could not fail."""
        raw = self.path.read_bytes()
        self.assertNotEqual(len(raw), len(raw.decode("utf-8")))

    def test_extraction_lands_on_the_correct_bytes(self):
        raw = self.path.read_bytes()
        for name, expected in (("primer", b'def primer():\n    return "\xc3\xb3"'),
                               ("segon", b'def segon():\n    return "\xc3\xbc"')):
            with self.subTest(symbol=name):
                sym = self.adapter.find(self.path, name)
                self.assertEqual(expected, raw[sym.start_byte:sym.end_byte])

    def test_splice_roundtrip_is_byte_exact(self):
        """Replacing a symbol with itself must reproduce the file exactly."""
        raw = self.path.read_bytes()
        sym = self.adapter.find(self.path, "segon")
        block = raw[sym.start_byte:sym.end_byte]
        rebuilt = raw[:sym.start_byte] + block + raw[sym.end_byte:]
        self.assertEqual(raw, rebuilt)


class DecoratorTest(unittest.TestCase):
    """A decorator belongs to the symbol it decorates."""

    def setUp(self):
        self.adapter = PythonAdapter()

    def _block(self, source: str, name: str) -> bytes:
        path = write(source)
        sym = self.adapter.find(path, name)
        return path.read_bytes()[sym.start_byte:sym.end_byte]

    def test_the_decorator_is_part_of_the_block(self):
        block = self._block("@cache\ndef f():\n    return 1\n", "f")
        self.assertTrue(block.startswith(b"@cache"),
                        f"block began {block[:12]!r}")

    def test_a_space_after_the_at_sign_does_not_orphan_it(self):
        """`@ cache` is legal Python. Stepping back one column from the AST
        node lands on the space, not the `@`, and leaves it behind."""
        block = self._block("@ cache\ndef f():\n    return 1\n", "f")
        self.assertTrue(block.startswith(b"@"),
                        f"the `@` was left outside the symbol: {block[:12]!r}")

    def test_every_decorator_in_a_stack_is_included(self):
        block = self._block(
            "@first\n@second\n@third\ndef f():\n    return 1\n", "f")
        for deco in (b"@first", b"@second", b"@third"):
            self.assertIn(deco, block)

    def test_a_decorated_method_keeps_its_indentation(self):
        path = write("class K:\n    @property\n    def v(self):\n        return 1\n")
        sym = self.adapter.find(path, "K.v")
        self.assertEqual("    ", sym.indent)
        block = path.read_bytes()[sym.start_byte:sym.end_byte]
        self.assertTrue(block.startswith(b"@property"))


class QualifiedNameTest(unittest.TestCase):
    def setUp(self):
        self.adapter = PythonAdapter()

    def test_a_nested_class_carries_its_whole_path(self):
        path = write("class A:\n    class B:\n        def m(self):\n            return 1\n")
        names = [s.fqn for s in self.adapter.symbols(path)]
        self.assertEqual(["A", "A.B", "A.B.m"], names)

    def test_a_nested_method_does_not_collide_with_a_top_level_one(self):
        """Both were once addressed as `B.m`, and `find` could only refuse."""
        path = write(
            "class A:\n    class B:\n        def m(self):\n            return 1\n"
            "\n\nclass B:\n    def m(self):\n        return 2\n"
        )
        raw = path.read_bytes()
        nested = self.adapter.find(path, "A.B.m")
        top = self.adapter.find(path, "B.m")
        self.assertIn(b"return 1", raw[nested.start_byte:nested.end_byte])
        self.assertIn(b"return 2", raw[top.start_byte:top.end_byte])

    def test_an_ambiguous_bare_name_is_refused_with_the_alternatives(self):
        path = write(
            "class A:\n    def m(self):\n        return 1\n"
            "\n\nclass B:\n    def m(self):\n        return 2\n"
        )
        with self.assertRaises(ExtractionError) as caught:
            self.adapter.find(path, "m")
        message = str(caught.exception)
        self.assertIn("ambiguous", message)
        self.assertIn("A.m", message)
        self.assertIn("B.m", message)

    def test_an_unknown_name_lists_what_there_is(self):
        path = write("def alpha():\n    return 1\n")
        with self.assertRaises(ExtractionError) as caught:
            self.adapter.find(path, "omega")
        self.assertIn("alpha", str(caught.exception))


class WhatIsAddressableTest(unittest.TestCase):
    """The boundary of the symbol map, pinned so it cannot move silently."""

    def setUp(self):
        self.adapter = PythonAdapter()

    def test_conditional_definitions_are_addressable(self):
        for kind, source in (
            ("if", "if True:\n    def f():\n        return 1\n"),
            ("try", "try:\n    def f():\n        return 1\nexcept Exception:\n    pass\n"),
            ("with", "with open('x') as fh:\n    def f():\n        return 1\n"),
        ):
            with self.subTest(kind=kind):
                names = [s.fqn for s in self.adapter.symbols(write(source))]
                self.assertEqual(["f"], names)

    def test_definitions_inside_loops_and_matches_are_not_addressable(self):
        """Current, deliberate limitation. `find` refuses by name rather
        than returning something approximate, which is the safe half of the
        behaviour; this test exists so widening it is a decision and not an
        accident."""
        for kind, source in (
            ("for", "for i in range(3):\n    def f():\n        return i\n"),
            ("match", "match 1:\n    case 1:\n        def f():\n            return 1\n"),
        ):
            with self.subTest(kind=kind):
                self.assertEqual([], self.adapter.symbols(write(source)))

    def test_async_definitions_are_addressable(self):
        path = write("async def fetch():\n    return 1\n")
        self.assertEqual(["fetch"], [s.fqn for s in self.adapter.symbols(path)])

    def test_a_file_without_a_trailing_newline_still_slices(self):
        path = write("def f():\n    return 1")
        sym = self.adapter.find(path, "f")
        self.assertEqual(b"def f():\n    return 1",
                         path.read_bytes()[sym.start_byte:sym.end_byte])


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.adapter = PythonAdapter()

    def test_valid_source_passes(self):
        ok, message = self.adapter.validate(b"def f():\n    return 1\n")
        self.assertTrue(ok)
        self.assertEqual("", message)

    def test_a_syntax_error_reports_its_line(self):
        ok, message = self.adapter.validate(b"def f():\nreturn 1\n")
        self.assertFalse(ok)
        self.assertIn("line 2", message)

    def test_a_null_byte_is_refused_without_raising(self):
        """`ast.parse` raises ValueError rather than SyntaxError for these,
        and an uncaught ValueError here would take the gate down instead of
        failing it."""
        ok, message = self.adapter.validate(b"def f():\n    return 1\n\x00")
        self.assertFalse(ok)
        self.assertTrue(message)

    def test_syntax_only_a_call_to_nothing_still_parses(self):
        ok, _ = self.adapter.validate(b"def f():\n    return does_not_exist()\n")
        self.assertTrue(ok, "gate 1 is syntax, not semantics")


class CountSymbolsTest(unittest.TestCase):
    """What gate 2 counts when it asks 'is this one symbol?'"""

    def setUp(self):
        self.adapter = PythonAdapter()

    def test_one_function_is_one(self):
        self.assertEqual(1, self.adapter.count_symbols(b"def f():\n    return 1\n"))

    def test_a_class_with_methods_is_one(self):
        self.assertEqual(1, self.adapter.count_symbols(
            b"class K:\n    def a(self):\n        return 1\n"
            b"    def b(self):\n        return 2\n"))

    def test_a_nested_helper_belongs_to_its_parent(self):
        self.assertEqual(1, self.adapter.count_symbols(
            b"def outer():\n    def inner():\n        return 1\n    return inner\n"))

    def test_two_siblings_are_two(self):
        self.assertEqual(2, self.adapter.count_symbols(
            b"def a():\n    return 1\n\n\ndef b():\n    return 2\n"))

    def test_an_unparsable_block_says_so(self):
        with self.assertRaises(ExtractionError) as caught:
            self.adapter.count_symbols(b"def f(:\n")
        self.assertIn("not parsable", str(caught.exception))
