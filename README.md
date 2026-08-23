# MCP-Bifrost

**Status: design and validation. No server code written yet.**

An MCP server that takes code work already analysed and split up by an
orchestrating model, extracts the exact target block with a real parser,
delegates the rewriting to a cheaper worker model, validates the result,
applies it atomically to disk, and records the whole thing outside the
orchestrator's context.

DeepSeek is the muscle. Claude is the head. Bifrost is the nerve between
them — and the part that guarantees nothing reaches disk broken.

---

## Why

A large codebase edited by an LLM has one real bottleneck, and it is not
intelligence: it is context. Reading a 4,600-line file to change thirty
lines of it burns the orchestrator's window on text it will never use
again.

Bifrost's premise is that the *mechanical* half of coding — writing the
replacement text — does not need the expensive model, and does not need to
pass through its context at all.

**The honest version of the economics** (see
[`docs/critical-review.md`](docs/critical-review.md), RF-4): for a single
small edit the saving is real but modest, because the orchestrator usually
had to read the code anyway to say what it wanted. The order-of-magnitude
win is in **volume** — transformations across many symbols where the
instruction can be written without reading anything:

| | Orchestrator does it all | Via Bifrost |
|---|---|---|
| 202 methods × ~800 tok | **~161,000 tok** — exceeds a context window | ~15,000 tok |

That is the use case this project is built for. Not "fix this bug."

---

## How it works

```
you ──▶ Claude Code ──▶ MCP-Bifrost ──▶ DeepSeek
         analyses,        parses,          writes one
         splits work      validates,       isolated block
                          applies, logs
                              │
                              ├──▶ source file (atomic splice)
                              └──▶ .bifrost/history.db
```

The orchestrator decides *what* and *how*. The worker decides nothing. The
server is the only component allowed to touch disk, and it refuses to do so
until every gate passes.

### Validation gates

Nothing is written until all of these hold:

| Gate | Checks | Can it fail? |
|---|---|---|
| **0 — offsets** | the block currently on disk is byte-identical to what we sent the worker | ✅ yes, and it is the critical one |
| **1 — syntax** | the reconstructed file passes `php -l` / `ast.parse()` | ✅ yes |
| **2 — one symbol** | the returned block defines exactly one symbol | ✅ yes |
| **3 — substance** | no calls, variables or control keywords silently vanished | ✅ yes |

A "perimeter check" comparing bytes outside the target range was specified,
designed, and then **removed**: the server builds the new file as
`original[:start] + block + original[end:]`, so the perimeter is preserved
*by construction* and the check can never fail. Calibration confirmed it
empirically — it reported 9/9 while three files were syntactically broken.
See RF-1 in the critical review.

### Rollback

Git is already a content-addressed database, so we use it as one.
`git hash-object -w` before each patch yields a blob SHA that goes in the
log; reverting is `git cat-file blob`. Deduplicated and compressed for
free, works with a dirty working tree, and there is no bespoke snapshot
format to maintain.

---

## Repository layout

| Path | What |
|---|---|
| [`docs/`](docs/) | English documentation — architecture, review, results, licensing |
| `brainstorm/` | the Catalan working journal the design was argued out in |
| `calibratge/` | the measurement harness (see below) |

---

## Calibration

Before writing a line of the server, one question had to be answered:

> Given a real method from the target codebase, packed with the compact
> schema, does the worker return code that can be applied without breaking
> anything?

The harness in `calibratge/` answers it. **Zero dependencies** — Python
stdlib plus the `php` binary.

```bash
export DEEPSEEK_API_KEY=...
python3 calibratge/calibra.py --dry-run    # show cases, no API calls
python3 calibratge/calibra.py --casos 9    # real run
```

**Result: the premise holds.** 9/9 valid JSON, 3/3 byte-identical on the
identity task, 3/3 with no original lines lost, 0/9 wrapped in markdown
fences, 2.6 s average latency.

It also caught a byte-offset bug that had nothing to do with the worker and
would have corrupted files silently in production. Full write-up:
[`docs/calibration.md`](docs/calibration.md).

---

## Documentation

| Document | What it is |
|---|---|
| [Architecture](docs/architecture.md) | what gets built and why |
| [Critical review](docs/critical-review.md) | a fresh-eyes pass hunting for reasons this fails — twelve findings, two later refuted by measurement |
| [Calibration results](docs/calibration.md) | what the worker actually did when asked |
| [Licensing](docs/licensing.md) | what we consume, what we grant |

The design was argued out in Catalan in `brainstorm/`, kept as the working
record. `docs/` is the English reference and is authoritative where the two
differ.

---

## License

[Apache License 2.0](LICENSE).

Use it, modify it, redistribute it, build on it commercially. Keep the
notices and say what you changed.

The reasoning behind choosing a permissive license over a no-resale one, and
why Apache-2.0 rather than MIT, is in [`docs/licensing.md`](docs/licensing.md).

Built on the [Model Context Protocol](https://modelcontextprotocol.io),
MIT-licensed by Anthropic, PBC. MCP-Bifrost is an independent project and is
not affiliated with, endorsed by, or sponsored by Anthropic, PBC.
