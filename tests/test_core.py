"""
Test suite for MCP-Bifrost core modules: languages/__init__.py, languages/php.py,
gates.py, patcher.py.

stdlib unittest only. Every assertion here is meant to be able to fail: where
an assertion would hold purely by construction of the code path under test,
it has been replaced by (or paired with) a check on actual content, so that a
real regression in offset math, indentation, gate logic, or file I/O trips it.

Run: python3 -m unittest discover tests -v   (from the repo root)
"""

from __future__ import annotations

import ast
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Make the package importable regardless of how `unittest discover` sets
# sys.path (its top-level-dir inference is not reliable across versions).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.support import requires_git, requires_php  # noqa: E402

from mcp_bifrost.languages import (  # noqa: E402
    ExtractionError,
    Symbol,
    apply_indent,
    strip_indent,
)
from mcp_bifrost.languages.php import PhpAdapter  # noqa: E402
from mcp_bifrost.gates import (  # noqa: E402
    check_offsets,
    check_single_symbol,
    check_size,
    check_substance,
    check_syntax,
)
from mcp_bifrost.patcher import (  # noqa: E402
    Applied,
    PatchError,
    apply_block,
    atomic_write,
    read_blob,
    repo_root,
    revert,
    stash_blob,
)


# --------------------------------------------------------------------- utils

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True,
    )


def make_git_repo(root: Path) -> Path:
    """Init a git repo at `root` with a local identity, no commits yet."""
    proc = _git(root, "init", "-q")
    assert proc.returncode == 0, proc.stderr
    _git(root, "config", "user.email", "bifrost-tests@example.com")
    _git(root, "config", "user.name", "Bifrost Tests")
    return root


def commit_file(repo: Path, rel_path: str, content: bytes) -> Path:
    path = repo / rel_path
    path.write_bytes(content)
    proc = _git(repo, "add", rel_path)
    assert proc.returncode == 0, proc.stderr
    proc = _git(repo, "commit", "-q", "-m", "fixture")
    assert proc.returncode == 0, proc.stderr
    return path


# ------------------------------------------------------ 1. byte-offset trap

CALC_PHP = """<?php

class Calculator
{
    // càlcul de la línia — comentari inicial amb accents
    // segona línia amb més caràcters multibyte: ó, ï, ü, ñ, ç

    public function compute(int $a, int $b): int
    {
        $sum = $a + $b;

        return $sum;
    }
}
""".encode("utf-8")


@requires_git
class ByteOffsetTrapTest(unittest.TestCase):
    """
    The bug the whole design turns on: PHP reports byte offsets, and a
    fixture with multi-byte characters before the target symbol will expose
    any code path that accidentally indexes with characters instead.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = make_git_repo(Path(self._tmp.name))
        self.path = commit_file(self.repo, "calc.php", CALC_PHP)
        self.php = PhpAdapter()

    def tearDown(self):
        self._tmp.cleanup()

    def test_fixture_actually_arms_the_trap(self):
        # If this doesn't hold, byte and character offsets coincide and the
        # rest of this test class is not testing anything.
        source_bytes = self.path.read_bytes()
        source_text = source_bytes.decode("utf-8")
        self.assertNotEqual(len(source_bytes), len(source_text))

    @requires_php
    def test_extraction_lands_on_the_correct_bytes(self):
        source_bytes = self.path.read_bytes()
        sym = self.php.find(self.path, "Calculator::compute")
        block = sym.extract(source_bytes)

        # A char-offset bug here would shift the cut: the block would start
        # a few bytes early (mid multibyte-comment) or late (mid keyword).
        self.assertTrue(
            block.startswith(b"public function compute"),
            f"block starts with {block[:40]!r}",
        )
        # None of the accented comment text should have leaked in.
        self.assertNotIn("càlcul".encode("utf-8"), block)
        self.assertNotIn("línia".encode("utf-8"), block)
        # A boundary landing mid-character would make this raise.
        block.decode("utf-8")

    @requires_php
    def test_extraction_splice_roundtrip_under_multibyte_prefix(self):
        source_bytes = self.path.read_bytes()
        sym = self.php.find(self.path, "Calculator::compute")
        sent_src = sym.extract(source_bytes)

        new_block = sent_src.replace(
            b"$sum = $a + $b;", b"$sum = $a + $b; // suma total"
        )
        self.assertNotEqual(new_block, sent_src)  # guard: the edit landed

        result = apply_block(
            self.path, sym.start_byte, sym.end_byte, sent_src, new_block,
            self.php,
        )
        self.assertIsInstance(result, Applied, getattr(result, "detail", result))

        on_disk = self.path.read_bytes()
        # The multibyte prefix before the symbol must be untouched, byte for
        # byte; a char/byte mixup would have shifted the splice point.
        self.assertEqual(on_disk[:sym.start_byte], source_bytes[:sym.start_byte])
        self.assertIn("càlcul".encode("utf-8"), on_disk)
        self.assertIn(b"suma total", on_disk)
        # Whole file must still decode cleanly.
        on_disk.decode("utf-8")


# --------------------------------------------------------- 2. indentation

class IndentRoundTripTest(unittest.TestCase):
    def test_roundtrip_with_blank_line_and_unindented_first_line(self):
        indent = "    "
        block = (
            b"public function foo(): int\n"
            b"    {\n"
            b"        $x = 1;\n"
            b"\n"
            b"        return $x;\n"
            b"    }"
        )
        stripped = strip_indent(block, indent)
        self.assertEqual(apply_indent(stripped, indent), block)

    def test_roundtrip_with_tabs(self):
        indent = "\t"
        block = (
            b"public function foo()\n"
            b"\t{\n"
            b"\t\t$x = 1;\n"
            b"\t}"
        )
        stripped = strip_indent(block, indent)
        self.assertEqual(apply_indent(stripped, indent), block)

    def test_roundtrip_with_empty_indent(self):
        block = b"public function foo()\n{\n    return 1;\n}"
        stripped = strip_indent(block, "")
        self.assertEqual(apply_indent(stripped, ""), block)

    def test_strip_removes_indent_from_body_lines(self):
        indent = "    "
        block = b"public function foo()\n    {\n        return 1;\n    }"
        stripped = strip_indent(block, indent)
        lines = stripped.split(b"\n")
        self.assertEqual(lines[0], b"public function foo()")
        self.assertEqual(lines[1], b"{")
        self.assertEqual(lines[2], b"    return 1;")
        self.assertEqual(lines[3], b"}")

    def test_apply_indent_does_not_pad_first_line(self):
        block = b"foo\nbar\nbaz"
        out = apply_indent(block, "  ")
        lines = out.split(b"\n")
        self.assertEqual(lines[0], b"foo")  # not b"  foo"
        self.assertEqual(lines[1], b"  bar")
        self.assertEqual(lines[2], b"  baz")

    def test_apply_indent_does_not_pad_blank_lines(self):
        block = b"foo\n\nbar"
        out = apply_indent(block, "  ")
        lines = out.split(b"\n")
        self.assertEqual(lines[1], b"")  # stayed blank, no trailing padding


# ------------------------------------------------------------ 3. gate 0

class CheckOffsetsTest(unittest.TestCase):
    def setUp(self):
        self.current = b"<?php\nclass Foo\n{\n    public function bar()\n    {\n        return 1;\n    }\n}\n"
        self.start = self.current.index(b"public function bar")
        self.end = self.start + len(b"public function bar()\n    {\n        return 1;\n    }")
        self.sent = self.current[self.start:self.end]

    def test_passes_when_block_unchanged(self):
        result = check_offsets(self.current, self.start, self.end, self.sent)
        self.assertTrue(result)

    def test_fails_when_file_changed_under_us(self):
        mutated = self.current.replace(b"return 1;", b"return 2;")
        result = check_offsets(mutated, self.start, self.end, self.sent)
        self.assertFalse(result)
        self.assertEqual(result.gate, "offsets")

    def test_fails_when_range_runs_past_eof(self):
        result = check_offsets(
            self.current, self.start, len(self.current) + 1, self.sent
        )
        self.assertFalse(result)

    def test_fails_when_off_by_one_byte_at_end(self):
        result = check_offsets(self.current, self.start, self.end - 1, self.sent)
        self.assertFalse(result)

    def test_fails_when_off_by_one_byte_at_start(self):
        result = check_offsets(self.current, self.start + 1, self.end, self.sent)
        self.assertFalse(result)


# ------------------------------------------------------------ 4. gate 1

@requires_php
class CheckSyntaxTest(unittest.TestCase):
    def setUp(self):
        self.php = PhpAdapter()

    def test_passes_on_valid_php(self):
        valid = b"<?php\nclass Foo\n{\n    public function bar()\n    {\n        return 1;\n    }\n}\n"
        result = check_syntax(self.php, valid)
        self.assertTrue(result)

    def test_fails_on_unbalanced_brace(self):
        broken = b"<?php\nclass Foo\n{\n    public function bar()\n    {\n        return 1;\n    // missing closing braces\n"
        result = check_syntax(self.php, broken)
        self.assertFalse(result)
        self.assertEqual(result.gate, "syntax")


# ------------------------------------------------------------ 5. gate 2

class CheckSingleSymbolTest(unittest.TestCase):
    def setUp(self):
        self.php = PhpAdapter()

    @requires_php
    def test_passes_for_one_method(self):
        block = b"public function foo()\n{\n    return 1;\n}"
        result = check_single_symbol(self.php, block)
        self.assertTrue(result)

    def test_fails_for_two_methods(self):
        block = (
            b"public function foo()\n{\n    return 1;\n}\n"
            b"public function bar()\n{\n    return 2;\n}"
        )
        result = check_single_symbol(self.php, block)
        self.assertFalse(result)
        self.assertEqual(result.gate, "single_symbol")


# ------------------------------------------------------------ 6. gate 3

class CheckSubstanceTest(unittest.TestCase):
    def test_passes_when_only_a_docblock_is_added(self):
        before = b"public function calc($a, $b)\n{\n    return $a + $b;\n}"
        after = (
            b"/**\n * Adds two numbers.\n */\n"
            b"public function calc($a, $b)\n{\n    return $a + $b;\n}"
        )
        result = check_substance(before, after)
        self.assertTrue(result)

    def test_fails_when_a_call_is_removed(self):
        before = b"public function calc()\n{\n    doSomething();\n    return 1;\n}"
        after = b"public function calc()\n{\n    return 1;\n}"
        result = check_substance(before, after)
        self.assertFalse(result)
        self.assertIn("doSomething", result.detail)

    def test_fails_when_a_variable_is_removed(self):
        before = b"public function calc($x)\n{\n    $y = $x + 1;\n    return $y;\n}"
        after = b"public function calc($x)\n{\n    return $x + 1;\n}"
        result = check_substance(before, after)
        self.assertFalse(result)
        self.assertIn("y", result.detail)

    def test_fails_when_an_if_is_removed(self):
        before = (
            b"public function calc($x)\n{\n"
            b"    if ($x > 0) {\n        echo 1;\n    }\n"
            b"    return $x;\n}"
        )
        after = b"public function calc($x)\n{\n    return $x;\n}"
        result = check_substance(before, after)
        self.assertFalse(result)
        self.assertIn("if", result.detail)

    def test_allow_removal_passes_regardless(self):
        before = b"public function calc()\n{\n    doSomething();\n    return 1;\n}"
        after = b"public function calc()\n{\n    return 1;\n}"
        result = check_substance(before, after, allow_removal=True)
        self.assertTrue(result)


# ------------------------------------------------------------ 7. check_size

class CheckSizeTest(unittest.TestCase):
    def test_passes_under_limit(self):
        self.assertTrue(check_size(50, limit=150))

    def test_passes_at_exact_limit(self):
        self.assertTrue(check_size(150, limit=150))

    def test_fails_over_limit(self):
        result = check_size(151, limit=150)
        self.assertFalse(result)
        self.assertEqual(result.gate, "size")


# ------------------------------------------------------------ 8. atomic_write

class AtomicWriteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "target.php"
        self.path.write_bytes(b"<?php\necho 'old';\n")
        os.chmod(self.path, 0o644)

    def tearDown(self):
        self._tmp.cleanup()

    def test_content_is_replaced(self):
        new_content = b"<?php\necho 'new';\n"
        atomic_write(self.path, new_content)
        self.assertEqual(self.path.read_bytes(), new_content)

    def test_permissions_are_preserved(self):
        os.chmod(self.path, 0o644)
        atomic_write(self.path, b"<?php\necho 'new';\n")
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode, 0o644)

    def test_permissions_preserved_for_a_different_mode(self):
        os.chmod(self.path, 0o600)
        atomic_write(self.path, b"<?php\necho 'new';\n")
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode, 0o600)


# ------------------------------------------------------- 9. apply_block e2e

SIMPLE_PHP = b"""<?php

class Greeter
{
    public function greet(string $name): string
    {
        $msg = "hello " . $name;
        return $msg;
    }
}
"""


@requires_php
@requires_git
class ApplyBlockEndToEndTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = make_git_repo(Path(self._tmp.name))
        self.path = commit_file(self.repo, "greeter.php", SIMPLE_PHP)
        self.php = PhpAdapter()
        self.sym = self.php.find(self.path, "Greeter::greet")
        self.original = self.path.read_bytes()
        self.sent_src = self.sym.extract(self.original)

    def tearDown(self):
        self._tmp.cleanup()

    def test_success_returns_applied_with_usable_blob_and_changes_file(self):
        new_block = self.sent_src.replace(
            b'"hello " . $name', b'"hello, " . $name . "!"'
        )
        result = apply_block(
            self.path, self.sym.start_byte, self.sym.end_byte,
            self.sent_src, new_block, self.php,
        )
        self.assertIsInstance(result, Applied)
        self.assertTrue(result.blob_before)

        # blob_before must be a real, readable git blob with the pre-patch
        # content.
        blob_content = read_blob(self.repo, result.blob_before)
        self.assertEqual(blob_content, self.original)

        on_disk = self.path.read_bytes()
        self.assertNotEqual(on_disk, self.original)
        self.assertIn(b"hello, ", on_disk)

    def test_revert_restores_file_byte_for_byte(self):
        new_block = self.sent_src.replace(b"$msg", b"$message")
        result = apply_block(
            self.path, self.sym.start_byte, self.sym.end_byte,
            self.sent_src, new_block, self.php,
        )
        self.assertIsInstance(result, Applied)
        self.assertNotEqual(self.path.read_bytes(), self.original)

        revert(self.path, result.blob_before)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_stale_sent_src_is_rejected_and_file_untouched(self):
        stale_sent_src = self.sent_src.replace(b"hello", b"bonjour")
        new_block = self.sent_src  # irrelevant, offsets gate fires first
        result = apply_block(
            self.path, self.sym.start_byte, self.sym.end_byte,
            stale_sent_src, new_block, self.php,
        )
        self.assertNotIsInstance(result, Applied)
        self.assertFalse(result)
        self.assertEqual(result.gate, "offsets")
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_broken_new_block_is_rejected_and_file_untouched(self):
        broken_block = self.sent_src.rstrip(b"}")  # drop the closing brace
        result = apply_block(
            self.path, self.sym.start_byte, self.sym.end_byte,
            self.sent_src, broken_block, self.php,
        )
        self.assertNotIsInstance(result, Applied)
        self.assertFalse(result)
        self.assertEqual(result.gate, "syntax")
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_apply_block_outside_git_repo_raises(self):
        with tempfile.TemporaryDirectory() as bare_dir:
            bare_path = Path(bare_dir) / "loose.php"
            bare_path.write_bytes(SIMPLE_PHP)
            sym = self.php.find(bare_path, "Greeter::greet")
            src = sym.extract(bare_path.read_bytes())
            with self.assertRaises(PatchError):
                apply_block(
                    bare_path, sym.start_byte, sym.end_byte, src, src, self.php,
                )


# --------------------------------------------------------- 10. PhpAdapter.find

@requires_php
class PhpAdapterFindTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.php = PhpAdapter()

    def tearDown(self):
        self._tmp.cleanup()

    def test_resolves_by_bare_name(self):
        path = self.dir / "one.php"
        path.write_bytes(SIMPLE_PHP)
        sym = self.php.find(path, "greet")
        self.assertEqual(sym.name, "greet")
        self.assertEqual(sym.cls, "Greeter")

    def test_resolves_by_class_colon_colon_method(self):
        path = self.dir / "one.php"
        path.write_bytes(SIMPLE_PHP)
        sym = self.php.find(path, "Greeter::greet")
        self.assertEqual(sym.fqn, "Greeter::greet")

    def test_unknown_name_raises(self):
        path = self.dir / "one.php"
        path.write_bytes(SIMPLE_PHP)
        with self.assertRaises(ExtractionError):
            self.php.find(path, "doesNotExist")

    def test_ambiguous_bare_name_raises(self):
        content = b"""<?php

class Alpha
{
    public function run(): int
    {
        return 1;
    }
}

class Beta
{
    public function run(): int
    {
        return 2;
    }
}
"""
        path = self.dir / "two.php"
        path.write_bytes(content)
        with self.assertRaises(ExtractionError):
            self.php.find(path, "run")
        # Qualified lookups must still resolve distinctly.
        alpha = self.php.find(path, "Alpha::run")
        beta = self.php.find(path, "Beta::run")
        self.assertEqual(alpha.cls, "Alpha")
        self.assertEqual(beta.cls, "Beta")
        self.assertNotEqual(alpha.start_byte, beta.start_byte)


if __name__ == "__main__":
    unittest.main()


@requires_php
class TestInterpolationBraces(unittest.TestCase):
    """
    Regression: PHP tokenises the '{' of "{$a['b']}" as T_CURLY_OPEN — a named
    token — but its matching '}' as a bare character. An extractor that counts
    only bare characters sees one close too many, the depth goes negative
    early, and the symbol is silently truncated.

    This corrupted a real patch: the worker balanced the truncated block by
    adding the brace it appeared to be missing, and splicing that back left
    the file with one '}' too many.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.adapter = PhpAdapter()

    def _fixture(self, body: str) -> Path:
        p = self.dir / "Interp.php"
        p.write_text(
            "<?php\n"
            "class Interp {\n"
            "    public function target($client) {\n"
            f"{body}"
            "    }\n\n"
            "    public function after() {\n"
            "        return 2;\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )
        return p

    def test_curly_interpolation_does_not_truncate(self):
        p = self._fixture(
            '        $s = "Hola {$client[\'nom\']}, benvingut";\n'
            "        return $s;\n"
        )
        sym = self.adapter.find(p, "target")
        block = sym.extract(p.read_bytes())
        self.assertTrue(block.rstrip().endswith(b"}"),
                        f"symbol truncated: ends {block[-30:]!r}")
        self.assertIn(b"return $s;", block)
        self.assertNotIn(b"public function after", block)

    def test_dollar_curly_interpolation(self):
        p = self._fixture('        return "x ${client} y";\n')
        sym = self.adapter.find(p, "target")
        block = sym.extract(p.read_bytes())
        self.assertTrue(block.rstrip().endswith(b"}"))
        self.assertNotIn(b"public function after", block)

    def test_extracted_block_reinjects_cleanly(self):
        """The authoritative check: put it back, the file must still parse."""
        p = self._fixture(
            '        $a = "{$client[\'x\']}";\n'
            '        $b = "{$client[\'y\']}";\n'
            "        return $a . $b;\n"
        )
        src = p.read_bytes()
        sym = self.adapter.find(p, "target")
        rebuilt = src[:sym.start_byte] + sym.extract(src) + src[sym.end_byte:]
        self.assertEqual(rebuilt, src)
        ok, msg = self.adapter.validate(rebuilt)
        self.assertTrue(ok, msg)

    def test_every_symbol_in_fixture_is_balanced(self):
        """Arms the test: without interpolation present it proves nothing."""
        p = self._fixture('        return "{$client[\'z\']}";\n')
        self.assertIn(b"{$", p.read_bytes(), "fixture lost its interpolation")
        for sym in self.adapter.symbols(p):
            block = sym.extract(p.read_bytes())
            self.assertEqual(block.count(b"{") - block.count(b"}"), 0,
                             f"{sym.fqn} unbalanced")


class TestPackaging(unittest.TestCase):
    """
    The PHP parser is a data file inside a Python package.

    A wheel built without it installs cleanly and then fails at runtime on
    every PHP operation — the worst shape a packaging bug can take, because
    nothing is wrong until the first real use. These assertions fail if the
    package-data declaration is ever dropped.
    """

    def test_extractor_ships_beside_the_module(self):
        from mcp_bifrost.languages.php import EXTRACTOR
        self.assertTrue(EXTRACTOR.exists(), f"{EXTRACTOR} is missing")
        self.assertIn("mcp_bifrost", EXTRACTOR.parts)
        self.assertEqual(EXTRACTOR.suffix, ".php")

    def test_extractor_is_resolved_relative_to_the_package(self):
        """Not relative to the working directory: an installed package is
        never run from its own source tree."""
        import mcp_bifrost.languages.php as mod
        from mcp_bifrost.languages.php import EXTRACTOR
        self.assertTrue(
            str(EXTRACTOR).startswith(str(Path(mod.__file__).parent)),
            "the extractor path is not anchored to the package directory")

    def test_pyproject_declares_the_php_file(self):
        root = Path(__file__).resolve().parent.parent
        pyproject = root / "pyproject.toml"
        if not pyproject.exists():
            self.skipTest("running outside the source tree")
        text = pyproject.read_text(encoding="utf-8")
        self.assertIn("package-data", text)
        self.assertIn('"*.php"', text)


class VersionConsistencyTest(unittest.TestCase):
    """
    One version number, declared in four places.

    `pyproject.toml` is what PyPI publishes, `server.json` is what the MCP
    registry ingests (twice: the server and the package it points at), and
    `serverInfo` is what a client sees on `initialize`. A mismatch between
    the first two is rejected by the registry days after the fact; a mismatch
    with the third is never rejected at all and simply reports the wrong
    version forever. They are bumped by hand, so this is the check that they
    were all bumped together.
    """

    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parent.parent

    def _read(self, name: str) -> str:
        p = self.root / name
        if not p.exists():
            self.skipTest("running outside the source tree")
        return p.read_text(encoding="utf-8")

    def test_all_four_declarations_agree(self):
        import tomllib

        pyproject = tomllib.loads(self._read("pyproject.toml"))
        declared = pyproject["project"]["version"]

        server_json = json.loads(self._read("server.json"))
        found = {
            "pyproject.toml": declared,
            "server.json:server": server_json["version"],
        }
        for i, pkg in enumerate(server_json["packages"]):
            found[f"server.json:packages[{i}]"] = pkg["version"]

        from mcp_bifrost import server as server_mod
        m = re.search(r'"serverInfo":\s*\{[^}]*"version":\s*"([^"]+)"',
                      Path(server_mod.__file__).read_text(encoding="utf-8"))
        self.assertIsNotNone(m, "serverInfo carries no version literal")
        found["server.py:serverInfo"] = m.group(1)

        disagreeing = {k: v for k, v in found.items() if v != declared}
        self.assertEqual(
            {}, disagreeing,
            f"version is {declared} in pyproject.toml but differs in: {disagreeing}")

    def test_the_changelog_documents_the_declared_version(self):
        """A release nobody wrote down is a release nobody can audit."""
        import tomllib

        declared = tomllib.loads(self._read("pyproject.toml"))["project"]["version"]
        changelog = self._read("CHANGELOG.md")
        headings = re.findall(r"^##\s+([0-9]+\.[0-9]+\.[0-9]+)", changelog, re.M)
        self.assertIn(
            declared, headings,
            f"CHANGELOG.md has no section for {declared} (found {headings})")
        self.assertEqual(
            declared, headings[0],
            f"CHANGELOG.md leads with {headings[0]}, not the declared {declared}")


class MinimumPythonSyntaxTest(unittest.TestCase):
    """
    Every module must parse under the oldest Python the package claims.

    `f"+{block.count(b'\\n') + 1}/-0"` is ordinary in 3.12 and a SyntaxError
    in 3.11, which PEP 701 changed. The package declares 3.11 as its
    minimum, so that line did not fail a test — it stopped `mcp_bifrost`
    from importing at all for everyone on the declared minimum, while
    passing cleanly on the author's newer interpreter. CI found it; nothing
    running locally could.

    `ast.parse(..., feature_version=(3, 11))` is no help: the 3.12 tokenizer
    reads f-strings its own way whatever version is asked of it. So the
    construct is what gets checked, by walking the tree and reading each
    interpolated expression back out of the source.
    """

    @staticmethod
    def _modules() -> list[Path]:
        root = Path(__file__).resolve().parent.parent / "mcp_bifrost"
        return sorted(root.rglob("*.py"))

    def test_no_f_string_expression_contains_a_backslash(self):
        offenders = []
        for module in self._modules():
            source = module.read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.FormattedValue):
                    continue
                segment = ast.get_source_segment(source, node.value) or ""
                if "\\" in segment:
                    offenders.append(f"{module.name}:{node.lineno}: {segment}")
        self.assertEqual(
            [], offenders,
            "a backslash inside an f-string expression is a SyntaxError "
            "before Python 3.12, which this package supports: "
            + "; ".join(offenders))

    def test_the_declared_minimum_is_the_one_this_checks_against(self):
        """If the floor is raised past 3.12 the check above stops being
        necessary, and should go rather than linger as folklore."""
        import tomllib

        root = Path(__file__).resolve().parent.parent
        pyproject = root / "pyproject.toml"
        if not pyproject.exists():
            self.skipTest("running outside the source tree")
        requires = tomllib.loads(
            pyproject.read_text(encoding="utf-8"))["project"]["requires-python"]
        self.assertEqual(
            ">=3.11", requires,
            "the minimum Python changed; revisit the f-string check above")
