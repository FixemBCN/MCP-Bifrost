"""
The MCP server.

Deliberately thin. All sequencing lives in engine.py so it can be tested
without a protocol in the way; this file only maps tools onto it and writes
the tool descriptions the orchestrator will read.

Those descriptions matter more than they look. An orchestrator will reach
for a tool because it exists, so the descriptions say plainly when NOT to
use it — see the note on `fix_symbol`.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .engine import Engine
from .worker import DeepSeekWorker

# Where the operational log lives, relative to the project being patched.
DB_PATH = Path(os.environ.get("BIFROST_DB", ".bifrost/history.db"))

WHEN_NOT_TO_USE = (
    "Use this when you have several similar transformations to make, or when "
    "you can state the instruction WITHOUT having read the code. For a "
    "one-off fix you already have in front of you, edit it yourself: the "
    "context saving does not repay the round trip."
)

TOOLS = [
    {
        "name": "fix_symbols",
        "description": (
            "Apply ONE instruction across MANY symbols. This is the tool this "
            "server exists for. For a single edit the saving is modest — you "
            "had to read the code anyway to say what you wanted. The "
            "order-of-magnitude win is a transformation stated once and "
            "applied to symbols you never read: 'add a docblock to every "
            "method here', 'switch every call to the new API'. "
            "Worker calls run in parallel, writes run in series, and each "
            "symbol is re-resolved by name just before its write, so earlier "
            "patches moving later offsets is not a problem."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "targets": {
                    "type": "array",
                    "description": "Objects with 'file_path' and "
                                   "'symbol_name'. An optional per-target "
                                   "'instruction' overrides the shared one.",
                    "items": {"type": "object"},
                },
                "instruction": {"type": "string",
                                "description": "Applied to every target that "
                                               "does not carry its own."},
                "max_parallel": {"type": "integer",
                                 "description": "Concurrent worker calls, "
                                                "default 6."},
            },
            "required": ["targets"],
        },
    },
    {
        "name": "fix_symbol",
        "description": (
            "Rewrite one named function, method or class in place. The symbol "
            "is extracted with the language's own parser, sent to a worker "
            "model, validated, and spliced back atomically. Addressing by "
            "name is immune to line drift, so it is safe to issue many "
            "against the same file. " + WHEN_NOT_TO_USE
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string",
                              "description": "Path to the source file."},
                "symbol_name": {
                    "type": "string",
                    "description": "Function or method name. Qualify as "
                                   "'Class::method' when the bare name is "
                                   "ambiguous — ambiguity is rejected, never "
                                   "guessed.",
                },
                "instruction": {
                    "type": "string",
                    "description": "What to change, stated compactly. The "
                                   "worker sees only this symbol, so anything "
                                   "it needs to know that is not in the code "
                                   "must be said here or passed in context.",
                },
                "context": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Optional. Signatures or snippets the "
                                   "worker needs. Bare sibling signatures are "
                                   "usually useless; send what actually "
                                   "matters, such as the shape of a parameter.",
                },
                "allow_secrets": {
                    "type": "boolean",
                    "description": "Override Heimdall, the gate that refuses "
                                   "to send anything looking like a "
                                   "credential to the worker. Only after "
                                   "seeing what it flagged and judging it a "
                                   "false positive. The override is recorded.",
                },
            },
            "required": ["file_path", "symbol_name", "instruction"],
        },
    },
    {
        "name": "fix_range",
        "description": (
            "Rewrite an explicit line range. The escape hatch for code that "
            "does not live inside a symbol — module-level configuration, a "
            "branch of a switch, markup embedded in the source. Prefer "
            "fix_symbol where one applies: line numbers move when earlier "
            "patches land, and a stale range is rejected rather than applied."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "start_line": {"type": "integer",
                               "description": "1-based, inclusive."},
                "end_line": {"type": "integer",
                             "description": "1-based, inclusive."},
                "instruction": {"type": "string"},
                "context": {"type": "array", "items": {"type": "string"}},
                "allow_secrets": {"type": "boolean"},
            },
            "required": ["file_path", "start_line", "end_line", "instruction"],
        },
    },
    {
        "name": "insert_symbol",
        "description": (
            "Add a new function, method or class next to an existing one. "
            "Anchoring to a symbol rather than a line number is what makes "
            "this usable in a batch: the anchor does not move when everything "
            "above it changes. The anchor is also shown to the worker as a "
            "style reference, so the new code matches its neighbours."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "anchor": {"type": "string",
                           "description": "Existing symbol to position "
                                          "against. 'Class::method' when "
                                          "ambiguous."},
                "position": {"type": "string",
                             "enum": ["before", "after", "end_of_class",
                                      "end_of_file"]},
                "instruction": {"type": "string",
                                "description": "What the new symbol should "
                                               "be. Say 'output only the "
                                               "symbol' to avoid getting a "
                                               "whole class back."},
                "context": {"type": "array", "items": {"type": "string"}},
                "allow_secrets": {"type": "boolean"},
            },
            "required": ["file_path", "anchor", "position", "instruction"],
        },
    },
    {
        "name": "insert_case",
        "description": (
            "Add a branch to a `switch`. A case label is not a symbol, so no "
            "parser addresses it — but in a router-style file it is the "
            "extension point every new endpoint goes through. Anchored to a "
            "neighbouring case label, which stays stable across a batch the "
            "way line numbers do not. A label that already exists is "
            "rejected: PHP takes the first match and silently ignores the "
            "rest, so a duplicate is dead code that looks alive."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "after_case": {"type": "string",
                               "description": "Label of the case to insert "
                                              "after, without quotes."},
                "instruction": {"type": "string",
                                "description": "What the new branch does. Say "
                                               "'output only the case block'."},
                "context": {"type": "array", "items": {"type": "string"}},
                "allow_secrets": {"type": "boolean"},
            },
            "required": ["file_path", "after_case", "instruction"],
        },
    },
    {
        "name": "create_file",
        "description": (
            "Write a new source file. Never overwrites. "
            "Pass `model_from` to generate BY ANALOGY: the worker is handed "
            "an existing sibling as a structural exemplar and traces its "
            "shape, naming and error handling rather than inventing its own. "
            "Where a codebase already has many files of one kind, this is by "
            "far the most reliable way to add another — the worker does not "
            "need to know your conventions, it needs to copy them."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "instruction": {"type": "string"},
                "model_from": {"type": "string",
                               "description": "Path to an existing file to "
                                              "use as the structural "
                                              "exemplar. Strongly recommended."},
                "allow_secrets": {"type": "boolean"},
            },
            "required": ["file_path", "instruction"],
        },
    },
    {
        "name": "patch_group",
        "description": (
            "Apply several operations as one transaction: all of them land, "
            "or none do. Use whenever a change spans more than one file — a "
            "new class plus the require_once that references it plus the "
            "method that returns it. Applying that halfway leaves the project "
            "worse than not starting. On failure everything already applied "
            "is rolled back in reverse order."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "description": "Each entry is an object with 'op' set to "
                                   "fix_symbol | fix_range | insert_symbol | "
                                   "create_file, plus that tool's arguments. "
                                   "Order matters: they are applied in "
                                   "sequence.",
                    "items": {"type": "object"},
                },
            },
            "required": ["operations"],
        },
    },
    {
        "name": "export_docs",
        "description": (
            "Turn the patch log into readable documentation — a changelog, "
            "release notes, a summary of a batch. Every patch already "
            "recorded why it was made, at no cost to your context; this reads "
            "that back. Runs ONLY when asked: nothing generates it "
            "automatically."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "output_path": {"type": "string",
                                "description": "Write here. Omit to get the "
                                               "markdown back instead."},
                "since": {"type": "string",
                          "description": "ISO date, e.g. 2026-08-01."},
                "title": {"type": "string"},
                "group": {"type": "string", "enum": ["file", "session"],
                          "description": "By file reads as a changelog; by "
                                         "session reads as a work log."},
                "session_only": {"type": "boolean",
                                 "description": "Limit to this session's "
                                                "changes."},
            },
        },
    },
    {
        "name": "publish_session",
        "description": (
            "Put this session's changes on a branch as one reviewable commit, "
            "with a body listing every change and the reason recorded for it. "
            "Worth doing after any sizeable batch: a batch loose in the "
            "working tree is close to unreviewable, and where there is no "
            "test suite, review is the only safety net there is. "
            "Opening a pull request is opt-in and pushes to the remote — ask "
            "before setting it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "branch": {"type": "string"},
                "subject": {"type": "string",
                            "description": "The commit subject line. Yours to "
                                           "write — a machine summarising a "
                                           "batch produces 'Update 12 "
                                           "methods'."},
                "pull_request": {"type": "boolean",
                                 "description": "Push and open a PR. Off by "
                                                "default: this leaves the "
                                                "machine."},
                "base": {"type": "string"},
                "draft": {"type": "boolean", "description": "Default true."},
            },
            "required": ["branch", "subject"],
        },
    },
    {
        "name": "revert_patch",
        "description": "Undo one patch by id, restoring the file from the "
                       "blob saved in git's object store before it was applied.",
        "inputSchema": {
            "type": "object",
            "properties": {"patch_id": {"type": "string"}},
            "required": ["patch_id"],
        },
    },
    {
        "name": "revert_session",
        "description": (
            "Undo every patch applied in this session, newest first. Use when "
            "a batch has gone wrong partway: you rarely want to undo one "
            "patch out of twenty, you want to undo the batch."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class Server:
    """
    JSON-RPC 2.0 over stdio.

    Hand-rolled rather than SDK-based for now so the package keeps zero
    third-party dependencies; the protocol surface an MCP server needs is
    small. Swapping in the official MIT-licensed SDK is a contained change
    if keeping up with spec revisions by hand becomes the larger cost.
    """

    # Versions this server can speak, newest first. The list exists because
    # the correct answer to `initialize` is the client's version when we
    # support it, not ours unconditionally — a client that asked for one
    # thing and is told another has to decide whether to proceed, and the
    # stricter ones do not.
    PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
    PROTOCOL = PROTOCOLS[0]

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def handle(self, req: dict) -> dict | None:
        method = req.get("method")
        rid = req.get("id")

        if method == "initialize":
            asked = (req.get("params") or {}).get("protocolVersion")
            agreed = asked if asked in self.PROTOCOLS else self.PROTOCOL
            return self._ok(rid, {
                "protocolVersion": agreed,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mcp-bifrost", "version": "0.1.0"},
            })

        if method == "notifications/initialized":
            return None  # a notification: no id, no reply

        if method == "tools/list":
            return self._ok(rid, {"tools": TOOLS})

        if method == "tools/call":
            params = req.get("params") or {}
            return self._ok(rid, self._call(params.get("name"),
                                            params.get("arguments") or {}))

        if method == "ping":
            return self._ok(rid, {})

        return self._err(rid, -32601, f"unknown method: {method}")

    def _call(self, name: str, args: dict) -> dict:
        try:
            if name == "fix_symbols":
                out = self.engine.fix_symbols(
                    args["targets"], args.get("instruction"),
                    int(args.get("max_parallel", 6)))
            elif name == "fix_symbol":
                out = self.engine.fix_symbol(
                    args["file_path"], args["symbol_name"],
                    args["instruction"], args.get("context"),
                    bool(args.get("allow_secrets", False)))
            elif name == "fix_range":
                out = self.engine.fix_range(
                    args["file_path"], int(args["start_line"]),
                    int(args["end_line"]), args["instruction"],
                    args.get("context"),
                    bool(args.get("allow_secrets", False)))
            elif name == "insert_symbol":
                out = self.engine.insert_symbol(
                    args["file_path"], args["anchor"], args["position"],
                    args["instruction"], args.get("context"),
                    bool(args.get("allow_secrets", False)))
            elif name == "insert_case":
                out = self.engine.insert_case(
                    args["file_path"], args["after_case"], args["instruction"],
                    args.get("context"),
                    bool(args.get("allow_secrets", False)))
            elif name == "create_file":
                out = self.engine.create_file(
                    args["file_path"], args["instruction"],
                    args.get("model_from"),
                    bool(args.get("allow_secrets", False)))
            elif name == "patch_group":
                out = self.engine.patch_group(args["operations"])
            elif name == "export_docs":
                out = self.engine.export_docs(
                    args.get("output_path"), args.get("since"),
                    args.get("title", "Changes"), args.get("group", "file"),
                    bool(args.get("session_only", False)))
            elif name == "publish_session":
                out = self.engine.publish_session(
                    args["branch"], args["subject"],
                    bool(args.get("pull_request", False)), args.get("base"),
                    bool(args.get("draft", True)))
            elif name == "revert_patch":
                out = self.engine.revert_patch(args["patch_id"])
            elif name == "revert_session":
                out = self.engine.revert_session()
            else:
                return self._content(f"ERROR unknown tool: {name}", True)
        except KeyError as e:
            return self._content(f"ERROR missing argument: {e}", True)
        except Exception as e:  # noqa: BLE001 - never take the server down
            return self._content(f"ERROR {type(e).__name__}: {e}", True)

        text = out.render()
        if out.ok and out.patch_id:
            text += f" [{out.patch_id[:8]}]"
        return self._content(text, not out.ok)

    @staticmethod
    def _content(text: str, is_error: bool = False) -> dict:
        return {"content": [{"type": "text", "text": text}],
                "isError": is_error}

    @staticmethod
    def _ok(rid, result) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    @staticmethod
    def _err(rid, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": code, "message": message}}

    def serve(self, stdin=sys.stdin, stdout=sys.stdout) -> None:
        for raw in stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                req = json.loads(raw)
            except json.JSONDecodeError as e:
                self._write(stdout, self._err(None, -32700, f"parse error: {e}"))
                continue
            resp = self.handle(req)
            if resp is not None:
                self._write(stdout, resp)

    @staticmethod
    def _write(stdout, payload: dict) -> None:
        stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        stdout.flush()


def main() -> int:
    try:
        worker = DeepSeekWorker()
    except RuntimeError as e:
        print(f"mcp-bifrost: {e}", file=sys.stderr)
        return 2
    engine = Engine(worker, DB_PATH)
    try:
        Server(engine).serve()
    finally:
        engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
