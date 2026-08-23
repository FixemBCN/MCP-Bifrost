"""
Heimdall — the gate on the bridge.

Nothing crosses to the worker without being looked at. The worker is a
third-party API on someone else's infrastructure, and the code we hand it is
production source; once a secret has left this machine it cannot be recalled.

Two rules shape everything here:

1. **The default is not to leak.** A finding blocks the send. Overriding is
   possible, explicit, and recorded — never implicit.
2. **Heimdall never repeats what it saw.** Findings carry a pattern name and
   a line number, never the matched text. A gate that logs the secret it
   caught has moved the secret, not stopped it.

Being wrong in the cautious direction costs one rejection the orchestrator
can override. Being wrong in the other direction costs a credential.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    pattern: str
    line: int
    hint: str
    # Byte span of the matched text within the scanned content. Needed to
    # redact, and therefore the one field that carries information derived
    # from the secret. It is an offset pair, not the text — but treat the
    # whole Finding as local-only regardless: never log it wholesale, use
    # __str__ or describe(), which are span-free by construction.
    span: tuple[int, int] | None = None

    def __str__(self) -> str:
        return f"line {self.line}: {self.pattern} ({self.hint})"


# Each entry: (name, regex, hint). Ordered roughly by confidence — the
# specific vendor formats first, the shapeless ones last.
#
# Deliberately not exhaustive. A pattern list is a floor, not a ceiling: it
# catches the shapes that are cheap to recognise, and the entropy check below
# covers what has no shape at all.
PATTERNS: list[tuple[str, re.Pattern[bytes], str]] = [
    ("private-key",
     re.compile(rb"-----BEGIN[ A-Z]*PRIVATE KEY-----"),
     "PEM private key"),

    ("aws-access-key",
     re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
     "AWS access key id"),

    ("google-api-key",
     re.compile(rb"\bAIza[0-9A-Za-z_\-]{35}\b"),
     "Google API key"),

    ("gcp-service-account",
     re.compile(rb'"type"\s*:\s*"service_account"'),
     "Google service-account credentials"),

    ("slack-token",
     re.compile(rb"\bxox[baprs]-[0-9A-Za-z-]{10,}"),
     "Slack token"),

    ("github-token",
     re.compile(rb"\bgh[pousr]_[0-9A-Za-z]{36,}\b"),
     "GitHub token"),

    ("stripe-key",
     re.compile(rb"\b[sr]k_(?:live|test)_[0-9A-Za-z]{16,}\b"),
     "Stripe secret key"),

    ("openai-style-key",
     re.compile(rb"\bsk-[0-9A-Za-z]{32,}\b"),
     "API key in sk- form"),

    ("jwt",
     re.compile(rb"\beyJ[0-9A-Za-z_\-]{10,}\.eyJ[0-9A-Za-z_\-]{10,}\."),
     "JSON Web Token"),

    ("bearer-token",
     re.compile(rb"[Bb]earer\s+[0-9A-Za-z._\-]{20,}"),
     "bearer token in an Authorization header"),

    ("connection-string",
     re.compile(rb"\b(?:mysql|postgres(?:ql)?|mongodb(?:\+srv)?|redis|amqp)"
                rb"://[^\s:/@]+:[^\s:/@]+@"),
     "connection string carrying a password"),

    # Assignment of a credential-shaped name to a non-trivial literal.
    # `$password = $data['password']` is fine; `$password = 'hunter2xyz'` is
    # not. Requires 8+ chars to skip placeholders like '' and 'x'.
    ("credential-literal",
     re.compile(
         rb"""(?ix)
         \b (?: pass(?:wd|word)? | passwd | secret | api[_-]?key
              | auth[_-]?token | access[_-]?token | client[_-]?secret
              | private[_-]?key )
         \s* (?: = | => | : ) \s*
         (['"]) (?!\s*[\$\{<%]) ([^'"\n]{8,}) \1
         """),
     "credential assigned to a string literal"),
]

# Patterns specific enough that a match is worth reporting even on a line that
# also says "example". The others are heuristics, and heuristics are what the
# placeholder suppression below exists to quieten.
#
# This split matters. Suppressing a whole LINE on the word "example" is
# fail-open: `$prod = "AKIA...."; // see example below` would sail through.
# A gate whose noise reduction can hide a real key is not a gate.
HIGH_CONFIDENCE = frozenset({
    "private-key", "aws-access-key", "google-api-key", "gcp-service-account",
    "slack-token", "github-token", "stripe-key", "openai-style-key", "jwt",
    "bearer-token", "connection-string",
})

# Patterns whose match is a self-contained token that can be swapped for a
# placeholder without changing what the surrounding code means.
#
# Excluded on purpose:
#   private-key         the regex matches only the PEM header; redacting that
#                       would leave the key body sitting in the payload.
#   gcp-service-account it matches a type marker, not the credential.
#   high-entropy        the run may BE the logic — a base64 constant the code
#                       depends on. Substituting it changes the program.
REDACTABLE = frozenset({
    "aws-access-key", "google-api-key", "slack-token", "github-token",
    "stripe-key", "openai-style-key", "jwt", "bearer-token",
    "connection-string", "credential-literal",
})

# For patterns where only part of the match is the secret. `credential-literal`
# matches `$password = "…"` but only the quoted value may be replaced; swapping
# the whole match would delete the assignment.
SPAN_GROUP: dict[str, int] = {"credential-literal": 2}

PLACEHOLDER = "__BIFROST_SECRET_{}__"
_PLACEHOLDER_RX = re.compile(rb"__BIFROST_SECRET_(\d+)__")

# Names that look like credentials but are the codebase talking about them
# rather than holding one. Only ever silences the heuristic patterns.
_INNOCENT = re.compile(
    rb"""(?ix)
    ^ (?: .* (?: example | sample | placeholder | dummy | changeme
                | your[_-]? (?:\w+ [_-])* (?:key|token|secret|password|pass)
                | my[_-]? (?:\w+ [_-])* (?:key|token|secret)
                | test[_-]?(?:key|token|secret) | fake | notarealkey
                | __bifrost_secret_\d+__
                | xxx+ | \.\.\. ) .* ) $
    """
)

_B64ISH = re.compile(rb"[A-Za-z0-9+/_\-]{32,}={0,2}")


def _entropy(data: bytes) -> float:
    """Shannon entropy in bits per byte."""
    if not data:
        return 0.0
    counts: dict[int, int] = {}
    for b in data:
        counts[b] = counts.get(b, 0) + 1
    n = len(data)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _high_entropy_strings(line: bytes, threshold: float = 4.4) -> bool:
    """
    A long, dense, random-looking run with no shape we recognise.

    Threshold is set high enough that base64-encoded English, minified code
    and long hex hashes of public data mostly stay below it. It will still
    miss short secrets; that is what the pattern list is for.
    """
    for m in _B64ISH.finditer(line):
        token = m.group(0)
        if len(token) < 32:
            continue
        if _entropy(token) >= threshold:
            return True
    return False


def scan(content: bytes, entropy: bool = True) -> list[Finding]:
    """
    Look for secrets. Returns findings, never the matched text.

    `entropy` disables the shapeless check, which is the one that produces
    false positives on minified assets and long hashes.
    """
    findings: list[Finding] = []
    offset = 0
    for lineno, line in enumerate(content.split(b"\n"), 1):
        looks_illustrative = bool(_INNOCENT.match(line.strip()))
        for name, rx, hint in PATTERNS:
            if name not in HIGH_CONFIDENCE and looks_illustrative:
                continue
            for m in rx.finditer(line):
                g = SPAN_GROUP.get(name, 0)
                start, end = m.span(g)
                findings.append(
                    Finding(name, lineno, hint,
                            span=(offset + start, offset + end))
                )
        if entropy and not looks_illustrative and _high_entropy_strings(line):
            findings.append(
                Finding("high-entropy", lineno,
                        "long random-looking string with no recognised shape")
            )
        offset += len(line) + 1  # +1 for the newline split consumed
    return findings


def scan_payload(src: str, context: list[str] | None = None,
                 entropy: bool = True) -> list[Finding]:
    """Everything that would leave the machine, in one pass."""
    findings = scan(src.encode("utf-8"), entropy=entropy)
    for i, c in enumerate(context or []):
        findings.extend(
            Finding(f.pattern, f.line, f"{f.hint} [context {i}]")
            for f in scan(c.encode("utf-8"), entropy=entropy)
        )
    return findings


def describe(findings: list[Finding], limit: int = 5) -> str:
    """
    A rejection message the orchestrator can act on without seeing a secret.
    """
    shown = "; ".join(str(f) for f in findings[:limit])
    more = f" (+{len(findings) - limit} more)" if len(findings) > limit else ""
    return (
        f"Heimdall blocked the send: {shown}{more}. "
        f"Nothing was transmitted. If this is a false positive, retry with "
        f"allow_secrets=true — that decision is recorded."
    )


# ------------------------------------------------------------------ redaction

class Vault:
    """
    Placeholder → original secret, held in memory for one round trip.

    Never printed, never logged, never serialised. `__repr__` is overridden
    because a Vault landing in a traceback or a debug print would undo the
    entire point of the gate.
    """

    __slots__ = ("_map", "_from_context")

    def __init__(self) -> None:
        self._map: dict[bytes, bytes] = {}
        self._from_context: set[bytes] = set()

    def add(self, placeholder: bytes, secret: bytes,
            from_context: bool = False) -> None:
        self._map[placeholder] = secret
        if from_context:
            self._from_context.add(placeholder)

    def __len__(self) -> int:
        return len(self._map)

    def __bool__(self) -> bool:
        return bool(self._map)

    def placeholders(self) -> list[bytes]:
        """Only the ones that belong in the output — the src-derived ones."""
        return [p for p in self._map if p not in self._from_context]

    def context_placeholders(self) -> list[bytes]:
        return list(self._from_context)

    def __repr__(self) -> str:
        return f"<Vault {len(self._map)} secrets, contents withheld>"

    __str__ = __repr__


def redact(content: bytes, entropy: bool = True, vault: Vault | None = None,
           from_context: bool = False, start_index: int = 0
           ) -> tuple[bytes, Vault, list[Finding]]:
    """
    Replace every redactable secret with a placeholder.

    Returns the redacted content, the vault needed to put the secrets back,
    and the findings that could NOT be redacted — those still have to block,
    because for them substitution would either leave part of the secret
    behind or change what the code means.
    """
    findings = scan(content, entropy=entropy)
    redactable = [f for f in findings
                  if f.pattern in REDACTABLE and f.span is not None]
    blocked = [f for f in findings if f not in redactable]

    vault = vault if vault is not None else Vault()
    # Right to left, so earlier spans keep their offsets.
    out = content
    n = len(redactable)
    for i, f in enumerate(sorted(redactable, key=lambda x: -x.span[0])):
        start, end = f.span
        token = PLACEHOLDER.format(start_index + n - 1 - i).encode("ascii")
        vault.add(token, out[start:end], from_context=from_context)
        out = out[:start] + token + out[end:]
    return out, vault, blocked


def redact_payload(src: str, context: list[str] | None = None,
                   entropy: bool = True
                   ) -> tuple[str, list[str], Vault, list[Finding]]:
    """
    Redact everything that would leave the machine.

    Context is redacted into the SAME vault but marked as context-derived,
    which is what lets restore() tell the two apart. That distinction is the
    point: a placeholder from `src` belongs back in the file, while one from
    an exemplar appearing in the worker's output means it copied a credential
    out of a neighbouring file into a new one. Restoring that would be
    obediently propagating a secret.
    """
    vault = Vault()
    red_src, vault, blocked = redact(src.encode("utf-8"), entropy=entropy,
                                     vault=vault)
    red_ctx: list[str] = []
    for i, c in enumerate(context or []):
        rc, vault, b = redact(c.encode("utf-8"), entropy=entropy, vault=vault,
                              from_context=True, start_index=1000 + i * 100)
        red_ctx.append(rc.decode("utf-8"))
        blocked.extend(
            Finding(f.pattern, f.line, f"{f.hint} [context {i}]", f.span)
            for f in b
        )
    return red_src.decode("utf-8"), red_ctx, vault, blocked


class RestoreError(RuntimeError):
    pass


def restore(content: bytes, vault: Vault) -> bytes:
    """
    Put the secrets back. Raises rather than returning partial output.

    Every placeholder must come back exactly once. A missing one means the
    worker dropped, reformatted or rewrote it, and writing that to disk would
    put `__BIFROST_SECRET_0__` into production source — destroying a live
    credential silently, since the code still compiles and nothing fails
    until something stops connecting. That is strictly worse than refusing.

    Duplicates are refused too: a worker that copied the line has changed the
    program in a way nobody asked for, and quietly writing the same secret
    twice is not a decision this function gets to make.
    """
    smuggled = [t.decode() for t in vault.context_placeholders()
                if t in content]
    if smuggled:
        raise RestoreError(
            f"the worker copied {len(smuggled)} redacted secret(s) out of the "
            f"reference material into its output ({', '.join(smuggled[:3])}). "
            f"Nothing was written — that is a credential being propagated, "
            f"not a patch."
        )

    missing, duplicated = [], []
    for token in vault.placeholders():
        n = content.count(token)
        if n == 0:
            missing.append(token.decode())
        elif n > 1:
            duplicated.append(f"{token.decode()}×{n}")

    if missing or duplicated:
        parts = []
        if missing:
            parts.append(f"{len(missing)} placeholder(s) did not come back: "
                         + ", ".join(missing[:4]))
        if duplicated:
            parts.append("duplicated: " + ", ".join(duplicated[:4]))
        raise RestoreError(
            "; ".join(parts)
            + ". Nothing was written — restoring a partial result would put a "
              "placeholder where a live credential belongs."
        )

    out = content
    for token in vault.placeholders():
        out = out.replace(token, vault._map[token])

    # Belt and braces: no placeholder of ours may survive into the file.
    leftover = _PLACEHOLDER_RX.search(out)
    if leftover:
        raise RestoreError(
            f"a placeholder survived restoration ({leftover.group(0).decode()}); "
            f"refusing to write."
        )
    return out
