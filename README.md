<!-- mcp-name: io.github.FixemBCN/mcp-bifrost -->

<picture>
  <source media="(prefers-color-scheme: dark)"
          srcset="https://raw.githubusercontent.com/FixemBCN/MCP-Bifrost/main/assets/Bifrost_Logo_DarkBackground.png">
  <img src="https://raw.githubusercontent.com/FixemBCN/MCP-Bifrost/main/assets/Bifrost_Logo_transparentBackground.png"
       alt="MCP-Bifrost" width="60" align="right">
</picture>

# MCP-Bifrost

**Rewrite 200 methods with a cheap model, without a single line of the
result passing through the expensive one's context — and without writing
anything to disk that does not compile.**

[![tests](https://github.com/FixemBCN/MCP-Bifrost/actions/workflows/tests.yml/badge.svg)](https://github.com/FixemBCN/MCP-Bifrost/actions/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/FixemBCN/MCP-Bifrost/blob/main/LICENSE)
[![python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![targets](https://img.shields.io/badge/targets-PHP%20%7C%20Python-777)](https://github.com/FixemBCN/MCP-Bifrost/blob/main/docs/comparison.md)

An MCP server that takes code work already analysed and split up by an
orchestrating model, extracts the exact target block with the language's own
parser, delegates the rewriting to a cheaper worker model, validates the
result, applies it atomically, and records the whole thing outside the
orchestrator's context.

The head decides. The muscle types. Bifrost is the nerve between them — and
the part that guarantees nothing reaches disk broken.

In the examples below the head is Claude and the muscle is DeepSeek, which is
simply the model that was to hand. Neither is a requirement. See
[The worker](#the-worker) for why a 7B model on your own machine may be the
more interesting choice.

---

## Why

A large codebase edited by an LLM has one real bottleneck, and it is not
intelligence: it is context. Reading a 4,600-line file to change thirty lines
of it burns the orchestrator's window on text it will never use again.

Bifrost's premise is that the mechanical half of coding — writing the
replacement text — does not need the expensive model, and does not need to
pass through its context at all.

| | Orchestrator does it all | Via Bifrost |
|---|---|---|
| 202 methods × ~800 tok | **~161,000 tok** — exceeds a context window | ~15,000 tok |

**The honest version** (see [RF-4](https://github.com/FixemBCN/MCP-Bifrost/blob/main/docs/critical-review.md)): for a single
small edit the saving is real but modest, because the orchestrator usually
had to read the code anyway to say what it wanted. The order-of-magnitude win
is in volume — transformations across many symbols where the instruction can
be written without reading anything.

That is the use case this is built for. Not "fix this bug."

**And the number the log reports is a counterfactual.** The test suites in
this repository were written through Bifrost itself: 97,909 bytes applied
from 1,608 bytes of instruction, which the formula scores at 61×. The
realised saving was zero, because the blocks were composed by the
orchestrator rather than by a worker, and every one of those bytes was paid
before Bifrost saw them. The log records what crossed the boundary, not
where it was written. [The worked
example](https://github.com/FixemBCN/MCP-Bifrost/blob/main/docs/architecture.md)
is in §11, kept unflattering on purpose.

---

## When *not* to use it

- **Exploratory work.** "Find why this crashes" is not an instruction Bifrost
  can execute. It needs to know the symbols before it starts.
- **Single small edits.** The token arithmetic is marginal, and we say so
  ([RF-4](https://github.com/FixemBCN/MCP-Bifrost/blob/main/docs/critical-review.md)). Use your agent's normal edit tool.
- **Latency-sensitive loops.** ~2.6 s per block, measured against DeepSeek.
- **Anything that is not PHP or Python.** Adding a language means writing a
  parser adapter, not rewriting the core — but it is not there today.
- **Cross-file refactors where one edit's shape depends on another's
  outcome.** `patch_group` gives atomicity, not sequencing.
- **Codebases with no way of telling you something broke.** Every gate here
  checks form; none understands meaning.

How this sits next to Aider, Serena and fast-apply models — including where
they are better — is in [`docs/comparison.md`](https://github.com/FixemBCN/MCP-Bifrost/blob/main/docs/comparison.md).

---

## How it works

```
you ──▶ Claude Code ──▶ MCP-Bifrost ──▶ worker model
         analyses,        parses,          writes one
         splits work      validates,       isolated block
                          applies, logs
                              │
                              ├──▶ source file (atomic splice)
                              └──▶ .bifrost/history.db
```

The orchestrator decides what and how. The worker decides nothing. The server
is the only component allowed to touch disk, and it refuses until every gate
passes.

### Validation gates

| Gate | Checks | Default |
|---|---|---|
| **0 — offsets** | the block on disk is byte-identical to what we sent the worker | on |
| **1 — syntax** | the rebuilt file passes `php -l` / `ast.parse()` | on |
| **2 — one symbol** | the returned block defines exactly one symbol — a class counts as one, whatever it holds | on |
| **3 — substance** | no call, variable or control keyword vanished silently | **off** |

**Three are on by default, not four.** The substance gate is a coarse regex
check that never fired during calibration, and a gate that rejects good
patches is worse than one waiting to be armed. Enable it with
`substance_gate=True` before bulk work.

A "perimeter check" comparing bytes outside the target range was specified,
built, and then **deleted**: the server rebuilds the file as
`original[:start] + block + original[end:]`, so the perimeter is preserved by
construction and the check can never fail. Calibration confirmed it — the
gate reported 9/9 while three files were left syntactically broken. See
[RF-1](https://github.com/FixemBCN/MCP-Bifrost/blob/main/docs/critical-review.md).

### Rollback

Git is already a content-addressed database, so it is used as one.
`git hash-object -w` before each patch yields a blob SHA that goes in the
log; reverting is `git cat-file blob`. Deduplicated and compressed for free,
works with a dirty working tree, and there is no bespoke snapshot format to
maintain.

---

## The worker

DeepSeek is what was to hand, and every number in this repository was
measured against it. It is not a requirement, and it is probably not the most
interesting way to run this.

The worker's job is deliberately narrow. It receives one isolated block and
one instruction, and returns one block. It does not choose files, plan
changes, decide what to edit, or see anything else in the codebase. That is a
task a 7B coding model can do — and the gates exist precisely so a weak
worker's mistakes are caught before they reach disk rather than after.

Which makes the local case the more compelling one:

- **Your code never leaves the machine.** For a proprietary codebase that is
  not a preference, it is a precondition.
- **Cost goes to zero** on exactly the workload this is built for, where
  hundreds of blocks in one run is normal rather than extreme.
- **The context requirement is tiny.** One method, not one file. An 8k window
  is plenty; the whole design is that the worker never sees more than it needs.
- **A weak worker is an acceptable worker** when every output is parsed,
  syntax-checked and diffed before it counts for anything. A bad block costs
  a retry, not a corrupted file.

That last one is the real argument. Delegating code generation to a small
local model is normally a bad idea because you cannot trust the output and
checking it by hand costs more than writing it. Bifrost's answer is that the
checking is mechanical, and the machine can do it.

Any OpenAI-compatible endpoint works — Ollama, llama.cpp's server, LM Studio,
vLLM:

```json
"env": {
  "BIFROST_WORKER_BASE_URL": "http://localhost:11434/v1",
  "BIFROST_WORKER_MODEL": "qwen2.5-coder:7b"
}
```

No key is needed when the endpoint is not the default one.

### Worker compatibility

**No local model has been measured yet.** The endpoint is configurable and
the protocol is a plain OpenAI-compatible chat completion, but this
repository does not publish claims it has not measured — and that includes
claims in its own favour.

The instrument exists. Point it at your endpoint:

```bash
BIFROST_TARGET=/path/to/your/codebase \
BIFROST_WORKER_BASE_URL=http://localhost:11434/v1 \
BIFROST_WORKER_MODEL=your-model \
python3 calibratge/calibra.py --cases 9
```

| Worker | Valid JSON | Byte-identical (identity task) | No lines lost | Unfenced | Latency |
|---|---|---|---|---|---|
| DeepSeek (`deepseek-chat`, API) | 9/9 | 3/3 | 3/3 | 9/9 | 2.6 s |
| *your model here* | | | | | |

If you run it, open a PR with the row. Numbers that make a model look bad are
as useful as numbers that make it look good — the table exists to say which
workers this actually works with, not to advertise.

**One thing to expect.** DeepSeek returned zero of nine responses wrapped in
markdown fences. Smaller models fence almost everything, and that is a
parsing problem rather than a capability one. Bifrost already strips fences;
if your model is otherwise sound but still fails on them, report it as a bug
here rather than as a mark against the model.

---

## What leaves the machine

The unit of work sent to a worker is one parsed block — a single method — and
never the file it came from. That is a consequence of the design rather than
a feature added to it: if the replacement code does not pass through the
orchestrator's context, it does not pass through anywhere else either.

**What it does not mean.** The block does leave, in the clear, to whatever
endpoint you configured. So does the instruction, which may itself describe
internal architecture.

**What already guards it.** Heimdall runs *before* the send, not before the
write. Where a secret is a self-contained token it is swapped for a
placeholder, the worker transforms the code around it, and the original goes
back before the file is written — every placeholder must return exactly once
or nothing is written at all. What cannot be safely redacted blocks the send
outright. Measured false-positive rate on a real codebase: 2 findings across
1,291 symbols, both correct refusals of code that *manipulates* keys rather
than holding one. That count is from 0.1.0, when the symbol map held methods
and functions only; classes became addressable in 0.1.4, so a rerun today
counts a larger denominator.

If your constraint is that nothing may leave at all, the answer is a local
worker, not a smaller payload.

### Designed, not built

Two additions would close most of the remaining gap. Neither exists yet, and
they are named here rather than hidden in an issue because the design is the
interesting part:

- **Egress log.** The log records the *size* of what was sent, not the bytes.
  Recording them alongside what came back is nearly free, and it turns "trust
  us" into "audit it".
- **Comment and literal redaction.** Heimdall redacts things shaped like
  secrets. The parser already produces the tree, so comments and string
  literals — often the highest-risk payload and frequently irrelevant to the
  transformation — could be replaced with opaque markers and restored on
  return.

The obvious objection to the second is that quality may suffer when the
worker cannot see the names. That is a measurable question, not an argument:
nine cases with redaction, nine without, `calibratge/calibra.py`. Whichever
way it comes out gets published.

---

## Quick start

Python 3.11+. **No runtime dependencies** — the server runs on the standard
library, and each language is parsed by its own official tooling (`php` as an
external binary, `ast` from the stdlib).

```bash
pipx install mcp-bifrost      # or: uv tool install mcp-bifrost
```

Add it to `.mcp.json` in the project you want to patch:

```json
{
  "mcpServers": {
    "bifrost": {
      "command": "mcp-bifrost",
      "env": { "BIFROST_DB": ".bifrost/history.db" }
    }
  }
}
```

**A tool the client only lists by name is easy to forget mid-task.** MCP
clients that defer tool schemas show Bifrost's tools as bare names until
something asks for them, and nothing about repeating the same mechanical edit
five times in a row prompts that ask on its own — that is exactly how the
first six months of dogfooding went: `insert_case` sat unused for an entire
session while the same switch-case pattern got hand-edited over and over,
which then collided across two parallel edits in exactly the way
`insert_case`'s anchoring exists to prevent. Close the gap once, for this
project:

```bash
mcp-bifrost init-hook            # writes .claude/settings.json in this project
mcp-bifrost init-hook --global   # or: ~/.claude/settings.json, for every project
```

This adds a `PreToolUse` hook on `Edit`/`Write` that reminds the client to
check for a fitting Bifrost tool before hand-editing — additively, so any
hooks you already have stay in place. Run it again any time; it is a no-op
once the hook is there. Commit the project-level `.claude/settings.json` so
the nudge travels with the repo instead of living only on one machine.

A reminder alone turned out not to be enough — a full build session with the
hook installed and firing 50+ times still routed 96% of new files through a
raw `Write` (see docs/critical-review.md, RF-13). A later session settled
it: asked directly, the client admitted it was not using the tools, loaded
them, made one call, then hand-edited a new switch case, two new functions
and two method rewrites anyway, with the reminder firing on every one. Text
that has already been read and agreed with does not change the next
decision.

So the hook *blocks* two cases outright:

- **An `Edit` or `Write` to an existing file this server adapts** (`.php`,
  `.py`) inside a project whose `.mcp.json` registers `bifrost`. The denial
  names the tool for each shape — `insert_case`, `insert_symbol`,
  `fix_symbol`, `fix_symbols`, `fix_range`, `patch_group`.
- **A `Write` creating a file that does not exist yet**, in a directory
  where three or more siblings already share its extension, naming the
  suggested `create_file(model_from=<nearest sibling>)`.

Everything else stays advisory. The first gate reads its scope off disk, so
`--global` is safe: a `.php` or `.py` file in a project that never
configured this server is never blocked. When an edit genuinely has no
symbol to address, `touch .bifrost/hook-override` lets the next one through
— it is consumed on use, so it buys one edit rather than a silent
session-wide opt-out.

#### If you develop this server, install the hook from a `main` worktree

The hook is enforced, global, and — installed editable from your development
checkout — it runs whatever branch that checkout happens to be on. Switching
to a feature branch then silently changes the gate for every other project on
the machine, which is exactly the class of surprise this tool exists to
remove. Keep a second worktree pinned to `main` and point the install at that
instead:

```bash
git worktree add ../MCP-Bifrost-main main
pipx install --force --editable ../MCP-Bifrost-main
```

Development continues in the original checkout, on any branch, with no effect
on the live hook. After merging to `main`, refresh the worktree deliberately:

```bash
git -C ../MCP-Bifrost-main pull       # editable install: no reinstall needed
```

The same applies to any `.mcp.json` that launches the server by path: point
its `PYTHONPATH` at the `main` worktree, not at the checkout you develop in.

Or from source, without installing:

```bash
git clone https://github.com/FixemBCN/MCP-Bifrost.git
cd MCP-Bifrost
python3 -m unittest discover tests    # 308 tests, ~32s
python3 -m mcp_bifrost.server         # same server, PYTHONPATH=.
```

**Without `php` on your PATH you will see `OK (skipped=80)`,** and that is the
expected result: those 80 tests drive the real PHP tokenizer, so on a machine
with no PHP there is nothing for them to prove. The remaining 228 — gates,
patcher, budget, Heimdall, the Python adapter — run on the standard library
alone. Install `php-cli` if you want the PHP half proven on your own machine;
[CI](https://github.com/FixemBCN/MCP-Bifrost/actions/workflows/tests.yml) runs
both environments on every push. `git` guards 91 tests the same way.

**The key does not go in that file.** Put it in `.bifrost.env` at your project
root, which the server reads when the environment does not carry it:

```bash
echo "DEEPSEEK_API_KEY=sk-..." > .bifrost.env
chmod 600 .bifrost.env
echo ".bifrost.env" >> .gitignore
```

Or skip the key entirely and point `BIFROST_WORKER_BASE_URL` at a local
model. Full instructions, and what to do before pointing this at anything
that matters, are in [the manual](https://github.com/FixemBCN/MCP-Bifrost/blob/main/docs/manual.md).

### Tools

| Tool | What it does |
|---|---|
| `fix_symbols` | one instruction across many symbols — **the main one** |
| `fix_symbol` / `fix_range` | rewrite one symbol, or an explicit line range — optional `verify` command, reverted on failure |
| `insert_symbol` / `insert_case` | add a function, method or class, or a branch to a switch router |
| `create_file` | write a new file, optionally by analogy with an existing one — optional `verify` command, deleted on failure |
| `patch_group` | several operations as one transaction |
| `export_docs` / `publish_session` | changelog from the log; batch onto a reviewable branch |
| `revert_patch` / `revert_session` | undo one patch, or the whole batch |

---

## Calibration

Before writing a line of the server, one question had to be answered:

> Given a real method from a real codebase, packed with the compact schema,
> does the worker return code that can be applied without breaking anything?

The harness in `calibratge/` answers it. Zero dependencies — Python stdlib
plus the `php` binary.

```bash
export BIFROST_TARGET=/path/to/your/codebase
python3 calibratge/calibra.py --dry-run    # show cases, no API calls
export DEEPSEEK_API_KEY=...
python3 calibratge/calibra.py --cases 9
```

**Result: the premise holds.** 9/9 valid JSON, 3/3 byte-identical on the
identity task, 3/3 with no original lines lost, 0/9 wrapped in markdown
fences, 2.6 s average latency.

It also caught a byte-offset bug that had nothing to do with the worker and
would have corrupted files silently in production. Full write-up:
[docs/calibration.md](https://github.com/FixemBCN/MCP-Bifrost/blob/main/docs/calibration.md).

---

## Repository layout

| Path | What |
|---|---|
| `mcp_bifrost/` | the server |
| [`CHANGELOG.md`](https://github.com/FixemBCN/MCP-Bifrost/blob/main/CHANGELOG.md) | what changed in each version, and why it was wrong before |
| [`docs/`](https://github.com/FixemBCN/MCP-Bifrost/blob/main/docs/) | manual, architecture, critical review, calibration, comparison, licensing |
| `tests/` | 308 tests — 80 need `php`, 91 need `git`, skipped when absent |
| [`brainstorm/`](https://github.com/FixemBCN/MCP-Bifrost/blob/main/brainstorm/) | the working record — how each decision was reached, including the reversed ones |
| `calibratge/` | the measurement harness |
| [`.github/`](https://github.com/FixemBCN/MCP-Bifrost/blob/main/.github/workflows/tests.yml) | the workflow the badge reports: the suite with `php`, and again without it |

---

## Behind the code

To be completely transparent: **not a single line of this codebase was
written by hand.** It was conceptualised, challenged, implemented, tested and
documented through a human-directed AI process. Here is what that actually
meant, as precisely as it can be stated.

**Human — problem, decisions, direction.** I brought the initial
specification and made every product decision: which worker model, which
languages, what to cut, what to build next, the licence, the naming, when to
stop. Several reversed earlier ones — the licence started as a no-resale
source-available one and ended up Apache-2.0 once I decided reach mattered
more than control. I also decided what the system must refuse to do, which
turned out to be the more consequential half.

**Claude Opus — adversarial design.** Before implementation, Claude reviewed
the specification as an outsider looking for reasons it would fail, and
produced twelve findings. Two killed design elements I had approved: the
central "perimeter check" the spec relied on turned out to be incapable of
failing, and the project's stated justification — token savings — was shown
to be marginal for single edits and only decisive in bulk. Both are
preserved, unedited, in [`brainstorm/`](https://github.com/FixemBCN/MCP-Bifrost/blob/main/brainstorm/).

**Measurement before code.** Rather than trusting the design, a calibration
harness was built first and run against the real worker on real code. It
failed 6 of 9 cases — none of them the worker's fault. The cause was a
byte-offset bug that would have silently corrupted any file containing an
accented character. It also refuted two of Claude's own review findings.
Those corrections sit above the original claims rather than replacing them.

**Claude Opus — the core; delegated models — the periphery.** Claude wrote
the parsing, patching, validation gates, secret handling and engine directly.
Two peripheral modules and the whole of the original test suite were
delegated to smaller models (Haiku and Sonnet) running as subagents. That
suite has since roughly doubled: the tests added in 0.1.2–0.1.5 were written
by Opus and applied through Bifrost itself, and they exist because the
original 128 never executed the MCP server, the worker's HTTP client, the
VCS layer or the Python adapter at all. The split was
deliberate rather than economical: a model starting cold on the patching code
would very plausibly have reintroduced the byte-offset bug, because the
natural way to write that code is the wrong way.

**The delegated models found four real bugs** in code Claude had written,
including one that detached a docblock from the method it documented and one
where nested `switch` statements silently dropped branches. Both passed every
validation gate. Adversarial review by a model with no stake in the code was
the only thing that caught them.

**Human — review and acceptance.** I directed the sequence, inspected
results, challenged claims, and decided what stayed. Claude executed the
validation and calibration runs; I read what came back and decided what it
meant.

### What this process did not provide

No human has read all ~10,700 lines of this repository — roughly 4,600 of
server, 5,400 of tests and 700 of measurement harness — line by line. The
confidence here comes from tests checked against deliberately broken code,
from measurements against a real codebase, and from a design that refuses to
write anything it cannot verify — not from manual audit.

If that is not the kind of confidence you want in a tool that edits your
source files, that is a reasonable position, and the
[responsibility section](https://github.com/FixemBCN/MCP-Bifrost/blob/main/docs/manual.md#responsibility) is specific about
what the gates do and do not catch.

### Why this is in the README

Bifrost is a demonstration of its own premise. The valuable human
contribution was not typing the code: it was defining the problem,
controlling the context, challenging the output, and insisting on enough
validation that generated code could be trusted at all.

The repository deliberately keeps the reasoning, the rejected ideas, the
adversarial review and the measurements — including the parts where the AI
was wrong and said so.

**This account stops at the first release, and the work did not.** What has
happened since is in [`CHANGELOG.md`](https://github.com/FixemBCN/MCP-Bifrost/blob/main/CHANGELOG.md),
which records each defect as it was found and what it had been doing
unnoticed; how the design was arrived at before that is in
[`brainstorm/`](https://github.com/FixemBCN/MCP-Bifrost/blob/main/brainstorm/)
and [`docs/critical-review.md`](https://github.com/FixemBCN/MCP-Bifrost/blob/main/docs/critical-review.md).

---

## Documentation

| Document | What it is |
|---|---|
| [Manual](https://github.com/FixemBCN/MCP-Bifrost/blob/main/docs/manual.md) | what it is, what it can do, how to install it, and what you are responsible for |
| [Architecture](https://github.com/FixemBCN/MCP-Bifrost/blob/main/docs/architecture.md) | what gets built and why |
| [Critical review](https://github.com/FixemBCN/MCP-Bifrost/blob/main/docs/critical-review.md) | a fresh-eyes pass hunting for reasons this fails — twelve findings, two later refuted by measurement |
| [Calibration results](https://github.com/FixemBCN/MCP-Bifrost/blob/main/docs/calibration.md) | what the worker actually did when asked |
| [Comparison](https://github.com/FixemBCN/MCP-Bifrost/blob/main/docs/comparison.md) | how this sits next to Aider, Serena and fast-apply — and where they win |
| [Licensing](https://github.com/FixemBCN/MCP-Bifrost/blob/main/docs/licensing.md) | what we consume, what we grant |

[`brainstorm/`](https://github.com/FixemBCN/MCP-Bifrost/blob/main/brainstorm/) holds the working record: the original spec, the
design journal across five revisions, the adversarial review, and the
calibration results. `docs/` is the reference and wins where the two differ.

---

## Responsibility

This tool edits your source files automatically using a language model.
Apache 2.0 means it is provided **as is, without warranty**: you are
responsible for what it does to your code. Read the diffs, run your tests,
deploy on purpose. The [manual](https://github.com/FixemBCN/MCP-Bifrost/blob/main/docs/manual.md#responsibility) is specific
about what the gates do and do not catch.

## Contributing

Contributions of every kind are welcome — including an argument that
something here is wrong. This project has already deleted one validation gate
for being tautological and refuted two of its own claims with measurement.

One convention, and it is the one that matters: **every test must be able to
fail.** Details in the [manual](https://github.com/FixemBCN/MCP-Bifrost/blob/main/docs/manual.md#contributing).

## License

[Apache License 2.0](https://github.com/FixemBCN/MCP-Bifrost/blob/main/LICENSE).

Built on the [Model Context Protocol](https://modelcontextprotocol.io),
MIT-licensed by Anthropic, PBC. MCP-Bifrost is an independent project and is
not affiliated with, endorsed by, or sponsored by Anthropic, PBC.
