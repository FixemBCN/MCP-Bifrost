"""
PHP adapter.

Extraction goes through `extract.php`, which uses `token_get_all()` — PHP's
official tokenizer, the same lexer the interpreter runs. Validation goes
through `php -l`. Both need the `php` binary and nothing else, which is why
this adapter has no third-party dependency at all.

Validated against 128 first-party files of the target codebase: zero
failures.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from . import Case, ExtractionError, Symbol

EXTRACTOR = Path(__file__).parent / "extract.php"


class PhpAdapter:
    name = "php"
    extensions = (".php",)

    def __init__(self, php_binary: str = "php") -> None:
        self.php = php_binary

    # ------------------------------------------------------------ extraction

    def symbols(self, path: Path) -> list[Symbol]:
        proc = subprocess.run(
            [self.php, str(EXTRACTOR), str(path)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            # Exit code 4 is the extractor's own sanity check: concatenating
            # the tokens did not reproduce the file. Offsets derived from
            # that are not to be trusted, and it says so rather than
            # returning them.
            raise ExtractionError(
                f"{path}: extract.php exited {proc.returncode}: "
                f"{proc.stderr.strip()}"
            )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise ExtractionError(f"{path}: unparsable symbol map: {e}") from e

        return [
            Symbol(
                name=s["name"],
                fqn=s["fqn"],
                cls=s["class"],
                start_byte=s["start_byte"],
                end_byte=s["end_byte"],
                start_line=s["start_line"],
                end_line=s["end_line"],
                n_lines=s["n_lines"],
                # A symbol not sitting alone on its line (`}else{`, or PHP
                # reopened mid-line) has no meaningful indentation. Treat it
                # as none rather than inventing one.
                indent=s["indent"] or "",
                abstract=s["abstract"],
                doc_start_byte=s["doc_start_byte"],
            )
            for s in data["symbols"]
        ]

    def find(self, path: Path, name: str) -> Symbol:
        """
        Resolve one symbol by name or by `Class::method`.

        Ambiguity is an error, never a guess. Two methods sharing a name
        across two classes in one file is normal PHP; picking one silently
        would patch the wrong body and every gate downstream would pass,
        because the result is perfectly valid code in the wrong place.
        """
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

    def cases(self, path: Path) -> list[Case]:
        """Every `switch` branch in the file, in source order."""
        proc = subprocess.run([self.php, str(EXTRACTOR), str(path)],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise ExtractionError(f"{path}: extract.php exited "
                                  f"{proc.returncode}: {proc.stderr.strip()}")
        return [
            Case(label=c["label"], start_byte=c["start_byte"],
                 end_byte=c["end_byte"], start_line=c["start_line"],
                 end_line=c["end_line"], indent=c["indent"] or "",
                 fallthrough=bool(c.get("fallthrough")))
            for c in json.loads(proc.stdout)["cases"]
            if c["end_byte"] is not None
        ]

    def find_case(self, path: Path, label: str) -> Case:
        """
        Resolve a case by its label.

        Duplicate labels are a bug in the file itself — PHP takes the first
        and silently ignores the rest — so an ambiguous lookup is reported
        rather than resolved. Patching the unreachable one would be a change
        with no effect, which is worse than an error.
        """
        cs = self.cases(path)
        matches = [c for c in cs if c.label == label]
        if not matches:
            raise ExtractionError(
                f"{path}: no case labelled {label!r}. "
                f"Known: {', '.join(c.label for c in cs[:20])}"
            )
        if len(matches) > 1:
            raise ExtractionError(
                f"{path}: {label!r} appears {len(matches)} times (lines "
                + ", ".join(str(m.start_line) for m in matches)
                + "). Only the first is reachable; fix the file first."
            )
        return matches[0]

    # ------------------------------------------------------------ validation

    def validate(self, source: bytes) -> tuple[bool, str]:
        """
        `php -l` against in-memory content.

        Syntax only. A call to a method that does not exist passes cleanly
        here — see docs/architecture.md §5, gate 1.
        """
        fd, tmp = tempfile.mkstemp(suffix=".php")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(source)
            proc = subprocess.run(
                [self.php, "-l", tmp], capture_output=True, text=True
            )
            if proc.returncode == 0:
                return True, ""
            # php -l puts the useful diagnostic ("PHP Parse error: unexpected
            # ... on line N") on stderr and a useless summary ("Errors
            # parsing <file>") on stdout. Taking the first line of the two
            # concatenated hands the orchestrator the summary and throws the
            # diagnostic away — which makes the rejection unactionable and
            # forces exactly the file re-read this project exists to avoid.
            lines = [l.strip() for l in (proc.stderr + proc.stdout).splitlines()
                     if l.strip()]
            useful = next(
                (l for l in lines
                 if "Parse error" in l or "Fatal error" in l), None
            )
            msg = (useful or (lines[0] if lines else "php -l failed"))
            return False, msg.replace(tmp, "<file>")
        finally:
            os.unlink(tmp)

    def count_symbols(self, block: bytes) -> int:
        """
        How many symbols a standalone block defines.

        Gate 2 uses this to catch a worker that returned a sibling function
        alongside the one it was asked for. The block is wrapped in `<?php`
        because on its own it is not a parsable file.
        """
        fd, tmp = tempfile.mkstemp(suffix=".php")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(b"<?php\nclass __BifrostProbe {\n" + block + b"\n}\n")
            proc = subprocess.run(
                [self.php, str(EXTRACTOR), tmp], capture_output=True, text=True
            )
            if proc.returncode != 0:
                # Not inside a class, perhaps. Try the block bare.
                with open(tmp, "wb") as fh:
                    fh.write(b"<?php\n" + block + b"\n")
                proc = subprocess.run(
                    [self.php, str(EXTRACTOR), tmp],
                    capture_output=True, text=True,
                )
                if proc.returncode != 0:
                    raise ExtractionError(
                        f"block is not parsable on its own: {proc.stderr.strip()}"
                    )
            return json.loads(proc.stdout)["n_symbols"]
        finally:
            os.unlink(tmp)
