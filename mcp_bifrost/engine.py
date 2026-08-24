"""
The engine: one patch, end to end.

This is where the pieces meet — adapter, worker, gates, patcher, log. The
MCP server above it is a thin shell; all the sequencing lives here so it can
be tested without a protocol in the way.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import gates, heimdall
from .budget import Budget, BudgetExceeded
from .languages import (LanguageAdapter, Symbol, apply_indent, normalise,
                        strip_indent)
from .languages.php import PhpAdapter
from .languages.python import PythonAdapter
from .log import PatchLog
from .patcher import (Applied, PatchError, apply_block, create, head_sha,
                      insert_at, repo_root, revert)
from .worker import WorkerClient
from . import docgen, vcs

ADAPTERS: dict[str, LanguageAdapter] = {}


def adapter_for(path: Path) -> LanguageAdapter:
    ext = path.suffix.lower()
    for a in ADAPTERS.values():
        if ext in a.extensions:
            return a
    raise ValueError(
        f"no adapter for {ext!r}. Supported: "
        + ", ".join(sorted(e for a in ADAPTERS.values() for e in a.extensions))
    )


ADAPTERS["php"] = PhpAdapter()
ADAPTERS["python"] = PythonAdapter()


@dataclass(frozen=True)
class Outcome:
    ok: bool
    message: str
    patch_id: str | None = None
    diff_stat: str | None = None
    gate: str | None = None

    def render(self) -> str:
        """
        What the orchestrator sees.

        Kept to one line on success. On failure it carries the reason and,
        where possible, what to do about it — an opaque ERROR would force the
        orchestrator to re-read the file, which is the cost this whole
        project exists to avoid.
        """
        if self.ok:
            # Patch outcomes carry a diff stat and want to stay one short
            # line. Reports and publish results ARE the message, so returning
            # a bare "OK" would throw away the only thing they produced.
            if self.diff_stat:
                return f"OK {self.diff_stat}"
            return f"OK {self.message}".strip() if self.message else "OK"
        return f"ERROR [{self.gate or 'engine'}] {self.message}"


class Engine:
    def __init__(self, worker: WorkerClient, db_path: Path,
                 session: str | None = None,
                 size_limit: int = 150,
                 substance_gate: bool = False,
                 budget: Budget | None = None,
                 entropy_scan: bool = True,
                 redact_secrets: bool = True) -> None:
        self.worker = worker
        self.log = PatchLog(db_path, session=session)
        self.size_limit = size_limit
        self.budget = budget or Budget()
        # The shapeless-secret check. Measured at 1 false positive in 1,291
        # real symbols; turn it off only for content where that rate is worse,
        # such as files carrying minified assets or long hashes.
        self.entropy_scan = entropy_scan
        # Redact rather than block where the secret is a self-contained token:
        # swap it for a placeholder, let the worker transform around it, put it
        # back before writing. Strictly better than refusing — provided every
        # placeholder comes back, which restore() enforces. Set False to go
        # back to blocking outright.
        self.redact_secrets = redact_secrets
        # Gate 3 is off by default: calibration never saw it fire, and a
        # coarse regex gate that rejects good patches is worse than one that
        # is not yet armed. Turn it on before bulk use. See gates.py.
        self.substance_gate = substance_gate

    # ------------------------------------------------------------------

    def fix_symbol(self, file_path: str, symbol: str,
                   instruction: str, context: list[str] | None = None,
                   allow_secrets: bool = False) -> Outcome:
        path = Path(file_path).resolve()
        if not path.is_file():
            return Outcome(False, f"no such file: {path}", gate="input")

        adapter = adapter_for(path)
        try:
            sym = adapter.find(path, symbol)
        except Exception as e:  # noqa: BLE001
            return Outcome(False, str(e), gate="resolve")

        size = gates.check_size(sym.n_lines, self.size_limit)
        if not size:
            return Outcome(False, size.detail, gate=size.gate)

        return self._run(path, adapter, sym, instruction, context or [],
                         allow_secrets=allow_secrets)

    def fix_range(self, file_path: str, start_line: int, end_line: int,
                  instruction: str, context: list[str] | None = None,
                  allow_secrets: bool = False) -> Outcome:
        path = Path(file_path).resolve()
        if not path.is_file():
            return Outcome(False, f"no such file: {path}", gate="input")
        if start_line < 1 or end_line < start_line:
            return Outcome(False, f"bad range {start_line}-{end_line}",
                           gate="input")

        adapter = adapter_for(path)
        source = path.read_bytes()
        lines = source.split(b"\n")
        if end_line > len(lines):
            return Outcome(False,
                           f"file has {len(lines)} lines, asked for {end_line}",
                           gate="input")

        # Byte offsets from line numbers. +1 per line for the newline we
        # split on; the last line of the range keeps no trailing newline.
        start = sum(len(l) + 1 for l in lines[:start_line - 1])
        end = start + sum(len(l) + 1 for l in lines[start_line - 1:end_line]) - 1

        first = lines[start_line - 1]
        indent = first[:len(first) - len(first.lstrip())].decode("utf-8", "replace")

        size = gates.check_size(end_line - start_line + 1, self.size_limit)
        if not size:
            return Outcome(False, size.detail, gate=size.gate)

        pseudo = Symbol(
            name=f"L{start_line}-{end_line}", fqn=f"{path.name}:{start_line}-{end_line}",
            cls=None, start_byte=start, end_byte=end,
            start_line=start_line, end_line=end_line,
            n_lines=end_line - start_line + 1, indent=indent,
        )
        return self._run(path, adapter, pseudo, instruction, context or [],
                         op="fix_range", allow_secrets=allow_secrets)

    # --------------------------------------------------------- generation

    def insert_symbol(self, file_path: str, anchor: str, position: str,
                      instruction: str, context: list[str] | None = None,
                      allow_secrets: bool = False) -> Outcome:
        """
        Add a new symbol next to an existing one.

        Anchoring to a symbol rather than a line number is what makes this
        usable in a batch: the anchor does not move when everything above it
        changes.
        """
        if position not in ("before", "after", "end_of_class", "end_of_file"):
            return Outcome(False, f"bad position {position!r}", gate="input")
        path = Path(file_path).resolve()
        if not path.is_file():
            return Outcome(False, f"no such file: {path}", gate="input")

        adapter = adapter_for(path)
        try:
            anc = adapter.find(path, anchor)
        except Exception as e:  # noqa: BLE001
            return Outcome(False, str(e), gate="resolve")

        source = path.read_bytes()
        anchor_src = anc.extract(source)
        before_syms = [s.fqn for s in adapter.symbols(path)]

        if position == "before":
            # Insert above the anchor's DOCBLOCK, not between the docblock and
            # the method it documents. Splicing in between detaches the two
            # and slips through every gate: the file still parses and the
            # symbol set is still correct, so only a human reading the diff
            # would notice the comment now describes the wrong function.
            head = anc.doc_start_byte if anc.doc_start_byte is not None \
                else anc.start_byte
            at = head - len(anc.indent)
        elif position == "after":
            at = anc.end_byte
        elif position == "end_of_class":
            same = [s for s in adapter.symbols(path) if s.cls == anc.cls]
            at = max(s.end_byte for s in same)
        else:
            at = len(source)

        payload = {
            "lang": adapter.name,
            "sym": f"new symbol {position} {anc.fqn}",
            "intent": instruction,
            "ctx": (context or []) + [
                "Neighbour for style reference, do not reproduce it:",
                strip_indent(anchor_src, anc.indent).decode("utf-8"),
            ],
            "indent": anc.indent,
            "src": "",
        }
        return self._generate(path, adapter, payload, at, anc, anchor_src,
                              before_syms, instruction, context or [],
                              allow_secrets)

    def insert_case(self, file_path: str, after_case: str,
                    instruction: str, context: list[str] | None = None,
                    allow_secrets: bool = False) -> Outcome:
        """
        Add a branch to a `switch`.

        This exists because a router file's extension point is not a symbol.
        Anchoring to a neighbouring case label keeps it stable across a batch,
        the same way symbol anchoring does for methods.
        """
        path = Path(file_path).resolve()
        if not path.is_file():
            return Outcome(False, f"no such file: {path}", gate="input")
        adapter = adapter_for(path)
        if not hasattr(adapter, "find_case"):
            return Outcome(False, f"{adapter.name} has no switch addressing",
                           gate="input")
        try:
            anc = adapter.find_case(path, after_case)
        except Exception as e:  # noqa: BLE001
            return Outcome(False, str(e), gate="resolve")

        if anc.fallthrough:
            return Outcome(
                False,
                f"case {anc.label!r} falls through to the next branch — it has "
                f"no body of its own. Inserting after it would break the "
                f"chain, and the result would still parse. Anchor on the last "
                f"label of the group instead.",
                gate="resolve")

        source = path.read_bytes()
        anchor_src = anc.extract(source)
        before_cases = [c.label for c in adapter.cases(path)]

        payload = {
            "lang": adapter.name,
            "sym": f"new switch case after '{anc.label}'",
            "intent": instruction,
            "ctx": (context or []) + [
                "The neighbouring case, for shape. Do not reproduce it:",
                strip_indent(anchor_src, anc.indent).decode("utf-8"),
            ],
            "indent": anc.indent,
            "src": "",
        }
        return self._insert_case(path, adapter, payload, anc, anchor_src,
                                 before_cases, instruction, allow_secrets)

    def _insert_case(self, path, adapter, payload, anc, anchor_src,
                     before_cases, instruction, allow_secrets) -> Outcome:
        findings = heimdall.scan_payload(
            "\n".join(payload["ctx"]), None, entropy=self.entropy_scan)
        if findings and not allow_secrets:
            return Outcome(False, heimdall.describe(findings), gate="heimdall")
        try:
            self.budget.check()
        except BudgetExceeded as e:
            return Outcome(False, str(e), gate="budget")

        result = self.worker.run(payload)
        self.budget.spend(result.tokens_in, result.tokens_out)

        common = dict(op="insert_case", fitxer=str(path), simbol=anc.label,
                      start_byte=anc.start_byte, end_byte=anc.end_byte,
                      instruccio=instruction, src_b=0,
                      in_b=len(instruction.encode()), resp_b=10,
                      tin=result.tokens_in, tout=result.tokens_out,
                      cache_hit=result.cache_hit, ms=result.ms,
                      head_sha=head_sha(repo_root(path) or path.parent))
        if not result.ok or result.out is None:
            self.log.record(estat="error", porta="worker",
                            **{**common, "rationale": result.error})
            return Outcome(False, result.error or "worker failed", gate="worker")

        block = apply_indent(result.out.encode("utf-8"), anc.indent)
        block = b"\n\n" + anc.indent.encode() + block.lstrip()
        common["out_b"] = len(block)
        common["rationale"] = result.why

        try:
            applied = insert_at(path, anc.end_byte, block, anchor_src,
                                anc.start_byte, anc.end_byte, adapter)
        except PatchError as e:
            # By this point `common` already carries the worker's own
            # rationale, so passing another one as a keyword was a TypeError
            # raised from inside the error handler: every patcher refusal —
            # "not inside a git repository" first among them — reached the
            # caller as an internal crash instead of its own message.
            self.log.record(estat="error", porta="patcher",
                            **{**common, "rationale": str(e)})
            return Outcome(False, str(e), gate="patcher")
        if isinstance(applied, gates.GateResult):
            self.log.record(estat="rebutjat", porta=applied.gate, **common)
            return Outcome(False, applied.detail, gate=applied.gate)

        after_cases = [c.label for c in adapter.cases(path)]
        cset = gates.check_case_set(before_cases, after_cases, expected_new=1)
        if not cset:
            revert(path, applied.blob_before)
            self.log.record(estat="rebutjat", porta=cset.gate, **common)
            return Outcome(False, cset.detail, gate=cset.gate)

        pid = self.log.record(estat="ok", blob_abans=applied.blob_before,
                              **common)
        return Outcome(True, "case inserted", patch_id=pid,
                       diff_stat=applied.diff_stat)

    def create_file(self, file_path: str, instruction: str,
                    model_from: str | None = None,
                    allow_secrets: bool = False) -> Outcome:
        """
        Write a new file, optionally by analogy with an existing one.

        `model_from` is the strong form. Handing the worker a sibling as a
        structural exemplar means it does not invent architecture, it traces
        it — which turns its ignorance of the project from a weakness into an
        irrelevance. It does not need to know the conventions; it needs to
        copy them.
        """
        path = Path(file_path).resolve()
        absent = gates.check_absent(path.exists(), str(path))
        if not absent:
            return Outcome(False, absent.detail, gate=absent.gate)

        adapter = adapter_for(path)
        ctx: list[str] = []
        if model_from:
            model = Path(model_from).resolve()
            if not model.is_file():
                return Outcome(False, f"no such exemplar: {model}", gate="input")
            ctx = [
                f"Structural exemplar ({model.name}). Follow its shape, "
                f"naming, error handling and conventions exactly. Do not "
                f"copy its subject matter:",
                model.read_bytes().decode("utf-8"),
            ]

        payload = {"lang": adapter.name, "sym": path.name,
                   "intent": instruction, "ctx": ctx, "indent": "", "src": ""}

        findings = heimdall.scan_payload(
            "\n".join(ctx), None, entropy=self.entropy_scan)
        if findings and not allow_secrets:
            return Outcome(False, heimdall.describe(findings), gate="heimdall")
        try:
            self.budget.check()
        except BudgetExceeded as e:
            return Outcome(False, str(e), gate="budget")

        result = self.worker.run(payload)
        self.budget.spend(result.tokens_in, result.tokens_out)
        common = dict(op="create_file", fitxer=str(path), instruccio=instruction,
                      src_b=0, in_b=len(instruction.encode()), resp_b=10,
                      tin=result.tokens_in, tout=result.tokens_out,
                      cache_hit=result.cache_hit, ms=result.ms)
        if not result.ok or result.out is None:
            self.log.record(estat="error", porta="worker",
                            **{**common, "rationale": result.error})
            return Outcome(False, result.error or "worker failed", gate="worker")

        content = result.out.encode("utf-8")
        if not content.endswith(b"\n"):
            content += b"\n"
        common["out_b"] = len(content)
        common["rationale"] = result.why

        empty = gates.check_nonempty(adapter, content)
        if not empty:
            self.log.record(estat="rebutjat", porta=empty.gate, **common)
            return Outcome(False, empty.detail, gate=empty.gate)

        outcome = create(path, content, adapter)
        if isinstance(outcome, gates.GateResult):
            self.log.record(estat="rebutjat", porta=outcome.gate, **common)
            return Outcome(False, outcome.detail, gate=outcome.gate)

        pid = self.log.record(estat="ok", blob_abans="", **common)
        return Outcome(True, "created", patch_id=pid,
                       diff_stat=outcome.diff_stat)

    def _generate(self, path, adapter, payload, at, anc, anchor_src,
                  before_syms, instruction, context, allow_secrets) -> Outcome:
        findings = heimdall.scan_payload(
            "\n".join(payload["ctx"]), None, entropy=self.entropy_scan)
        if findings and not allow_secrets:
            return Outcome(False, heimdall.describe(findings), gate="heimdall")
        try:
            self.budget.check()
        except BudgetExceeded as e:
            return Outcome(False, str(e), gate="budget")

        result = self.worker.run(payload)
        self.budget.spend(result.tokens_in, result.tokens_out)

        common = dict(op="insert_symbol", fitxer=str(path), simbol=anc.fqn,
                      instruccio=instruction, src_b=0,
                      in_b=len(instruction.encode()), resp_b=10,
                      tin=result.tokens_in, tout=result.tokens_out,
                      cache_hit=result.cache_hit, ms=result.ms,
                      head_sha=head_sha(repo_root(path) or path.parent))
        if not result.ok or result.out is None:
            self.log.record(estat="error", porta="worker",
                            **{**common, "rationale": result.error})
            return Outcome(False, result.error or "worker failed", gate="worker")

        block = apply_indent(result.out.encode("utf-8"), anc.indent)
        # Sit the new symbol on its own lines, indented like its neighbour.
        block = b"\n" + anc.indent.encode() + block.lstrip() + b"\n"
        common["out_b"] = len(block)
        common["rationale"] = result.why

        try:
            applied = insert_at(path, at, block, anchor_src,
                                anc.start_byte, anc.end_byte, adapter)
        except PatchError as e:
            self.log.record(estat="error", porta="patcher",
                            **{**common, "rationale": str(e)})
            return Outcome(False, str(e), gate="patcher")
        if isinstance(applied, gates.GateResult):
            self.log.record(estat="rebutjat", porta=applied.gate, **common)
            return Outcome(False, applied.detail, gate=applied.gate)

        # Only now can the symbol set be compared: the gate is about the file
        # after the write, and rolling back is cheap because the blob is saved.
        after_syms = [s.fqn for s in adapter.symbols(path)]
        sset = gates.check_symbol_set(before_syms, after_syms, expected_new=1)
        if not sset:
            revert(path, applied.blob_before)
            self.log.record(estat="rebutjat", porta=sset.gate, **common)
            return Outcome(False, sset.detail, gate=sset.gate)

        pid = self.log.record(estat="ok", blob_abans=applied.blob_before,
                              **common)
        return Outcome(True, "inserted", patch_id=pid,
                       diff_stat=applied.diff_stat)

    # ------------------------------------------------------------------

    def _run(self, path: Path, adapter, sym: Symbol, instruction: str,
             context: list[str], op: str = "fix_symbol",
             allow_secrets: bool = False) -> Outcome:
        source = path.read_bytes()
        original = sym.extract(source)

        # Normalise indentation away before the worker ever sees it, so it
        # cannot get it wrong. Calibration: 1 of 9 blocks came back at the
        # wrong indentation while being otherwise correct. Skipped when the
        # round trip would not be lossless — see normalise().
        normalised, effective_indent = normalise(original, sym.indent)

        payload = {
            "lang": adapter.name,
            "sym": sym.fqn,
            "intent": instruction,
            "ctx": context,
            "indent": effective_indent,
            "src": normalised.decode("utf-8"),
        }

        # Heimdall stands before the worker, not after it: a finding must
        # stop the send, and anything checked afterwards has already crossed.
        vault = None
        if self.redact_secrets and not allow_secrets:
            red_src, red_ctx, vault, findings = heimdall.redact_payload(
                payload["src"], context, entropy=self.entropy_scan)
            if not findings:
                # Everything found was redactable: the worker gets placeholders
                # and the secrets never leave this process.
                payload["src"], payload["ctx"] = red_src, red_ctx
            else:
                vault = None  # something unredactable remains; block instead
        else:
            findings = heimdall.scan_payload(payload["src"], context,
                                             entropy=self.entropy_scan)
        if findings and not allow_secrets:
            self.log.record(
                op=op, fitxer=str(path), simbol=sym.fqn,
                start_byte=sym.start_byte, end_byte=sym.end_byte,
                instruccio=instruction, src_b=len(original),
                estat="rebutjat", porta="heimdall",
                # Pattern names and line numbers only. Recording the matched
                # text would move the secret into the log instead of stopping
                # it there.
                rationale="; ".join(str(f) for f in findings[:10]),
            )
            return Outcome(False, heimdall.describe(findings), gate="heimdall")
        if findings:
            # Overridden. Worth a trail: someone decided this was safe.
            override = f"heimdall overridden ({len(findings)} findings)"
        elif vault:
            override = f"heimdall redacted {len(vault)} secret(s)"
        else:
            override = None

        try:
            self.budget.check()
        except BudgetExceeded as e:
            return Outcome(False, str(e), gate="budget")

        t0 = time.time()
        result = self.worker.run(payload)
        self.budget.spend(result.tokens_in, result.tokens_out)

        # in_b / resp_b measure what the ORCHESTRATOR spent, not what we
        # spent talking to the worker. Wiring the worker's HTTP payload in
        # here made the efficacy log report a negative saving, because that
        # payload carries the whole source block — the very thing the
        # orchestrator did not have to carry. tin/tout already record the
        # worker's own cost.
        orchestrator_in = len(
            (str(path) + sym.fqn + instruction + "".join(context)).encode("utf-8")
        )

        common = dict(
            op=op, fitxer=str(path), simbol=sym.fqn,
            start_byte=sym.start_byte, end_byte=sym.end_byte,
            instruccio=instruction, src_b=len(original),
            in_b=orchestrator_in, resp_b=len(b"OK +00/-00"),
            tin=result.tokens_in, tout=result.tokens_out,
            cache_hit=result.cache_hit, ms=result.ms,
            head_sha=head_sha(repo_root(path) or path.parent),
            override=override,
        )

        if not result.ok or result.out is None:
            self.log.record(estat="error", porta="worker",
                            **{**common, "rationale": result.error})
            return Outcome(False, result.error or "worker failed", gate="worker")

        produced = result.out.encode("utf-8")
        if vault:
            try:
                produced = heimdall.restore(produced, vault)
            except heimdall.RestoreError as e:
                self.log.record(estat="rebutjat", porta="heimdall-restore",
                                **{**common, "rationale": str(e)})
                return Outcome(False, str(e), gate="heimdall-restore")

        new_block = apply_indent(produced, effective_indent)
        common["out_b"] = len(new_block)
        common["rationale"] = result.why

        # Gate 2 before touching disk. Cheap, and it needs no file rebuild.
        single = gates.check_single_symbol(adapter, new_block) \
            if op == "fix_symbol" else gates.PASS
        if not single:
            self.log.record(estat="rebutjat", porta=single.gate, **common)
            return Outcome(False, single.detail, gate=single.gate)

        if self.substance_gate:
            sub = gates.check_substance(original, new_block)
            if not sub:
                self.log.record(estat="rebutjat", porta=sub.gate, **common)
                return Outcome(False, sub.detail, gate=sub.gate)

        try:
            applied = apply_block(path, sym.start_byte, sym.end_byte,
                                  original, new_block, adapter)
        except PatchError as e:
            self.log.record(estat="error", porta="patcher",
                            **{**common, "rationale": str(e)})
            return Outcome(False, str(e), gate="patcher")

        if isinstance(applied, gates.GateResult):
            self.log.record(estat="rebutjat", porta=applied.gate, **common)
            return Outcome(False, applied.detail, gate=applied.gate)

        patch_id = self.log.record(estat="ok", blob_abans=applied.blob_before,
                                   **common)
        return Outcome(True, "applied", patch_id=patch_id,
                       diff_stat=applied.diff_stat)

    # ------------------------------------------------------------------

    def revert_patch(self, patch_id: str) -> Outcome:
        row = self.log.get(patch_id)
        if row is None:
            return Outcome(False, f"unknown patch {patch_id}", gate="revert")
        return self._undo(row)

    @staticmethod
    def _undo(row: dict) -> Outcome:
        """
        Undoing a creation is deletion, not restoration — there was no
        previous content to put back. The empty blob is what distinguishes
        the two, so it has to be checked before treating it as "nothing was
        applied".
        """
        path = Path(row["fitxer"])
        if row.get("op") == "create_file":
            path.unlink(missing_ok=True)
            return Outcome(True, f"deleted {path}")
        if not row.get("blob_abans"):
            return Outcome(False, f"patch {row['id']} has no saved blob "
                                  f"(it was never applied)", gate="revert")
        try:
            revert(path, row["blob_abans"])
        except PatchError as e:
            return Outcome(False, str(e), gate="revert")
        return Outcome(True, f"reverted {path}")

    def revert_session(self, session: str | None = None) -> Outcome:
        """
        Undo a whole batch, newest first.

        Reverse order is the point: when a batch of 20 fails at the 17th you
        do not want to undo one, you want to undo all of them — and several
        may have touched the same file, where restoring the oldest blob last
        is the only order that ends up correct.
        """
        rows = self.log.session_patches(session, estat="ok")
        done, failed = 0, []
        for row in rows:
            out = self._undo(row)
            if out.ok:
                done += 1
            else:
                failed.append(out.message)
        if failed:
            return Outcome(False, f"reverted {done}, failed {len(failed)}: "
                                  + "; ".join(failed[:3]), gate="revert")
        return Outcome(True, f"reverted {done} patches")

    def close(self) -> None:
        self.log.close()

    # -------------------------------------------------------- transactions

    def patch_group(self, operations: list[dict]) -> Outcome:
        """
        Apply N operations, or none of them.

        Adding one feature to a real codebase touches three or four files at
        once, and doing it halfway leaves the project worse than before
        starting: a new class with no `require_once` referencing it is worse
        than no class.

        Applied sequentially rather than staged, because each operation's
        offsets depend on the ones before it in the same file. On failure the
        applied ones are undone in reverse — which is exactly the rollback
        machinery that already exists, so a transaction costs no new
        mechanism.
        """
        dispatch = {
            "fix_symbol": lambda o: self.fix_symbol(
                o["file_path"], o["symbol_name"], o["instruction"],
                o.get("context"), o.get("allow_secrets", False)),
            "fix_range": lambda o: self.fix_range(
                o["file_path"], int(o["start_line"]), int(o["end_line"]),
                o["instruction"], o.get("context"),
                o.get("allow_secrets", False)),
            "insert_case": lambda o: self.insert_case(
                o["file_path"], o["after_case"], o["instruction"],
                o.get("context"), o.get("allow_secrets", False)),
            "insert_symbol": lambda o: self.insert_symbol(
                o["file_path"], o["anchor"], o.get("position", "after"),
                o["instruction"], o.get("context"),
                o.get("allow_secrets", False)),
            "create_file": lambda o: self.create_file(
                o["file_path"], o["instruction"], o.get("model_from"),
                o.get("allow_secrets", False)),
        }

        applied: list[str] = []
        for i, op in enumerate(operations, 1):
            kind = op.get("op")
            if kind not in dispatch:
                return self._abort(applied, f"operation {i}: unknown op "
                                            f"{kind!r}")
            try:
                out = dispatch[kind](op)
            except KeyError as e:
                return self._abort(applied, f"operation {i}: missing {e}")
            if not out.ok:
                return self._abort(applied,
                                   f"operation {i} ({kind}) failed: "
                                   f"{out.message}")
            if out.patch_id:
                applied.append(out.patch_id)

        return Outcome(True, f"applied {len(applied)} operations",
                       diff_stat=f"{len(applied)} ops")

    def _abort(self, applied: list[str], why: str) -> Outcome:
        undone, stuck = 0, []
        for pid in reversed(applied):
            row = self.log.get(pid)
            if row is None:
                continue
            out = self._undo(row)
            if out.ok:
                undone += 1
            else:
                stuck.append(out.message)
        tail = (f" Rolled back {undone}/{len(applied)}."
                if not stuck else
                f" ROLLBACK INCOMPLETE — {len(stuck)} file(s) left changed: "
                + "; ".join(stuck[:3]))
        return Outcome(False, why + tail, gate="group")

    # ---------------------------------------------------------------- batch

    def fix_symbols(self, targets: list[dict], instruction: str | None = None,
                    max_parallel: int = 6) -> Outcome:
        """
        One instruction across many symbols. The tool this project exists for.

        For a single edit the context saving is modest, because the
        orchestrator had to read the code anyway to say what it wanted. The
        order-of-magnitude win is here: a transformation stated once and
        applied to two hundred symbols the orchestrator never reads.

        **Calls run in parallel, writes run in series.** Serialising per file
        — the obvious design — never engages where it matters, because in a
        real codebase most of a batch lands in the same large file and would
        run entirely sequentially. Worker calls are independent; only disk
        application needs ordering.

        What makes the parallelism safe is symbol addressing. Between the
        call and the write, earlier patches move the offsets of later ones —
        but not their *content*. So each symbol is re-resolved by name just
        before its write, and gate 0 then compares content, not position. A
        mismatch there means something genuinely changed underneath, which is
        exactly when it should refuse.
        """
        if not targets:
            return Outcome(False, "no targets", gate="input")
        if self.budget.calls + len(targets) > self.budget.max_calls:
            return Outcome(
                False, f"batch of {len(targets)} would exceed the call limit "
                       f"({self.budget.summary()})", gate="budget")

        prepared, failures = self._prepare_batch(targets, instruction,
                                                 max_parallel)
        applied, rejected = [], list(failures)

        for item in prepared:
            out = self._land(item)
            if out.ok:
                applied.append(out.patch_id)
            else:
                rejected.append(f"{item['label']}: {out.message}")

        ok = bool(applied) and not rejected
        msg = f"{len(applied)}/{len(targets)} applied"
        if rejected:
            msg += ". Rejected — " + "; ".join(r[:120] for r in rejected[:5])
            if len(rejected) > 5:
                msg += f" (+{len(rejected) - 5} more)"
        return Outcome(ok, msg, diff_stat=f"{len(applied)} ok",
                       gate=None if ok else "batch")

    def _prepare_batch(self, targets, instruction, max_parallel):
        """Resolve, redact and call the worker for every target, in parallel."""
        def one(t: dict):
            label = f"{Path(t['file_path']).name}::{t['symbol_name']}"
            try:
                path = Path(t["file_path"]).resolve()
                adapter = adapter_for(path)
                sym = adapter.find(path, t["symbol_name"])
            except Exception as e:  # noqa: BLE001
                return None, f"{label}: {e}"

            size = gates.check_size(sym.n_lines, self.size_limit)
            if not size:
                return None, f"{label}: {size.detail}"

            intent = t.get("instruction") or instruction
            if not intent:
                return None, f"{label}: no instruction"

            original = sym.extract(path.read_bytes())
            normalised, eff_indent = normalise(original, sym.indent)
            payload = {"lang": adapter.name, "sym": sym.fqn, "intent": intent,
                       "ctx": t.get("context") or [], "indent": eff_indent,
                       "src": normalised.decode("utf-8")}

            vault = None
            if self.redact_secrets and not t.get("allow_secrets"):
                red_src, red_ctx, vault, blocked = heimdall.redact_payload(
                    payload["src"], payload["ctx"], entropy=self.entropy_scan)
                if blocked:
                    return None, f"{label}: {heimdall.describe(blocked)}"
                payload["src"], payload["ctx"] = red_src, red_ctx

            result = self.worker.run(payload)
            if not result.ok or result.out is None:
                return None, f"{label}: {result.error or 'worker failed'}"
            return {"label": label, "path": path, "adapter": adapter,
                    "sym": sym, "original": original, "intent": intent,
                    "result": result, "vault": vault,
                    "indent": eff_indent}, None

        prepared, failures = [], []
        with ThreadPoolExecutor(max_workers=max_parallel) as pool:
            for item, err in pool.map(one, targets):
                (failures if err else prepared).append(err or item)
        for item in prepared:
            self.budget.spend(item["result"].tokens_in,
                              item["result"].tokens_out)
        return prepared, failures

    def _land(self, item) -> Outcome:
        """Write one prepared result, re-resolving its position first."""
        path, adapter, sym = item["path"], item["adapter"], item["sym"]
        result, original = item["result"], item["original"]

        # Re-resolve: earlier writes in this batch have moved the offsets,
        # though not the content.
        try:
            sym = adapter.find(path, sym.fqn)
        except Exception as e:  # noqa: BLE001
            return Outcome(False, str(e), gate="resolve")

        produced = result.out.encode("utf-8")
        if item["vault"]:
            try:
                produced = heimdall.restore(produced, item["vault"])
            except heimdall.RestoreError as e:
                return Outcome(False, str(e), gate="heimdall-restore")

        new_block = apply_indent(produced, item["indent"])
        common = dict(op="fix_symbol", fitxer=str(path), simbol=sym.fqn,
                      start_byte=sym.start_byte, end_byte=sym.end_byte,
                      instruccio=item["intent"], src_b=len(original),
                      out_b=len(new_block), in_b=len(item["intent"].encode()),
                      resp_b=10, tin=result.tokens_in, tout=result.tokens_out,
                      cache_hit=result.cache_hit, ms=result.ms,
                      rationale=result.why,
                      override=(f"heimdall redacted {len(item['vault'])} "
                                f"secret(s)") if item["vault"] else None,
                      head_sha=head_sha(repo_root(path) or path.parent))

        single = gates.check_single_symbol(adapter, new_block)
        if not single:
            self.log.record(estat="rebutjat", porta=single.gate, **common)
            return Outcome(False, single.detail, gate=single.gate)

        if self.substance_gate:
            sub = gates.check_substance(original, new_block)
            if not sub:
                self.log.record(estat="rebutjat", porta=sub.gate, **common)
                return Outcome(False, sub.detail, gate=sub.gate)

        try:
            applied = apply_block(path, sym.start_byte, sym.end_byte,
                                  original, new_block, adapter)
        except PatchError as e:
            self.log.record(estat="error", porta="patcher",
                            **{**common, "rationale": str(e)})
            return Outcome(False, str(e), gate="patcher")
        if isinstance(applied, gates.GateResult):
            self.log.record(estat="rebutjat", porta=applied.gate, **common)
            return Outcome(False, applied.detail, gate=applied.gate)

        pid = self.log.record(estat="ok", blob_abans=applied.blob_before,
                              **common)
        return Outcome(True, "applied", patch_id=pid,
                       diff_stat=applied.diff_stat)

    # ------------------------------------------------------- documentation

    def export_docs(self, output_path: str | None = None,
                    since: str | None = None, title: str = "Changes",
                    group: str = "file", session_only: bool = False) -> Outcome:
        """
        Turn the log into readable documentation. Only when asked.

        Nothing generates this on a schedule or as a side effect of patching.
        The rationale for every change has been accumulating at zero context
        cost since the first patch; this is what reads it back.
        """
        try:
            entries = docgen.read(
                self.log.db_path, since=since,
                session=self.log.session if session_only else None)
        except FileNotFoundError as e:
            return Outcome(False, str(e), gate="docs")

        if not entries:
            return Outcome(False, "no recorded changes to document",
                           gate="docs")

        root = Path.cwd()
        text = docgen.as_changelog(entries, title=title, root=root,
                                  group=group)
        if output_path:
            dest = Path(output_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
            return Outcome(True, f"{docgen.summarise(entries)} → {dest}")
        return Outcome(True, text)

    # ---------------------------------------------------------------- vcs

    def session_files(self) -> list[str]:
        """Files this session actually changed, in first-touched order."""
        seen, out = set(), []
        for row in reversed(self.log.session_patches(estat="ok")):
            f = row["fitxer"]
            if f not in seen:
                seen.add(f)
                out.append(f)
        return out

    def publish_session(self, branch: str, subject: str,
                        pull_request: bool = False,
                        base: str | None = None,
                        draft: bool = True) -> Outcome:
        """
        Put this session's work on a branch, as one reviewable commit.

        With no test suite underneath, review is the only thing standing
        between a bad generation and production. A batch left loose in the
        working tree is not realistically reviewable; the same batch as a
        branch with a commit body listing each change and its stated reason
        is.

        `pull_request` is opt-in and off by default, because opening one
        publishes to a remote — that is a decision for the person asking, not
        a default.
        """
        files = self.session_files()
        if not files:
            return Outcome(False, "this session changed nothing", gate="vcs")

        repo = Path(files[0]).parent
        try:
            st = vcs.state(repo)
            entries = docgen.read(Path(self.log.db_path), 
                                  session=self.log.session)
            message = docgen.as_commit_message(entries, subject,
                                               root=st.root)
            vcs.create_branch(st.root, branch)
            sha = vcs.commit_files(st.root,
                                   [str(Path(f).relative_to(st.root))
                                    for f in files], message)
        except (vcs.VcsError, ValueError) as e:
            return Outcome(False, str(e), gate="vcs")

        result = f"committed {len(files)} file(s) to {branch} ({sha[:8]})"
        if not pull_request:
            return Outcome(True, result + ". Not pushed.")

        try:
            vcs.push(st.root, branch)
            url = vcs.open_pr(st.root, subject,
                              body=docgen.as_changelog(entries, title=subject,
                                                       root=st.root),
                              base=base, draft=draft)
        except vcs.VcsError as e:
            return Outcome(True, result + f". Pushed/PR failed: {e}")
        return Outcome(True, result + f". PR: {url}")
