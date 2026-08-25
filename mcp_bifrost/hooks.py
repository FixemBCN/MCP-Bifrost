"""
The editor-side nudge that keeps this server's tools from going unused.

A deferred MCP tool is listed to the client by bare name, with its
description hidden until something asks for it. Nothing about repeating the
same mechanical edit five times in a row prompts that ask — see the incident
in docs/manual.md that motivated this file: `insert_case` sat unused for an
entire session, hand-edited duplicates of it collided across two parallel
edits, and the tool was only found because someone asked what it did.

A Claude Code `PreToolUse` hook on `Edit`/`Write` closes that gap by
injecting a reminder every time a manual edit is about to happen. This module
is both halves of that: the reminder the hook prints (`hook_guard`), and the
settings.json surgery that wires the hook up (`init_hook`), so installing the
nudge is one command rather than a snippet to copy by hand.
"""

from __future__ import annotations

import json
from pathlib import Path

# Substring used to recognise our own hook entry on a second run, and to
# avoid adding it twice. Deliberately just the command, not the whole JSON
# blob below, so a future wording change to REMINDER does not orphan the one
# already written to someone's settings.json.
MARKER = "mcp-bifrost hook-guard"

REMINDER = (
    "MANDATORY: before hand-editing, check whether a deferred "
    "mcp__bifrost__* tool fits better: insert_case (new switch branch), "
    "insert_symbol (new method/class beside an existing one), fix_symbol "
    "(rewrite one named symbol), fix_symbols (same change across many "
    "symbols), create_file+model_from (new file by analogy to a sibling), "
    "patch_group (atomic multi-file change). These are deferred — call "
    "ToolSearch for them; do not assume unavailable just because not listed."
)


def hook_guard_output() -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": REMINDER,
        }
    }


def hook_guard() -> int:
    """Entry point for `mcp-bifrost hook-guard`, invoked by the hook itself."""
    print(json.dumps(hook_guard_output()))
    return 0


def init_hook(settings_path: Path) -> str:
    """
    Merge a PreToolUse hook for `Edit|Write` into settings_path, additively.

    Additive on purpose: settings.json is shared with whatever else the user
    has wired up (other tools' hooks, permissions, models), so this only ever
    adds our one hook entry — an existing `Edit|Write` matcher keeps its
    other hooks, and every other matcher is untouched. Idempotent: run twice
    and the second run is a no-op.
    """
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(settings_path.read_text()) if settings_path.exists() else {}

    pre = data.setdefault("hooks", {}).setdefault("PreToolUse", [])

    for block in pre:
        if block.get("matcher") == "Edit|Write":
            if any(MARKER in h.get("command", "")
                   for h in block.get("hooks", [])):
                return f"already present in {settings_path}"
            block.setdefault("hooks", []).append(
                {"type": "command", "command": MARKER})
            break
    else:
        pre.append({
            "matcher": "Edit|Write",
            "hooks": [{"type": "command", "command": MARKER}],
        })

    settings_path.write_text(json.dumps(data, indent=2) + "\n")
    return f"added to {settings_path}"


def init_hook_main(argv: list[str]) -> int:
    """Entry point for `mcp-bifrost init-hook`."""
    target = (Path.home() if "--global" in argv else Path.cwd()) \
        / ".claude" / "settings.json"
    print(f"mcp-bifrost: {init_hook(target)}")
    return 0
