"""
DeepSeek worker module for MCP-Bifrost.

Generalizes the proven DeepSeek client from calibratge/calibra.py into a
reusable worker class. Dependencies: stdlib only.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol


# ---------------------------------------------------------------- constants

API_URL_BASE = "https://api.deepseek.com"
MODEL_DEFAULT = "deepseek-chat"

SYSTEM_PROMPT = """You are a code transformation worker. You receive a JSON payload and return a JSON object. You never explain, never apologize, never add prose.

INPUT KEYS:
  lang       language of the code
  sym        the symbol being modified
  intent     the modification to perform
  ctx        signatures of related code, for reference only
  src        the exact code block to transform
  indent     the indentation that will be re-applied to your output; informational, do not reproduce it

OUTPUT KEYS (JSON object, nothing else):
  out       the complete transformed code block, raw, no markdown fences
  why       one short technical sentence
  diff_stat "+N/-M"

HARD RULES:
1. `out` replaces `src` verbatim in the source file. It must be a drop-in replacement.
2. Preserve the original indentation of every line exactly as given in `src`.
3. Change ONLY what `intent` asks. Touch nothing else.
4. Never add, remove, or rename anything outside the scope of `intent`.
5. Never wrap `out` in markdown code fences.
6. Keep the language and wording of existing comments; do not translate them.
7. The code in `src` has been normalised to zero indentation. Return `out` at zero indentation too. Do not add leading whitespace to the outermost lines.
8. An EMPTY `src` means you are AUTHORING new code, not transforming it. `out` must then be the complete new code, written from `intent` and any exemplar in `ctx`. Never return an empty `out` — there is nothing to leave unchanged."""


# ---------------------------------------------------------------- helpers

def strip_fences(text: str) -> str:
    """Strip markdown fences if the worker added them despite instructions."""
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines[0].lstrip("`").strip() in ("", "php", "python", "py"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines)
    return text


# ---------------------------------------------------------------- result type

@dataclass
class WorkerResult:
    """Result of a worker run."""
    ok: bool                  # whether the run succeeded
    out: str | None           # transformed code block, fences already stripped
    why: str | None           # why explanation from response
    diff_stat: str | None     # diff statistics from response
    error: str | None         # error message if something went wrong
    ms: int                   # elapsed time in milliseconds
    tokens_in: int | None     # prompt tokens consumed
    tokens_out: int | None    # completion tokens produced
    cache_hit: int | None     # cache hit tokens
    request_bytes: int        # length of JSON payload sent, utf-8 bytes
    response_bytes: int       # length of JSON response received, utf-8 bytes


# ---------------------------------------------------------------- protocol

class WorkerClient(Protocol):
    """Protocol for worker implementations."""
    def run(self, payload: dict) -> WorkerResult:
        """Execute a transformation task."""
        ...


# ---------------------------------------------------------------- worker

class DeepSeekWorker:
    """DeepSeek-based code transformation worker."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = MODEL_DEFAULT,
        timeout: int = 180,
        base_url: str = API_URL_BASE,
    ):
        """Initialize the worker.

        Args:
            api_key: DeepSeek API key. Defaults to DEEPSEEK_API_KEY environment
                     variable. Raises RuntimeError if neither is provided.
            model: Model identifier (default: "deepseek-chat").
            timeout: Request timeout in seconds (default: 180).
            base_url: API base URL (default: "https://api.deepseek.com").

        Raises:
            RuntimeError: If DEEPSEEK_API_KEY is not set and api_key is None.
        """
        if api_key is None:
            api_key = os.environ.get("DEEPSEEK_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "DEEPSEEK_API_KEY environment variable not set. "
                    "Set it or pass api_key to DeepSeekWorker."
                )
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.base_url = base_url

    def run(self, payload: dict) -> WorkerResult:
        """Execute a transformation task.

        Args:
            payload: Dictionary with keys: lang, sym, intent, ctx, src, and
                     optionally indent.

        Returns:
            WorkerResult with the outcome, timing, and token usage.
        """
        t0 = time.time()

        # Prepare request body
        body_dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
            "max_tokens": 4096,
        }
        body = json.dumps(body_dict).encode("utf-8")
        request_bytes = len(body)

        # Make request
        url = f"{self.base_url}/chat/completions"
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                response_data = resp.read()
                response_bytes = len(response_data)
                data = json.loads(response_data.decode("utf-8"))
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8")[:400]
            ms = int((time.time() - t0) * 1000)
            return WorkerResult(
                ok=False,
                out=None,
                why=None,
                diff_stat=None,
                error=f"HTTP {e.code}: {error_body}",
                ms=ms,
                tokens_in=None,
                tokens_out=None,
                cache_hit=None,
                request_bytes=request_bytes,
                response_bytes=0,
            )
        except Exception as e:  # noqa: BLE001
            ms = int((time.time() - t0) * 1000)
            return WorkerResult(
                ok=False,
                out=None,
                why=None,
                diff_stat=None,
                error=repr(e),
                ms=ms,
                tokens_in=None,
                tokens_out=None,
                cache_hit=None,
                request_bytes=request_bytes,
                response_bytes=0,
            )

        ms = int((time.time() - t0) * 1000)

        # Extract usage info
        usage = data.get("usage", {})
        tokens_in = usage.get("prompt_tokens")
        tokens_out = usage.get("completion_tokens")
        cache_hit = usage.get("prompt_cache_hit_tokens")

        # Parse response content
        try:
            content = data["choices"][0]["message"]["content"]
            response_json = json.loads(content)
        except Exception as e:  # noqa: BLE001
            return WorkerResult(
                ok=False,
                out=None,
                why=None,
                diff_stat=None,
                error=f"response not parseable: {e!r}",
                ms=ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cache_hit=cache_hit,
                request_bytes=request_bytes,
                response_bytes=response_bytes,
            )

        # Validate response structure
        if not isinstance(response_json, dict) or "out" not in response_json:
            return WorkerResult(
                ok=False,
                out=None,
                why=None,
                diff_stat=None,
                error="response missing 'out' key or not a dict",
                ms=ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cache_hit=cache_hit,
                request_bytes=request_bytes,
                response_bytes=response_bytes,
            )

        # Extract output
        out_raw = response_json["out"]
        if not isinstance(out_raw, str):
            return WorkerResult(
                ok=False,
                out=None,
                why=None,
                diff_stat=None,
                error=f"`out` is not a string but {type(out_raw).__name__}",
                ms=ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cache_hit=cache_hit,
                request_bytes=request_bytes,
                response_bytes=response_bytes,
            )

        out = strip_fences(out_raw)
        why = response_json.get("why")
        diff_stat = response_json.get("diff_stat")

        return WorkerResult(
            ok=True,
            out=out,
            why=why,
            diff_stat=diff_stat,
            error=None,
            ms=ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cache_hit=cache_hit,
            request_bytes=request_bytes,
            response_bytes=response_bytes,
        )
