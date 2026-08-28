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


class HookGuardDenyTests(unittest.TestCase):
    """
    RF-13's mechanical gate: `Write` to a file that does not exist, in a
    directory where >=3 siblings already share its extension, is denied
    rather than merely nudged — the one trigger the hook payload alone can
    prove without any judgment call.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _write_payload(self, file_path) -> dict:
        return {"tool_name": "Write", "tool_input": {"file_path": str(file_path)}}

    def test_denies_a_new_file_matching_an_established_pattern(self):
        for name in ("alpha.py", "beta.py", "gamma.py"):
            (self.dir / name).write_text("# fixture\n")
        target = self.dir / "delta.py"

        out = hook_guard_output(self._write_payload(target))

        spec = out["hookSpecificOutput"]
        self.assertEqual(spec.get("permissionDecision"), "deny")
        self.assertIn("create_file", spec["permissionDecisionReason"])
        self.assertIn("delta.py", spec["permissionDecisionReason"])

    def test_names_the_most_similarly_named_sibling_as_model_from(self):
        (self.dir / "widget_alpha.py").write_text("# fixture\n")
        (self.dir / "widget_beta.py").write_text("# fixture\n")
        (self.dir / "totally_unrelated.py").write_text("# fixture\n")
        target = self.dir / "widget_gamma.py"

        out = hook_guard_output(self._write_payload(target))

        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("widget_", reason)
        self.assertNotIn("totally_unrelated.py", reason)

    def test_stays_advisory_below_the_sibling_threshold(self):
        (self.dir / "alpha.py").write_text("# fixture\n")
        (self.dir / "beta.py").write_text("# fixture\n")  # only 2, need 3
        target = self.dir / "gamma.py"

        out = hook_guard_output(self._write_payload(target))

        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])
        self.assertIn("additionalContext", out["hookSpecificOutput"])

    def test_stays_advisory_when_the_target_already_exists(self):
        for name in ("alpha.py", "beta.py", "gamma.py"):
            (self.dir / name).write_text("# fixture\n")
        target = self.dir / "alpha.py"  # already exists — this is an edit

        out = hook_guard_output(self._write_payload(target))

        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])

    def test_stays_advisory_for_a_different_extension(self):
        for name in ("alpha.py", "beta.py", "gamma.py"):
            (self.dir / name).write_text("# fixture\n")
        target = self.dir / "config.json"  # no .json siblings

        out = hook_guard_output(self._write_payload(target))

        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])

    def test_stays_advisory_for_a_non_write_tool(self):
        for name in ("alpha.py", "beta.py", "gamma.py"):
            (self.dir / name).write_text("# fixture\n")
        target = self.dir / "delta.py"
        payload = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}

        out = hook_guard_output(payload)

        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])

    def test_stays_advisory_with_no_payload_at_all(self):
        out = hook_guard_output(None)
        self.assertNotIn("permissionDecision", out["hookSpecificOutput"])


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

    def _run(self, *argv: str, stdin: str = "") -> str:
        old_argv = sys.argv
        old_stdout = sys.stdout
        old_stdin = sys.stdin
        sys.argv = ["mcp-bifrost", *argv]
        sys.stdout = captured = io.StringIO()
        sys.stdin = io.StringIO(stdin)  # not a tty: isatty() is False
        try:
            code = main()
        finally:
            sys.argv = old_argv
            sys.stdout = old_stdout
            sys.stdin = old_stdin
        self.assertEqual(code, 0)
        return captured.getvalue()

    def test_hook_guard_dispatch(self):
        out = json.loads(self._run("hook-guard"))
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"],
                         "PreToolUse")

    def test_hook_guard_dispatch_denies_over_real_stdin(self):
        """The stdin plumbing end to end: main() -> hook_guard() ->
        _read_payload() -> hook_guard_output(), not just the pure function."""
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for name in ("alpha.py", "beta.py", "gamma.py"):
                (tmp_path / name).write_text("# fixture\n")
            target = tmp_path / "delta.py"
            payload = json.dumps({
                "tool_name": "Write",
                "tool_input": {"file_path": str(target)},
            })

            out = json.loads(self._run("hook-guard", stdin=payload))

        self.assertEqual(out["hookSpecificOutput"].get("permissionDecision"),
                         "deny")

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
