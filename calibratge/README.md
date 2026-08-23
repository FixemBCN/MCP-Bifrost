# Calibration harness

Measures whether the MCP-Bifrost premise holds, **before** a line of the
server gets written.

One question: given a real method from the target codebase, packed with the
compact schema from the spec, does the worker return code that can be
applied without breaking anything?

## Dependencies

None. Python 3 stdlib plus the `php` binary (tested against PHP 8.5).

## Usage

```bash
export BIFROST_TARGET=/path/to/the/codebase/you/want/to/patch
python3 calibra.py --dry-run          # list the cases, no API calls

export DEEPSEEK_API_KEY=sk-...
python3 calibra.py --cases 9          # real run
```

Point `BIFROST_TARGET` at your own codebase. The harness picks real methods
out of it, because that is the only thing worth measuring — a synthetic
fixture tells you nothing about whether the worker can handle *your* code.
`BIFROST_TARGET_GLOB` narrows the search (default `**/*.php`).

Results land in `resultats/` (git-ignored — the output embeds fragments of
the target codebase).

## Parts

| File | What |
|---|---|
| `extract.php` | exact symbol map via `token_get_all()`, PHP's official tokenizer. Validated against all 128 first-party PHP files of the target project: zero failures. |
| `calibra.py` | case selection, worker call, gate-by-gate evaluation |
| `registre.py` | reads the operational log and reports measured efficacy |

## Test tasks

| Task | What it measures |
|---|---|
| `identity` | Can it return a block untouched when told to? If this fails, nothing else matters. |
| `phpdoc` | Additive: no original line may disappear. |
| `guard` | Behavioural: the actual use case. |

## Log reader

Two purposes, two lifetimes, **one write**:

- `.bifrost/history.db` — operational log. SQLite, permanent, source of
  truth. Instructions, rationale, rollback blobs, which gate rejected what.
- `.bifrost/efficacy.jsonl` — **derived** from it. Measurements only, no free
  text. Exists to answer one question ("does the saving justify the latency
  and the risk?") and gets deleted once it is answered.

The JSONL is never written alongside SQLite; it is exported from it. Writing
both on every operation would let them diverge if the process died in
between — and you would not know which one was right precisely when the log
mattered most.

```bash
python3 registre.py .bifrost/history.db --detail
python3 registre.py .bifrost/history.db --export .bifrost/efficacy.jsonl
python3 registre.py .bifrost/efficacy.jsonl        # same numbers
```

The log stores **measurements**, never conclusions: the saving estimate is
derived at read time, so the formula can be corrected without invalidating
history. And it is compared against what the orchestrator would have spent
doing the edit itself — not against sending the whole file, which is the
inflated number RF-4 calls out.
