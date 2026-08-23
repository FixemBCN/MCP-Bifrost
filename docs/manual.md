# MCP-Bifrost — what it is, what it does, how to run it

## What it is

An **MCP server**. Not a fork of anything, not a wrapper around an existing
tool — an original, standalone implementation of the Model Context Protocol
that a coding agent connects to as one more tool provider.

It sits between two models and does the part neither of them should:

```
you ──▶ Claude ──────────▶ MCP-Bifrost ──────────▶ DeepSeek
        decides what        parses, validates,      writes one
        and how, splits     applies, records        isolated block
        the work up
```

**Claude is the mastermind. DeepSeek is the minion.** Claude decides what
needs doing and how, and splits the work into isolated units. DeepSeek writes
the code for one unit at a time and decides nothing. Bifrost is the nerve
between them, and the only component allowed to touch your files.

That division is the whole idea. The expensive model's context is spent on
judgement; the cheap model does the typing; and a deterministic layer in the
middle refuses to write anything it cannot verify.

## What it is for

Not "fix this bug." For a single edit, an agent has to read the code anyway
to say what it wants, so routing it through a second model saves little.

Bifrost earns its place on **volume** — one instruction applied across many
symbols that the orchestrator never reads:

> "Add a docblock to every method in this class."
> "Switch every call to the old API."
> "Add error logging to every method in this directory."

| | Agent does it all | Via Bifrost |
|---|---|---|
| 202 methods × ~800 tok | **~161,000 tok** — exceeds a context window | ~15,000 tok |

That is a job that did not fit before, not a job that got slightly cheaper.

## What it can do

Eleven tools:

| Tool | What it does |
|---|---|
| `fix_symbols` | one instruction across many symbols — **the main one**. Calls in parallel, writes in series |
| `fix_symbol` | rewrite one named function, method or class |
| `fix_range` | rewrite an explicit line range, for code outside any symbol |
| `insert_symbol` | add a method or class next to an existing one |
| `insert_case` | add a branch to a `switch` router |
| `create_file` | write a new file, optionally **by analogy** with an existing one |
| `patch_group` | several operations as one transaction: all land, or none |
| `export_docs` | turn the log into a changelog. On demand only |
| `publish_session` | put the batch on a branch as one reviewable commit |
| `revert_patch` | undo one patch |
| `revert_session` | undo the whole batch, newest first |

### Languages

**PHP and Python.** Each is analysed with its own official tooling — PHP
through `token_get_all()`, Python through `ast` — rather than a common
third-party grammar. That is less uniform and more faithful, and fidelity is
what matters when the answer decides whether a file gets overwritten.

Java was considered and dropped: it has no equivalent official lexer callable
for free, so it would introduce the project's first third-party dependency.
Adding a language means writing one adapter; the rest of the system does not
know what it is patching.

### What it will not do

- Write anything that fails a validation gate.
- Send a credential to the worker (see Heimdall, below).
- Overwrite a file through `create_file`.
- Patch a file outside a git repository — rollback depends on git's object
  store.
- Touch a symbol over ~150 lines, where the whole premise stops paying.
- Push, branch or open a pull request as a side effect. Those are separate,
  explicit tools.

## How it keeps your code safe

Nothing reaches disk until every gate passes:

| Gate | Checks | Default |
|---|---|---|
| **offsets** | the block on disk is byte-identical to what we sent the worker | on |
| **syntax** | the reconstructed file parses (`php -l`, `ast.parse`) | on |
| **one symbol** | the returned block defines exactly one thing | on |
| **substance** | no call, variable or control keyword silently vanished | **off** |

The substance gate ships disabled. It is a coarse regex check that never
fired during calibration, and a gate that rejects good patches is worse than
one waiting to be armed — but that means three gates protect you by default,
not four. Turn it on with `substance_gate=True` before bulk work.

A "perimeter check" comparing bytes outside the target range was specified,
built, and then **deleted**: the server constructs the file as
`original[:start] + block + original[end:]`, so the perimeter is preserved by
construction and the check could never fail. During calibration it reported
9/9 while three files were left syntactically broken.

**Rollback** uses git as the content-addressed database it already is:
`git hash-object -w` before each patch, `git cat-file blob` to restore. No
snapshot format, no extra disk, and it works with a dirty working tree.

**Heimdall** guards the bridge. Where a secret is a self-contained token it is
swapped for a placeholder, the worker transforms the code around it, and the
original goes back before the file is written — so the work proceeds and the
credential never leaves your machine. Every placeholder must return exactly
once or nothing is written. What cannot be safely redacted blocks instead.
Findings carry a pattern name and a line number, never the matched text.

---

## Installing

### Requirements

- Python 3.11+ — **standard library only**, no `pip install` needed
- `php` on `PATH` if you patch PHP (PHP 8.x; also used for validation)
- `git` — rollback depends on it
- A worker: a DeepSeek API key, **or** any OpenAI-compatible endpoint
  (Ollama, llama.cpp, LM Studio, vLLM) via `BIFROST_WORKER_BASE_URL` — no key
  needed for a local one. Note that no local model has been measured yet; the
  calibration harness exists so you can measure yours before trusting it.
- `gh` only if you want `publish_session` to open pull requests

### Get it

```bash
pipx install mcp-bifrost      # or: uv tool install mcp-bifrost
```

Or from source:

```bash
git clone https://github.com/FixemBCN/MCP-Bifrost.git
cd MCP-Bifrost
python3 -m unittest discover tests    # 128 tests, ~15s
```

### Connect it to Claude Code

Add it to `.mcp.json` in the project you want to patch:

```json
{
  "mcpServers": {
    "bifrost": {
      "command": "mcp-bifrost",
      "cwd": "/path/to/the/project/you/are/patching",
      "env": { "BIFROST_DB": ".bifrost/history.db" }
    }
  }
}
```

`cwd` is the repository being patched, not Bifrost's own directory. The log
lands in `.bifrost/` there — add it to that project's `.gitignore`.

**The API key does not go in this file.** Put it in `.bifrost.env` at your
project root and keep it out of version control:

```bash
echo "DEEPSEEK_API_KEY=sk-..." > .bifrost.env
chmod 600 .bifrost.env
echo ".bifrost.env" >> .gitignore
```

The server reads that file when the environment does not carry the key,
walking up from its working directory. Relying on an exported variable
instead works right up until whoever launches the client forgets to export
it — and a missing MCP server does not announce itself, it simply fails to
appear in the tool list.

Restart Claude Code and the eleven tools appear.

### Calibrate against your own code first

Before trusting it, measure whether the worker handles *your* codebase:

```bash
export BIFROST_TARGET=/path/to/your/project
export DEEPSEEK_API_KEY=sk-...
python3 calibratge/calibra.py --cases 9
```

It picks real methods out of your code, sends them through the same schema
the server uses, and reports what came back — byte-identical on the identity
task, syntactically valid, nothing lost. A tenth of a cent, and it answers
the only question that matters before you point this at anything.

### Or drive it directly

```python
from pathlib import Path
from mcp_bifrost.engine import Engine
from mcp_bifrost.worker import DeepSeekWorker

engine = Engine(DeepSeekWorker(), Path(".bifrost/history.db"))
print(engine.fix_symbol("src/Thing.php", "Thing::compute",
                        "Add a guard returning null on empty input").render())
```

### Before you point it at anything that matters

1. **Commit first.** Rollback is reliable, but a clean starting point is
   cheaper than trusting it.
2. **Start on low-risk files.** A directory of small, uniform classes is a
   good first target; the 4,000-line one is not.
3. **Read the first batch's diff.** Not the tenth.
4. **Have something that fails when the code breaks.** See below.

---

## The part you should not skip

Bifrost validates *form*: that a file parses, that a symbol still exists,
that nothing vanished from a block. **No gate here understands what your code
means.** A worker can return something that compiles perfectly and is
semantically wrong, and nothing in this repository will notice.

Automating bulk changes over a codebase with no test coverage multiplies your
risk surface exactly as fast as it multiplies your productivity. If you have
no way to find out that something broke, Bifrost will help you break things
faster.

Full coverage is not the ask. Something executable that loads your classes,
exercises the main paths and fails loudly is enough — and you can build it
*with* Bifrost, against low-risk code, as its first real job.

Two pieces did the job on the codebase this was built against, and the shape
generalises:

- **A structural pass** that needs no data: every file parses, every class
  loads, every route resolves to a handler that exists, no duplicate route
  labels, every `require` points at something. It catches exactly what an
  automated editor breaks, in seconds, with zero risk.
- **A runner over whatever tests you already have**, using their exit codes
  rather than their output. Snapshot your database before, restore it after,
  and **verify the restore by hash** — a restore that silently fails is
  worse than none, because nobody finds out until data is missing.

Building that exposed two tests that had never been able to fail: they
printed their failures and exited zero, so every runner would have counted
them green forever. Assume yours has some of those, and check.

---

## Responsibility

**You are responsible for what this does to your code.**

It is a tool that edits source files automatically using a language model.
It is licensed under Apache 2.0, which means, in the plain words of section
7: it is provided **"AS IS", without warranties or conditions of any kind**.
Nobody who wrote it is liable for what it changes, breaks, or costs you.

Concretely, and none of this is theoretical:

- The worker is a third-party API. Code you patch is sent to it. Heimdall
  reduces that exposure; it does not eliminate it, and it cannot know what
  *you* consider sensitive.
- API calls cost money. There are spending limits, and they are yours to set.
- The gates catch syntax and structure. They do not catch wrong.
- Rollback depends on git. Outside a repository there is none, which is why
  patching outside one is refused.

Read the diffs. Run your tests. Deploy on purpose.

---

## Contributing

**Contributions are welcome — all of them.** Bug reports, a language adapter,
a better secret pattern, a gate nobody thought of, a correction to the docs,
or an argument that something here is wrong.

That last one especially. This project has already deleted one validation
gate and rejected another for being tautological, refuted two of its own
design claims with measurement, and found four real bugs in its own code
through adversarial review. Being shown you are wrong early is the cheapest
thing that happens to a project like this.

One convention, and it is the one that matters:

> **Every test must be able to fail.**
>
> An assertion that holds by construction is worse than no assertion: it
> emits a positive signal without inspecting anything. If you write one,
> delete it. When a test covers something important, break the code on
> purpose and confirm the test notices.

Beyond that: standard library where possible, comments that explain *why*
rather than *what*, and no new dependency without a reason that survives
being questioned.

Apache 2.0 means contributions arrive under Apache 2.0 (§5). There is no CLA
and nothing to sign.

---

## Where to read further

| Document | What it is |
|---|---|
| [architecture.md](architecture.md) | what gets built and why |
| [critical-review.md](critical-review.md) | a fresh-eyes pass hunting for reasons this fails — twelve findings, two later refuted by measurement |
| [calibration.md](calibration.md) | what the worker actually did when asked, and the bug it exposed |
| [licensing.md](licensing.md) | what we consume, what we grant |

[`brainstorm/`](../brainstorm/) holds the working record: the original spec,
the design journal across five revisions, the adversarial review, and the
calibration results. This directory is the reference and wins where the two
differ — the journal is kept for how the conclusions were reached, including
the two findings that measurement later refuted.
