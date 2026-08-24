"""
Classes are symbols.

For most of this project's life the PHP symbol map listed methods and
nothing else. A class had no address, so `insert_symbol` could not add one —
the `symbol_set` gate saw a class arrive with its methods, counted the names
rather than the declarations, refused, and rolled the write back, which made
the refusal look like a gate catching something real. The tool description
promised classes the whole time.

`extract.php` now emits containers — class, interface, trait, enum — as
symbols in their own right, exactly as the Python adapter emits `ClassDef`.
Everything here is the consequence of that, and the two regressions it
turned up in the container tracking:

* the class stack popped on `depth > $depth`, which is never true for a
  class declared at the top level, so it only ever grew. The newest class
  shadowed the rest, which looked correct for classes in sequence and was
  wrong for everything after the last one — a top-level function following a
  class was reported as a method of it;
* the same fault attributed a method of an anonymous class to whichever
  named class happened to precede it.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp_bifrost.engine import Engine  # noqa: E402
from mcp_bifrost.languages.php import PhpAdapter  # noqa: E402
from mcp_bifrost.languages.python import PythonAdapter  # noqa: E402
from mcp_bifrost.worker import WorkerResult  # noqa: E402
from tests.support import requires_git, requires_php  # noqa: E402

FIXTURE = '''<?php
namespace App\\Service;

/**
 * A documented class.
 */
final class Calculator extends Base implements Contract
{
    public function add(int $a, int $b): int
    {
        return $a + $b;
    }
}

abstract class Partial
{
    abstract public function todo(): void;
}

interface Contract
{
    public function add(int $a, int $b): int;
}

trait Helper
{
    public function help(): string { return 'help'; }
}

enum Suit: string
{
    case Hearts = 'H';
    public function colour(): string { return 'red'; }
}

function topLevel(): void {}

$anon = new class {
    public function inAnon(): int { return 1; }
};

$which = Calculator::class;
'''


def php_file(source: str) -> Path:
    path = Path(tempfile.mkdtemp()) / "subject.php"
    path.write_text(source, encoding="utf-8")
    return path


class FixedWorker:
    def __init__(self, out: str) -> None:
        self.out = out
        self.calls: list[dict] = []

    def run(self, payload: dict) -> WorkerResult:
        self.calls.append(payload)
        return WorkerResult(ok=True, out=self.out, why="stub",
                            diff_stat="+1/-0", error=None, ms=1, tokens_in=5,
                            tokens_out=5, cache_hit=0, request_bytes=0,
                            response_bytes=0)


@requires_php
class PhpContainerTest(unittest.TestCase):
    def setUp(self):
        self.adapter = PhpAdapter()
        self.path = php_file(FIXTURE)
        self.symbols = {s.fqn: s for s in self.adapter.symbols(self.path)}

    def test_each_kind_of_container_is_addressable_as_itself(self):
        for fqn, kind in (("Calculator", "class"), ("Partial", "class"),
                          ("Contract", "interface"), ("Helper", "trait"),
                          ("Suit", "enum")):
            with self.subTest(fqn=fqn):
                self.assertIn(fqn, self.symbols)
                self.assertEqual(kind, self.symbols[fqn].kind)
                self.assertTrue(self.symbols[fqn].is_container)

    def test_a_container_spans_its_whole_declaration(self):
        """From the first modifier to the closing brace — the docblock above
        it is offered separately, as it is for a method."""
        raw = self.path.read_bytes()
        calculator = self.symbols["Calculator"]
        block = raw[calculator.start_byte:calculator.end_byte]
        self.assertTrue(block.startswith(b"final class Calculator"),
                        block[:40])
        self.assertTrue(block.rstrip().endswith(b"}"), block[-40:])
        self.assertIn(b"public function add", block)
        self.assertIsNotNone(calculator.doc_start_byte)
        self.assertIn(b"A documented class",
                      raw[calculator.doc_start_byte:calculator.start_byte])

    def test_a_container_slice_is_valid_php_on_its_own(self):
        """The extent is exact or it is nothing: a class that does not parse
        when sliced out would corrupt any file it was written back into."""
        raw = self.path.read_bytes()
        for fqn in ("Calculator", "Contract", "Helper", "Suit"):
            with self.subTest(fqn=fqn):
                sym = self.symbols[fqn]
                out = Path(tempfile.mkdtemp()) / "slice.php"
                out.write_bytes(b"<?php\n" + raw[sym.start_byte:sym.end_byte]
                                + b"\n")
                proc = subprocess.run(["php", "-l", str(out)],
                                      capture_output=True, text=True)
                self.assertEqual(0, proc.returncode,
                                 proc.stdout + proc.stderr)

    def test_methods_still_belong_to_their_container(self):
        method = self.symbols["Calculator::add"]
        self.assertEqual("Calculator", method.cls)
        self.assertEqual("function", method.kind)
        self.assertFalse(method.is_container)

    def test_a_top_level_function_after_a_class_is_top_level(self):
        """The regression: the class stack never popped, so `topLevel` was
        reported as `Suit::topLevel` — the last class to be declared."""
        self.assertIn("topLevel", self.symbols)
        self.assertIsNone(self.symbols["topLevel"].cls)
        self.assertNotIn("Suit::topLevel", self.symbols)

    def test_a_method_of_an_anonymous_class_is_not_adopted_by_the_last_class(self):
        self.assertIn("inAnon", self.symbols)
        self.assertIsNone(self.symbols["inAnon"].cls)

    def test_an_anonymous_class_is_not_a_symbol(self):
        """Nothing without a name can be addressed by name."""
        containers = [s for s in self.symbols.values() if s.is_container]
        self.assertEqual({"Calculator", "Partial", "Contract", "Helper", "Suit"},
                         {c.fqn for c in containers})

    def test_the_class_keyword_constant_is_not_a_symbol(self):
        """`Calculator::class` is a string, not a declaration."""
        self.assertEqual(1, sum(1 for f in self.symbols if f == "Calculator"))

    def test_a_bodyless_method_ends_at_its_semicolon(self):
        raw = self.path.read_bytes()
        todo = self.symbols["Partial::todo"]
        self.assertTrue(todo.abstract)
        self.assertTrue(
            raw[todo.start_byte:todo.end_byte].rstrip().endswith(b";"))

    def test_a_container_declared_inside_a_function_keeps_its_own_name(self):
        path = php_file("<?php\nfunction make(): void {\n    class Inner {\n"
                        "        public function m(): int { return 1; }\n"
                        "    }\n}\n")
        symbols = {s.fqn: s for s in self.adapter.symbols(path)}
        self.assertIn("Inner", symbols)
        self.assertIn("Inner::m", symbols)
        self.assertIsNone(symbols["make"].cls)


@requires_php
class PhpPreambleTest(unittest.TestCase):
    """
    What sits above a declaration and belongs to it.

    A docblock was already understood. PHP 8 attributes were not, and they
    are not decoration: `#[Route('/users')]` above a method is the routing
    table. Inserting between an attribute and its declaration passes every
    gate — the file parses, the symbol set is unchanged — and silently gives
    the attribute to a different method.

    They are kept OUT of the symbol's own bytes, exactly as a docblock is:
    the worker never sees them, never has to reproduce them, and so cannot
    lose them. The Python adapter reaches the same guarantee from the other
    side, by taking decorators INTO the symbol, because `ast` hands over
    their positions and a decorator there reads as part of the definition.
    """

    def setUp(self):
        self.adapter = PhpAdapter()

    def preamble(self, source: str, fqn: str) -> bytes:
        path = php_file(source)
        raw = path.read_bytes()
        sym = {s.fqn: s for s in self.adapter.symbols(path)}[fqn]
        if sym.doc_start_byte is None:
            return b""
        return raw[sym.doc_start_byte:sym.start_byte]

    def test_an_attribute_above_a_class_belongs_to_it(self):
        self.assertIn(b"#[Deprecated]", self.preamble(
            "<?php\n#[Deprecated]\nclass Foo {\n    public function a() {}\n}\n",
            "Foo"))

    def test_an_attribute_above_a_method_belongs_to_it(self):
        self.assertIn(b"#[Test]", self.preamble(
            "<?php\nclass Foo {\n    #[Test]\n    public function a() {}\n}\n",
            "Foo::a"))

    def test_a_docblock_and_an_attribute_are_both_taken(self):
        """Including one whose arguments contain an array of their own, which
        is where a naive bracket walk stops early."""
        preamble = self.preamble(
            "<?php\n/** Doc. */\n#[Route('/u', methods: ['GET', 'POST'])]\n"
            "class Foo {\n    public function a() {}\n}\n", "Foo")
        self.assertIn(b"/** Doc. */", preamble)
        self.assertIn(b"#[Route", preamble)

    def test_stacked_attributes_are_all_taken(self):
        preamble = self.preamble(
            "<?php\n#[A]\n#[B]\nclass Foo {\n    public function a() {}\n}\n",
            "Foo")
        self.assertIn(b"#[A]", preamble)
        self.assertIn(b"#[B]", preamble)

    def test_an_array_expression_above_is_not_an_attribute(self):
        """`$x = [1, 2];` also ends in `]`. Walking back from it must land on
        a plain `[` and stop, not swallow the statement."""
        self.assertEqual(b"", self.preamble(
            "<?php\n$x = [1, 2];\nfunction f() {}\n", "f"))

    def test_the_attribute_stays_outside_the_symbols_own_bytes(self):
        """So a rewrite cannot drop it: the worker is never handed it."""
        path = php_file(
            "<?php\nclass Foo {\n    #[Test]\n    public function a() {}\n}\n")
        raw = path.read_bytes()
        sym = {s.fqn: s for s in self.adapter.symbols(path)}["Foo::a"]
        self.assertNotIn(b"#[Test]", raw[sym.start_byte:sym.end_byte])


@requires_php
@requires_git
class InsertBeforeAnAttributeTest(unittest.TestCase):
    """The failure the preamble exists to prevent, end to end."""

    SOURCE = ("<?php\n\nclass Router\n{\n    #[Route('/users')]\n"
              "    public function users(): int\n    {\n        return 1;\n"
              "    }\n}\n")

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)
        for key, value in (("user.email", "t@example.invalid"), ("user.name", "T")):
            subprocess.run(["git", "-C", str(self.repo), "config", key, value],
                           check=True)
        self.path = self.repo / "router.php"
        self.path.write_text(self.SOURCE, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "i"],
                       check=True)

    def test_inserting_before_a_decorated_method_does_not_steal_its_attribute(self):
        worker = FixedWorker("public function health(): int\n{\n    return 0;\n}")
        engine = Engine(worker=worker, db_path=self.repo / "log.db",
                        entropy_scan=False)
        outcome = engine.insert_symbol(str(self.path), "Router::users",
                                       "before", "add a health endpoint")
        engine.close()

        self.assertTrue(outcome.ok, outcome.message)
        source = self.path.read_text(encoding="utf-8")
        # The attribute must still sit directly above the method it routes.
        self.assertIn("#[Route('/users')]\n    public function users", source)
        self.assertIn("public function health", source)
        # And the new method must be above the attribute, not between them.
        self.assertLess(source.index("public function health"),
                        source.index("#[Route('/users')]"))


@requires_php
class PhpCountSymbolsTest(unittest.TestCase):
    """What gate 2 counts. A class is one symbol, whatever it contains."""

    def setUp(self):
        self.adapter = PhpAdapter()

    def test_a_bare_method_is_one(self):
        self.assertEqual(1, self.adapter.count_symbols(
            b"public function a(): int { return 1; }"))

    def test_two_methods_are_two(self):
        self.assertEqual(2, self.adapter.count_symbols(
            b"public function a(): int { return 1; }\n"
            b"public function b(): int { return 2; }"))

    def test_a_class_with_methods_is_one(self):
        self.assertEqual(1, self.adapter.count_symbols(
            b"class Foo {\n    public function a(): int { return 1; }\n"
            b"    public function b(): int { return 2; }\n}"))

    def test_two_classes_are_two(self):
        self.assertEqual(2, self.adapter.count_symbols(
            b"class Foo { public function a(): int { return 1; } }\n"
            b"class Bar { public function b(): int { return 2; } }"))

    def test_a_class_with_no_methods_still_counts_as_one(self):
        """It used to count as none, so `create_file` refused a file whose
        class held only constants as 'defining no symbols'."""
        self.assertEqual(1, self.adapter.count_symbols(
            b"<?php\nclass Config {\n    const LIMIT = 10;\n}\n"))


@requires_php
@requires_git
class PhpInsertContainerTest(unittest.TestCase):
    """The operation that could not be performed at all."""

    SOURCE = ("<?php\n\nclass First\n{\n    public function a(): int\n"
              "    {\n        return 1;\n    }\n}\n")

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)
        for key, value in (("user.email", "t@example.invalid"), ("user.name", "T")):
            subprocess.run(["git", "-C", str(self.repo), "config", key, value],
                           check=True)
        self.path = self.repo / "subject.php"
        self.path.write_text(self.SOURCE, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "i"],
                       check=True)
        self.adapter = PhpAdapter()

    def engine(self, block: str) -> Engine:
        return Engine(worker=FixedWorker(block),
                      db_path=self.repo / "log.db", entropy_scan=False)

    def test_a_class_with_methods_can_be_inserted(self):
        block = ("class Second\n{\n    public function b(): int\n    {\n"
                 "        return 2;\n    }\n\n    public function c(): int\n"
                 "    {\n        return 3;\n    }\n}")
        engine = self.engine(block)
        outcome = engine.insert_symbol(str(self.path), "First", "after",
                                       "add a sibling class")
        engine.close()

        self.assertTrue(outcome.ok, outcome.message)
        fqns = [s.fqn for s in self.adapter.symbols(self.path)]
        self.assertEqual(["First", "First::a", "Second", "Second::b",
                          "Second::c"], fqns)

    def test_end_of_class_against_the_class_itself_appends_a_member(self):
        """The natural way to say 'add a method to First', and impossible
        until the class had an address."""
        engine = self.engine("public function z(): int\n{\n    return 26;\n}")
        outcome = engine.insert_symbol(str(self.path), "First",
                                       "end_of_class", "add a method")
        engine.close()

        self.assertTrue(outcome.ok, outcome.message)
        source = self.path.read_text(encoding="utf-8")
        self.assertIn("    public function z(): int", source,
                      "the member did not take its siblings' indentation")
        self.assertEqual(["First", "First::a", "First::z"],
                         [s.fqn for s in self.adapter.symbols(self.path)])

    def test_end_of_class_fills_an_empty_class_without_a_blank_line_first(self):
        self.path.write_text("<?php\n\nclass Empty_\n{\n}\n", encoding="utf-8")
        engine = self.engine("public function only(): int\n{\n    return 1;\n}")
        outcome = engine.insert_symbol(str(self.path), "Empty_",
                                       "end_of_class", "add the first method")
        engine.close()

        self.assertTrue(outcome.ok, outcome.message)
        source = self.path.read_text(encoding="utf-8")
        self.assertIn("{\n    public function only(): int", source,
                      f"a blank line opened the class body: {source!r}")
        self.assertEqual(["Empty_", "Empty_::only"],
                         [s.fqn for s in self.adapter.symbols(self.path)])

    def test_a_whole_class_can_be_rewritten(self):
        """Gate 2 counts one symbol for a class, so this is now allowed."""
        engine = self.engine("class First\n{\n    public function a(): int\n"
                             "    {\n        return 111;\n    }\n}")
        outcome = engine.fix_symbol(str(self.path), "First",
                                    "return 111 instead")
        engine.close()

        self.assertTrue(outcome.ok, outcome.message)
        self.assertIn("return 111", self.path.read_text(encoding="utf-8"))

    def test_a_container_written_on_one_line_is_filled_correctly(self):
        """
        `class NotFound extends Exception {}` is a common shape, and its
        closing brace shares a line with the opening one. Inserting there has
        to break the line itself; nothing else will.
        """
        self.path.write_text("<?php\n\nclass Marker {}\n", encoding="utf-8")
        engine = self.engine("public function only(): int\n{\n    return 1;\n}")
        outcome = engine.insert_symbol(str(self.path), "Marker",
                                       "end_of_class", "add the first method")
        engine.close()

        self.assertTrue(outcome.ok, outcome.message)
        source = self.path.read_text(encoding="utf-8")
        self.assertIn("class Marker {\n    public function only(): int", source)
        self.assertTrue(source.rstrip().endswith("}"),
                        f"the class was left unclosed: {source!r}")
        self.assertEqual(["Marker", "Marker::only"],
                         [s.fqn for s in self.adapter.symbols(self.path)])


@requires_git
class PythonEndOfClassTest(unittest.TestCase):
    """The same operation on the other adapter, which always had classes."""

    SOURCE = "class First:\n    def a(self):\n        return 1\n"

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)
        for key, value in (("user.email", "t@example.invalid"), ("user.name", "T")):
            subprocess.run(["git", "-C", str(self.repo), "config", key, value],
                           check=True)
        self.path = self.repo / "subject.py"
        self.path.write_text(self.SOURCE, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "i"],
                       check=True)
        self.adapter = PythonAdapter()

    def engine(self, block: str) -> Engine:
        return Engine(worker=FixedWorker(block),
                      db_path=self.repo / "log.db", entropy_scan=False)

    def test_end_of_class_appends_a_method_at_the_members_indentation(self):
        engine = self.engine("def z(self):\n    return 26")
        outcome = engine.insert_symbol(str(self.path), "First",
                                       "end_of_class", "add a method")
        engine.close()

        self.assertTrue(outcome.ok, outcome.message)
        source = self.path.read_text(encoding="utf-8")
        self.assertIn("        return 1\n\n    def z(self):\n", source)
        self.assertEqual(["First", "First.a", "First.z"],
                         [s.fqn for s in self.adapter.symbols(self.path)])
