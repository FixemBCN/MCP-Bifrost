"""
The editor-side nudge that keeps this server's tools from going unused.

A deferred MCP tool is listed to the client by bare name, with its
description hidden until something asks for it. Nothing about repeating the
same mechanical edit five times in a row prompts that ask — see the incident
in docs/manual.md that motivated this file: `insert_case` sat unused for an
entire session, hand-edited duplicates of it collided across two parallel
edits, and the tool was only found because someone asked what it did.

A Claude Code `PreToolUse` hook on `Edit`/`Write` closes part of that gap by
injecting a reminder every time a manual edit is about to happen — but
`additionalContext` cannot block, and field evidence (docs/critical-review.md,
RF-13) showed a pure reminder gets habituated past by the third repetition:
54 files created in one session, 2 through this server.

A second incident settled it. In a session where the tools had already
been loaded and used once, the reminder fired on every call and was still
passed over for a new switch case, two new functions and two method rewrites
in files this server adapts. A reminder that has already been read and agreed
with does not change the next decision. So the gate no longer relies on being
persuasive.

Two triggers are denied outright rather than nudged. The first is the
mechanical case above — a new file in a directory where several siblings
already share its extension, answered with `create_file(model_from=...)`. The
second is broader and is the one that incident demanded: any `Edit` or `Write`
to an existing file whose extension this server adapts, inside a project whose
`.mcp.json` actually registers this server. That combination is not a judgment
call; it is the exact case every editing tool here was built for.

Both stay honest the same way. The scope is read off configuration on
disk, so a project that never registered this server is never blocked, and a
one-shot marker file lets a genuinely unaddressable edit through at the cost
of one command — a gate with no exit is one that gets removed the first time
it is wrong.

This module is every part of that: the reminder and the deny decision
(`hook_guard`), and the settings.json surgery that wires the hook up
(`init_hook`), so installing it is one command rather than a snippet to copy
by hand.
"""

from __future__ import annotations

import json
import sys
from difflib import SequenceMatcher
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


# Below this many same-extension siblings, "matches an established pattern"
# is not a claim the hook payload alone can back up — one or two files could
# be coincidence, not a convention worth generating by analogy to.
MIN_SIBLINGS = 3

# The file extensions this server has adapters for. Duplicated here
# rather than imported from `engine.ADAPTERS` on purpose — this module
# is loaded on every single Edit and Write in the session, and engine
# pulls in the worker, the gates and the patcher behind it.
# tests/test_hooks.py asserts the two stay in sync, so the duplication
# cannot drift silently.
ADAPTED_EXTENSIONS = frozenset({".php", ".py"})

# Where the one-shot escape marker lives, relative to the project root.
# Inside `.bifrost/` because that directory is already this server's own
# scratch space in a checkout, so the marker needs no new gitignore
# entry.
OVERRIDE_PATH = Path(".bifrost/hook-override")


def _nearest_sibling(target_stem: str, siblings: list[Path]) -> Path:
    """
    The sibling to suggest as `model_from`, picked by name similarity to the
    file about to be created — a mechanical stand-in for "which existing file
    is this one most like", with no read of any file's content required.
    """
    return max(siblings,
              key=lambda p: SequenceMatcher(None, target_stem, p.stem).ratio())


def _bifrost_project_root(target: Path) -> Path | None:
    """
    This is what scopes the block. The deny below only fires where this server
    is genuinely wired up, so a .php or .py file in a project that has never
    heard of mcp-bifrost is never blocked — the hook is installed globally but
    must not hold projects hostage to a tool they do not have. Detection is by
    configuration on disk rather than by a live connection because a PreToolUse
    hook is a separate short-lived process with no view of the client's MCP
    session.
    """
    for directory in (target.parent, *target.parent.parents):
        config_path = directory / ".mcp.json"
        try:
            with config_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
            if any("bifrost" in key.lower() for key in servers):
                return directory
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            continue
    return None


def _consume_override(root: Path) -> bool:
    """
    The escape hatch, deliberately one-shot. Some edits genuinely have no
    symbol to address — a licence header, a stray import, a merge-conflict
    cleanup — and a gate with no exit is a gate that gets disabled wholesale
    the first time it is wrong. Consuming the marker on use is what keeps it
    honest: it buys exactly one manual edit and has to be re-created for the
    next, so it cannot become an unnoticed session-wide opt-out.
    """
    marker = root / OVERRIDE_PATH
    try:
        if marker.exists():
            marker.unlink(missing_ok=True)
            return True
    except OSError:
        pass
    return False


def _edit_deny_reason(target: Path, root: Path) -> str:
    """
    The reason string is the whole user interface of a deny — it is the only
    thing the model sees, so it names the specific tool for each shape rather
    than saying "use bifrost", and it carries its own escape so being wrong
    once costs one command and not the whole gate.
    """
    return (
        f"Raw edit to {target.name} is blocked: it is a {target.suffix} file in {root}, "
        f"whose .mcp.json registers mcp-bifrost — the server that owns edits to this language.\n"
        "  new switch branch -> mcp__bifrost__insert_case\n"
        "  new function, method or class -> mcp__bifrost__insert_symbol\n"
        "  rewrite one named symbol -> mcp__bifrost__fix_symbol\n"
        "  one instruction across many symbols -> mcp__bifrost__fix_symbols\n"
        "  an edit with no symbol to address -> mcp__bifrost__fix_range\n"
        "  several files that must land together -> mcp__bifrost__patch_group\n"
        "These are deferred tools: call ToolSearch for them and do not assume they are unavailable because they are not listed.\n"
        "This applies to every subsequent edit of an adapted file for the rest of the session, not only this one — a single correction does not persist attention.\n"
        f"If no tool above can express the edit, run `touch {root / OVERRIDE_PATH}`, then retry and say why. It is consumed by one edit."
    )


def _deny_reason(payload: dict) -> str | None:
    """
    Two narrow gates, both built on facts rather than judgment.

    First: an existing .php/.py file inside a project whose .mcp.json
    actually registers this server is precisely the case every one of this
    server's editing tools was built for, so a raw Edit there is a miss by
    construction, not a judgment call. Second: RF-13's original new-file
    gate — `Write` to a file that does not yet exist, in a directory where
    `MIN_SIBLINGS` or more files already share its extension. Nothing here
    reads a file's contents or guesses intent — a suffix, an existence
    check, a directory listing and a config file are the whole basis, which
    is what makes denying rather than suggesting defensible. Returns the
    `permissionDecisionReason`, or None to fall through to the advisory
    reminder.
    """
    file_path = (payload.get("tool_input") or {}).get("file_path")
    if not file_path:
        return None

    target = Path(file_path)
    if not target.is_absolute():
        cwd = payload.get("cwd")
        if cwd:
            target = Path(cwd) / target

    if payload.get("tool_name") not in ("Edit", "Write"):
        return None

    # First trigger: adapted-language gate.
    if (target.suffix.lower() in ADAPTED_EXTENSIONS
            and target.exists()
            and (root := _bifrost_project_root(target)) is not None):
        if _consume_override(root):
            return None
        return _edit_deny_reason(target, root)

    # Second trigger: original new-file gate (Write only).
    if payload.get("tool_name") != "Write":
        return None
    if target.exists():
        return None  # an existing file is what fix_symbol/fix_range are for
    if target.suffix.lower() not in ADAPTED_EXTENSIONS:
        return None  # create_file has no adapter for it, so denying would point at a tool that cannot help

    try:
        siblings = [p for p in target.parent.iterdir()
                    if p.is_file() and p.suffix == target.suffix]
    except OSError:
        return None  # directory does not exist yet — nothing to pattern-match

    if len(siblings) < MIN_SIBLINGS:
        return None

    nearest = _nearest_sibling(target.stem, siblings)
    return (
        f"Blocked: {target.name} does not exist yet, and "
        f"{target.parent}/ already has {len(siblings)} other {target.suffix} "
        f"files. Use the deferred mcp__bifrost__create_file tool instead of "
        f"a raw Write — call ToolSearch for it if not listed — with "
        f"model_from={str(nearest)!r} so the new file is generated by "
        f"analogy to its closest-named neighbour instead of inventing this "
        f"directory's conventions from scratch."
    )


def hook_guard_output(payload: dict | None = None) -> dict:
    reason = _deny_reason(payload or {})
    if reason is not None:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": REMINDER,
        }
    }


def _read_payload() -> dict:
    """
    The PreToolUse event Claude Code feeds this hook on stdin: `tool_name`
    and `tool_input` among other fields.

    `isatty()` guards against a hang: Claude Code always pipes JSON in, never
    a terminal, so a live stdin means this was invoked some other way (a
    shell, a test) and there is no payload to read. Anything unparseable
    degrades to "no payload" rather than raising — advisory-only is the safe
    failure mode for a hook that fires on every Edit/Write in the session.
    """
    if sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def hook_guard() -> int:
    """Entry point for `mcp-bifrost hook-guard`, invoked by the hook itself."""
    print(json.dumps(hook_guard_output(_read_payload())))
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
