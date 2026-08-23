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

def _key_from_file() -> str | None:
    """
    Fall back to a key file when the environment does not carry the key.

    Without this, the server depends on whoever launched the client having
    exported the variable first — which works until the day they forget, and
    then the tool silently is not there. A missing MCP server does not
    announce itself; it just fails to appear in the list.

    Looked for, in order: $BIFROST_ENV_FILE, then `.bifrost.env` walking up
    from the working directory to the filesystem root. Accepts `KEY=value`
    or `export KEY=value`, quoted or not. Comments and blank lines ignored.
    """
    import pathlib

    candidates: list[pathlib.Path] = []
    explicit = os.environ.get("BIFROST_ENV_FILE")
    if explicit:
        candidates.append(pathlib.Path(explicit))
    here = pathlib.Path.cwd().resolve()
    candidates += [p / ".bifrost.env" for p in [here, *here.parents]]

    for path in candidates:
        try:
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:]
                key, _, value = line.partition("=")
                if key.strip() != "DEEPSEEK_API_KEY":
                    continue
                return value.strip().strip("'\"") or None
        except OSError:
            continue
    return None


class DeepSeekWorker:
    """DeepSeek-based code transformation worker."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int = 180,
        base_url: str | None = None,
    ):
        """Initialize the worker.

        Args:
            api_key: Worker API key. Falls back to BIFROST_WORKER_API_KEY,
                     then DEEPSEEK_API_KEY, then a `.bifrost.env` file.
            model: Model identifier. Falls back to BIFROST_WORKER_MODEL,
                   then "deepseek-chat".
            timeout: Request timeout in seconds (default: 180).
            base_url: OpenAI-compatible endpoint. Falls back to
                      BIFROST_WORKER_BASE_URL, then DeepSeek's.

        The endpoint is deliberately configurable. Nothing in the protocol is
        DeepSeek-specific — it is an OpenAI-compatible chat completion with a
        JSON response format — so Ollama, llama.cpp, LM Studio or vLLM serve
        equally well. A local endpoint is the only configuration in which the
        code being patched never leaves the machine.

        Local endpoints usually need no credential. When the base URL is not
        the default and no key is found, an empty one is sent rather than
        refusing to start: demanding a key that the server will ignore would
        block the exact setup this indirection exists to allow.

        Raises:
            RuntimeError: If a remote endpoint is configured with no key.
        """
        base_url = base_url or os.environ.get(
            "BIFROST_WORKER_BASE_URL") or API_URL_BASE
        model = model or os.environ.get(
            "BIFROST_WORKER_MODEL") or MODEL_DEFAULT
        if api_key is None:
            api_key = (os.environ.get("BIFROST_WORKER_API_KEY")
                       or os.environ.get("DEEPSEEK_API_KEY")
                       or _key_from_file())
        if not api_key:
            if base_url != API_URL_BASE:
                api_key = ""      # local endpoint, credential not required
            else:
                raise RuntimeError(
                    "No worker API key found. Either export "
                    "DEEPSEEK_API_KEY (or BIFROST_WORKER_API_KEY), or put it "
                    "in a `.bifrost.env` file at your project root as "
                    "DEEPSEEK_API_KEY=sk-... (chmod 600, and keep it out of "
                    "version control). For a local worker set "
                    "BIFROST_WORKER_BASE_URL instead — no key is then needed."
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
