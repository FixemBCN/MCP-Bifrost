"""
Tests for the PreToolUse nudge and its settings.json wiring.

The scenario that motivates this file: a client lists mcp-bifrost's tools
only by name until something asks for the schema, and nothing about
repeating a manual edit prompts that ask on its own. `init_hook` is the fix
that ships with the package rather than living only in one person's global
config — these tests are mostly about it being safe to run against a
settings.json that already has other things in it.
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp_bifrost.hooks import MARKER, hook_guard_output, init_hook  # noqa: E402
from mcp_bifrost.server import main  # noqa: E402


class HookGuardOutputTests(unittest.TestCase):

    def test_shape(self):
        out = hook_guard_output()
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"],
                         "PreToolUse")
        self.assertIn("mcp__bifrost__",
                      out["hookSpecificOutput"]["additionalContext"])


class InitHookTests(unittest.TestCase):

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / ".claude" / "settings.json"

    def _block(self, data: dict) -> dict:
        for block in data["hooks"]["PreToolUse"]:
            if block["matcher"] == "Edit|Write":
                return block
        raise AssertionError("no Edit|Write matcher written")

    def test_creates_file_and_directory(self):
        init_hook(self.path)
        self.assertTrue(self.path.exists())
        data = json.loads(self.path.read_text())
        commands = [h["command"] for h in self._block(data)["hooks"]]
        self.assertIn(MARKER, commands)

    def test_idempotent(self):
        init_hook(self.path)
        init_hook(self.path)
        data = json.loads(self.path.read_text())
        commands = [h["command"] for h in self._block(data)["hooks"]]
        self.assertEqual(commands.count(MARKER), 1)

    def test_preserves_unrelated_settings(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({
            "theme": "dark",
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash|Grep",
                     "hooks": [{"type": "command", "command": "some-other-tool"}]},
                ]
            },
        }))

        init_hook(self.path)

        data = json.loads(self.path.read_text())
        self.assertEqual(data["theme"], "dark")
        bash_block = next(b for b in data["hooks"]["PreToolUse"]
                          if b["matcher"] == "Bash|Grep")
        self.assertEqual(bash_block["hooks"][0]["command"], "some-other-tool")
        self.assertIn(MARKER,
                      [h["command"] for h in self._block(data)["hooks"]])

    def test_preserves_other_hooks_on_the_same_matcher(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Edit|Write",
                     "hooks": [{"type": "command", "command": "graphify hook-guard"}]},
                ]
            },
        }))

        init_hook(self.path)

        data = json.loads(self.path.read_text())
        commands = [h["command"] for h in self._block(data)["hooks"]]
        self.assertIn("graphify hook-guard", commands)
        self.assertIn(MARKER, commands)


class MainDispatchTests(unittest.TestCase):
    """
    `hook-guard` and `init-hook` must short-circuit before `main` ever builds
    a `DeepSeekWorker` — otherwise a missing DEEPSEEK_API_KEY would break the
    one command meant to work with zero configuration.
    """

    def _run(self, *argv: str) -> str:
        old_argv = sys.argv
        old_stdout = sys.stdout
        sys.argv = ["mcp-bifrost", *argv]
        sys.stdout = captured = io.StringIO()
        try:
            code = main()
        finally:
            sys.argv = old_argv
            sys.stdout = old_stdout
        self.assertEqual(code, 0)
        return captured.getvalue()

    def test_hook_guard_dispatch(self):
        out = json.loads(self._run("hook-guard"))
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"],
                         "PreToolUse")

    def test_init_hook_dispatch(self):
        with TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            try:
                os.chdir(tmp)
                self._run("init-hook")
            finally:
                os.chdir(old_cwd)
            data = json.loads((Path(tmp) / ".claude" / "settings.json").read_text())
            self.assertTrue(data["hooks"]["PreToolUse"])


if __name__ == "__main__":
    unittest.main()
