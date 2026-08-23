# MCP-Bifrost — Project plan (living)

**Status: v0.5 — checked against real data. The calibration ran
against DeepSeek and produced 7 changes, already incorporated.**
Input spec: [`original-spec.md`](./original-spec.md) (not touched).
📋 [`critical-review.md`](./critical-review.md) — 12 findings with
fresh eyes. **Incorporated.**
📊 [`calibration-results.md`](./calibration-results.md) —
calibration run against DeepSeek: premise validated, byte-offset bug
uncovered, RF-5 refuted and RF-10 softened. **Incorporated.**
This document is what we iterate on until it's final.

---

## 1. What it is, in one sentence

An MCP server that receives from Claude Code code jobs already
**analyzed and chunked**, surgically extracts the exact rangee via a
parser, delegates the writing to DeepSeek, validates and applies the
patch atomically to disk, and logs the entire execution outside
Claude's context.

**Confirmed division of roles:**
- **You** → talk to Claude in natural language.
- **Claude Code** → analyzes *what* needs to be done and *how*, decides
  the strategy, chunks the work into isolated units and sends them to
  the MCP. Also verifies the result (tests, lint, coherence).
- **MCP-Bifrost** → parses, packages, calls DeepSeek, validates,
  applies, logs.
- **DeepSeek** → writes the code for one isolated unit. Decides
  nothing.

DeepSeek is the **muscle**; Claude is the **head**. The MCP is the
nerve that connects them and the one that guarantees nothing reaches
disk broken.

---

## 2. Closed decisions

| Topic | Decision |
|---|---|
| Worker | **DeepSeek**, confirmed. There are credits and real volume of work. |
| MVP languages | **Python + PHP both from the start.** Java is out of scope. |
| `export_docs` | **Out of scope for now.** But every execution is logged. |
| Log format | **SQLite** for the operational log (permanent) + **derived JSONL** to measure effectiveness (temporary). A single write (§5). |
| Rollback | **Tied to git**, always (see §4.2). |
| User interface | Claude Code. There is no user CLI in the MVP. |

---

## 2.5 Reconnaissance of the terrain — the target codebase

Confirmed real target: **the PHP code of the target codebase**.
Measured, not assumed:

| Metric | Value |
|---|---|
| Own PHP files (excluding `vendor/`, `OLD_*`, `_deploy`) | **131** |
| Lines of own PHP | **~34,900** |
| Largest file | `src/app/AdminService.php` — 4,652 lines |
| Second | `src/api/router.php` — 3,320 lines |
| Methods in `AdminService.php` | **202**, a single class |
| `case`s in the switch of `src/api/router.php` | **199** |
| Sibling `Calc` files | **46**, same shape |
| Files with mixed HTML | **1** (`DocumentTemplate.php`) |

### What this reveals

**🟢 The PHP/HTML risk feared in v0.2 is marginal.** Only one file
mixes HTML. The rest is pure PHP with well-formed classes and
functions. The §6 concern about having to depend heavily on
`fix_rangee` in PHP is ruled out.

**🟢 `AdminService.php` is the perfect use case.** 202 methods with
the uniform pattern `public function name(...)`, inside a single
class. Addressing by symbol will work almost always. And the payoff
is brutal: sending a method of ~30 lines instead of a 4,652-line file
is a **~99% reduction**, above the spec's 85-95% target.

**🟡 The switch in `src/api/router.php` needs its own addressing.** 199
`case`s that are the extension point of the entire API, and that are
not PHP symbols. They need to be handled explicitly (§4.4).

**🟡 46 sibling `Calc` files = a goldmine for generation.** Having 46
implementations of the same shape turns "generate a new `Calc`" into
"copy this one" (§4.4, solution 2).

**🔴 There are credentials inside the repo tree.** The credentials
directory of the target codebase contains service and user files that
**are deliberately synced** with the repo. The secret-detection gate
(§4.5) stops being a theoretical precaution: there is sensitive
material in the very tree the tool will pass through. **Moves up to
Phase 1.**

*(The specific file names have been removed from this document:
MCP-Bifrost is meant to be public and must not publish the credential
map of a production application.)*

**🔴 `AdminService.php` with 4,652 lines and 202 methods in one class
is a concurrency risk.** If Claude chunks a job into 15 units and 12
of them touch this same file, strict per-file serialization is needed
(§4.6) and the stale-rangee guard (§4.3) becomes critical there, not
optional.

---

## 3. General verdict

The idea is solid and the pattern (orchestrator-worker / model
cascade) is real and proven. With the roles clarified, the biggest
risk from v0.1 — the worker operating blind — is greatly reduced: **if
Claude is the one chunking, the quality of the chunk is Claude's
responsibility**, and that is exactly where this decision belongs.

What remains to be solved is not the concept, but **the guarantees**:
that nothing reaches disk broken, that everything is reversible, and
that on a huge project the system doesn't turn into a machine for
generating silent regressions at scale.

---

## 4. Solutions to the gaps in the spec

### 4.0 — Byte rule (validated with data, 2026-08-23)

**The entire path extraction → payload → splice → write operates in
`bytes`.** It is decoded to text only to send it to the worker, and
re-encoded on receiving it.

This is not a style preference. PHP's `token_get_all()` returns
**byte offsets**; Python's `str` is indexed **by character**. Mixing
the two shifts the cut by as many characters as there are multibyte
bytes before the symbol:

```
ShiftCalc.php:  4,506 bytes / 4,504 characters    <- 2 accented chars

slice on str    → 'blic function closeShift($shiftId) {'
slice on bytes  → 'public function closeShift($shiftId) {'
```

The calibration failed **6 of 9 cases** for this reason before it was
detected. The target codebase has Catalan comments everywhere: this
is not an edge case, it's the normal case. And it fails **silently**
— it clips the start of the block, adds bytes from the end, and the
result still looks like code.

**Any `read_text()` on this path is a bug waiting for a file with
accented characters.** Details in
[`calibration-results.md`](./calibration-results.md).

### 4.1 — Validation gates before writing (resolves §3.1 of v0.1)

**Thoroughly revised.** The previous version had three gates, and
[the critical review](./critical-review.md) (RF-1) showed that two of
them could never fail. The calibration confirmed this with data: on
the first batch, the perimeter gate scored **9/9 while 3 files ended
up syntactically broken**.

No byte touches disk without passing these gates, in this order:

**Gate 0 — offsets (the critical one).**
```
fitxer[start:end] == the_src_we_sent     # en bytes
```
Checked by re-reading the file right before writing. It detects that
the file has changed since extraction (an earlier patch in the batch,
another process, the user's editor) and any boundary-calculation
error.

This is **the gate that prevents the worst damage**: splicing with
wrong offsets injects the block into the middle of another method and
destroys code. It's the same check as §4.3, and the calibration
validated its value — it would have caught the 9 corruptions in the
first batch.

**Gate 1 — syntax.** The whole file is rebuilt in memory and
validated:
- PHP → `php -l` on a temp file.
- Python → `ast.parse()` (stdlib).

*Known limit:* it only validates the shape. A
`$this->nonExistentMethod()` sails through with no problem (RF-12).
Cheap mitigation for later: check that every `$this->x(` and
`Classe::x(` in the new block exists in the map that `extract.php`
already generates.

**Gate 2 — a single symbol.** The returned block must contain
**exactly one** symbol definition. This prevents the worker from
adding a sibling function on its own initiative. Two lines with
`extract.php`.

*Note:* this is what remains of the old "structural integrity gate".
The version that compared the symbols of **the whole file** has been
removed: with per-symbol extraction the worker never sees neighboring
code and cannot delete it.

**Gate 3 — substance** (before heavy use, not on day 1).
Compare the set of calls, variables and control keywords of the block
before and after. If the new block has lost any that the instruction
didn't ask to remove → reject, with the list of what disappeared.

It targets the real risk in RF-2: code that compiles but has lost
logic from inside the method. **The calibration didn't see it show
up** (3/3 on additive tasks with no line lost), which is why it drops
in priority — but 9 cases of 5-28 lines don't rule it out, and the
risk grows with block size.

**Removed — the perimeter gate.** Comparing the bytes outside the
rangee can never fail: the MCP builds the file as
`original[:start] + block + original[end:]`, and the perimeter is
identical **by construction**. The test that checked it was also
removed: a test that can't fail is worse than no test, because it
gives a positive signal without checking anything.

If any gate fails → structured `ERROR` to Claude with the reason, no
write, and the failed attempt is logged with **which gate** rejected
it.

**Cost:** milliseconds.

### 4.2 — Rollback via the git object store (resolves §3.2, leveraging that git is already there)

We take advantage of git already being a content-addressed database.
No need to duplicate snapshots.

**Before each patch:**
```
git hash-object -w <fitxer>   →  blob_sha
```
This writes the original content to `.git/objects` and returns its
SHA. We store `blob_sha` in the log. **Extra disk cost: effectively
zero** — git already compresses and dedupes; if the content already
existed (because it's committed), nothing new is written.

**To revert:**
```
git cat-file blob <blob_sha>  →  original content  →  rewrite file
```

**Advantages over a manual snapshot:**
- Works even if the working tree is dirty (no discipline required).
- Dedup and compression for free.
- Survives across sessions and is auditable with standard git tools.
- Zero custom format to maintain.

**Caveat to document:** these blobs are *unreachable* until they are
committed, and an aggressive `git gc --prune` could remove them.
Mitigation: create a ref (`refs/bifrost/<id>`) to anchor them, or
simply don't run `gc --prune=now` (the default expiry is 2 weeks,
well above the useful window for a rollback).

**Rollback tools:**
- `revert_patch(patch_id)` — restores one specific patch.
- `revert_session()` — restores all patches from the current session,
  in reverse order. Essential when Claude has chunked a job into 20
  units and the 17th went wrong: you don't want to undo one, you want
  to undo all of them.

**GitHub integration:** the MVP doesn't push or create branches
automatically. But the log stores the `HEAD` sha at the moment of
each patch, which allows grouping them by commit later and, in a
later phase, generating branches or PRs per batch of changes without
reconstructing anything.

### 4.3 — Stale-rangee guard (resolves §3.5)

When Claude sends 20 units against the same file, the lines shift
under its feet.

**Solution:** each patch records the `sha256` of the block it expects
to find at the given rangee. Right before writing, the MCP re-reads
the file and recomputes it. If it doesn't match → `ERROR: stale
rangee` with the current content of the rangee, and Claude re-resolves.

**Strong improvement:** add `fix_symbol` **from the MVP**, not as
phase 2. Addressing by symbol (`function_name`) instead of by line is
immune to line shifting. With Claude chunking work into large batches
against the same file, `fix_symbol` isn't a luxury — it's the primary
mode of use. `fix_rangee` remains as an escape hatch for code that
doesn't live inside a symbol (module-level configuration, HTML blocks
inside PHP, etc.).

### 4.4 — Generating new code (the gap in the original spec)

The spec only covers **replacing** existing code. You said "fix
**and generate**". With the terrain now mapped (§2.5), this gap has a
much more concrete solution than it seemed.

#### The observation that changes everything

In the target codebase, **generating new code is almost never writing
into a blank page**. It's replicating a pattern that already exists
46 times, at a known structural anchor point. Adding a new entity to
the system almost always means the same sequence:

```
1. src/calc/NouCalc.php        ← new file, sibling of 46 identical ones
2. src/app/AdminService.php        ← require_once + 4-6 CRUD methods
3. src/api/router.php            ← 4-6 new switch cases + functions
4. public/js/…                     ← frontend
```

This is not "free generation". It's **anchored insertion** and
**replication by analogy**. Two tractable problems, not one fuzzy
one.

#### Solution 1 — Anchored insertion

`insert_symbol(file_path, anchor_symbol, position, instruction)`

- `anchor_symbol`: an existing symbol (`updateClient`).
- `position`: `before` | `after` | `end_of_class` | `end_of_file`.

Anchoring to a symbol instead of a line number is what makes this
work in a 4,652-line file that Claude is modifying in batch: the
anchor doesn't move even if everything above it changes.

`insert_case(file_path, switch_anchor, after_case, instruction)`

A specific but essential case: `src/api/router.php` has a `switch`
with **199 `case`s**. It's the main extension pattern of the whole
API and it isn't a PHP symbol — no parser will hand it to you as one.
It's solved as a block delimited between two sibling `case`s, which
tree-sitter can indeed identify.

Without this, every new endpoint forces a `fix_rangee` on a
3,320-line file, which is exactly the fragile case we want to avoid.

#### Solution 2 — Generation by analogy (`create_file`)

`create_file(file_path, model_from, instruction)`

Instead of describing from scratch what `ExtraWarrantyCalc.php`
should contain, you pass it `WarrantyCalc.php` as a **structural
exemplar** and a difference instruction. DeepSeek doesn't invent
architecture: it copies it.

Why this is the strongest piece of this design:
- **Low token cost**: a Calc file runs about 100-200 lines. A single
  exemplar is all the context needed.
- **Very high consistency**: the new code comes out identical in
  style, naming conventions, error handling and structure to the 46
  siblings. This is precisely where a cheap, low-context worker tends
  to fail, and here it can't.
- **Verifiable**: the set of methods in the generated file can be
  compared against the exemplar and deviations flagged.

This technique turns the worker's weakness (it knows nothing about
the project) into irrelevant: it doesn't need to know it, it needs to
copy well.

#### Solution 3 — Validation gates adapted to insertion

The perimeter gate from §4.1 assumes replacement: comparing
everything outside the rangee. In an insertion the offsets shift and
the check has to be split in two:

- **Split perimeter**: everything *before* the insertion point must
  be byte-identical, and everything *after* it too. The only
  permitted difference is content added at the exact point. Still a
  hard guarantee, and still cheap.
- **Structural gate**: the set of pre-existing symbols must remain
  **intact**, plus the N newly declared ones. If the insertion has
  "eaten" the neighboring method — the most typical error when a
  model rewrites a region — it is caught here.
- **Syntax gate**: same as before (`php -l`).

For `create_file`: there is no perimeter, but there is syntax, plus
its own added gate: **the file must not already exist**. Never
overwrite via `create_file`.

#### Solution 4 — Multi-file transactions (the hole inside the hole)

This is the real problem the list above uncovers: **generating a new
feature in the target codebase touches 3-4 files at once, and doing
it halfway makes no sense.** A `NouCalc.php` created without the
`require_once` in `AdminService.php` leaves the project worse off
than before starting.

Proposal: `patch_group`.

- Claude opens a group, sends N operations to it (patches,
  insertions, creations), and closes it.
- The MCP validates **all** of them before applying any.
- Either all are applied, or none. If the 3rd of 4 fails a gate, the
  first two are automatically reverted.
- We already get the rollback for free: the git blobs from §4.2,
  reverted in reverse order. Created files are deleted.

Without this, the tool is reliable at the file level and fragile at
the feature level — which is the level you actually work at.

#### Recommended order

`insert_symbol` and `create_file` are the ones that give immediate
value on the target codebase and are the simplest. `insert_case` is
tied to the API and is specific but high-value. `patch_group` is what
makes all of this safe for real work, and should land before it's
used heavily, not after.

### 4.4b — Indentation normalization in the payload (validated with data)

**The worker must never guess indentation.** The MCP strips it before
sending and puts it back on receiving:

```
extracted     "    public function foo() {\n        return 1;\n    }"
   ↓  normalise (indent="    ")
sent     "public function foo() {\n    return 1;\n}"
   ↓  worker
received      "public function foo() {\n    return 2;\n}"
   ↓  re-indent with indent
applied    "    public function foo() {\n        return 2;\n    }"
```

The payload carries a **mandatory from day 1** `indent` field, even
if a given implementation doesn't use it.

**Why this isn't a theoretical precaution:** in the calibration, 1 of
9 cases returned the block with indentation different from the
original. Everything else was correct — it compiled, the signature
was intact, no line lost — but the formatting had shifted. Across
35,000 lines, accumulated cosmetic drift is debt.

And it's **the same problem §11 anticipated for Python**, showing up
first in PHP. There it's worse: Python's indentation is semantic, and
a badly re-indented block isn't ugly, it's broken code. Solving this
now at the payload level closes both cases at once.

### 4.5 — Secret detection before sending (resolves §3.4)

With DeepSeek confirmed and a huge production project, this moves
from "suggestion" to **mandatory**. Scanning the `src` and `ctx`
block before the call, looking for patterns for: API keys, bearer
tokens, connection strings with credentials, private keys, passwords
in literals.

If detected → `ERROR: secret detected`, nothing is sent, and Claude
is warned. False positive? It can be forced with an explicit per-call
flag. The key is that the default behavior is **not to leak**, and
that skipping it is a conscious, logged decision.

### 4.6 — Cost and concurrency limits (resolves §4.5 of v0.1)

With Claude automatically chunking work on a huge project, the
volume of calls can spike without anyone noticing.

- Call and token counter per session, with a configurable hard cutoff.
- **Controlled parallelism:** if Claude sends a batch of N independent
  units, the MCP can process them in parallel (semaphore, e.g. 4-6
  concurrent).

  **Serialize the write, not the call** (RF-8). Calls to the worker
  are independent — each symbol is processed in isolation — and only
  the write to disk needs ordering. Serializing per file, as the
  previous version said, never actually kicked in where it mattered:
  the main target is `AdminService.php`, a single file with 200
  symbols, and a batch of 30 fixes would have gone through it
  entirely in series.

  **Measured latency: 2.6s average per call** (calibration,
  2026-08-23). 30 calls in series ≈ 78s; in parallel of 6, ≈ 13s. The
  per-call latency isn't a problem — it's comparable to a Claude
  `Edit` — but multiplied across a batch, it is.

---

## 5. Log — two purposes, two lifespans, **one single write**

The split is correct, because these are two things with **different
lifespans**:

| | Operational log | Effectiveness log |
|---|---|---|
| Purpose | know what was done, and be able to undo it | prove whether the project is worth it |
| Lifespan | permanent, grows forever | **temporary — deleted once the premise is proven or refuted** |
| How it's read | point queries (by file, by date) | all at once |
| Format | **SQLite** `.bifrost/history.db` | **JSONL** `.bifrost/eficacia.jsonl` |

That the second one has an expiry date is precisely what justifies
keeping it separate. A measuring instrument shouldn't live inside the
system it measures.

### The important nuance: the JSONL is **derived**, not written in parallel

Writing both files on every operation looks harmless, and it isn't.
If the process dies between the two writes — or one fails and the
other doesn't — the two logs diverge, and **there's no way to know
which of the two is right**. It's the classic failure mode of having
two sources of truth for the same data, and it shows up exactly when
the log is needed most: when something has gone wrong.

Rule: **SQLite is the only write.** The effectiveness JSONL is an
**export** generated on demand from SQLite. Cost: one query. Gain:
divergence impossible by construction.

```
operation -> SQLite (single, transactional write)
                │
                └── export --> eficacia.jsonl ──> registre.py
```

This doesn't take away anything you wanted: you still get the
lightweight, readable, separate JSONL to measure savings. Only
**where it comes from** changes.

### Operational schema (SQLite)

```sql
CREATE TABLE patches (
  id           TEXT PRIMARY KEY,   -- uuid4
  ts           TEXT NOT NULL,      -- ISO-8601 UTC
  session      TEXT,               -- groups a batch
  grup         TEXT,               -- patch_group, if any (§4.4)
  op           TEXT NOT NULL,      -- fix_symbol | fix_rangee | create_file…
  fitxer       TEXT NOT NULL,
  simbol       TEXT,
  start_byte   INTEGER,
  end_byte     INTEGER,
  estat        TEXT NOT NULL,      -- ok | rejected | error
  porta        TEXT,               -- which gate rejected it, if any
  blob_abans   TEXT,               -- git hash-object -> rollback (§4.2)
  head_sha     TEXT,               -- repo HEAD at the time
  instruccio   TEXT,               -- what was asked of it
  rationale    TEXT,               -- the worker's `why`
  src_b        INTEGER,            -- --- efficacy measurements ---
  out_b        INTEGER,
  in_b         INTEGER,
  resp_b       INTEGER,
  tin          INTEGER,
  tout         INTEGER,
  cache_hit    INTEGER,            -- tokens servits de cache (43% mesurat)
  ms           INTEGER
);
CREATE INDEX idx_fitxer  ON patches(fitxer);
CREATE INDEX idx_ts      ON patches(ts);
CREATE INDEX idx_session ON patches(session);
CREATE INDEX idx_estat   ON patches(estat);
```

**Everything** is logged, including rejected attempts and which gate
rejected them (§9, question 3). This is where the information to
calibrate the system lives.

`PRAGMA journal_mode=WAL` — safe concurrent writes when the batch
runs in parallel (RF-8).

### Effectiveness log (derived JSONL)

Only the measurement columns. No free text: neither `instruccio` nor
`rationale`, which is what would make the file grow and isn't needed
for counting.

```json
{"ts":"2026-08-23T14:02:11Z","op":"fix_symbol","f":"src/app/AdminService.php",
 "sym":"AdminService::updateRecord","ok":true,"src_b":812,"out_b":905,
 "in_b":142,"resp_b":58,"tin":298,"tout":241,"ms":6210}
```

~150 bytes per job. 10,000 jobs ≈ 1.5 MB.

### Principle that stands: measurements, not conclusions

Nowhere is `estalvi: 1850` stored. The bytes are stored, and the
savings are derived on read. When the formula turns out to be
optimistic — and it will be, RF-4 — it gets corrected without
invalidating anything already logged.

```
cost_si_ho_fes_claude  ≈ (src_b + out_b) / 3.5
cost_real_per_claude   ≈ (in_b + resp_b) / 3.5
estalvi                = cost_si_ho_fes_claude - cost_real_per_claude
```

**Upper bound**, and labeled as such: if Claude had already read the
block to formulate the instruction, `src_b` was already paid for.

### When the JSONL gets deleted

When the question it exists to answer gets answered: *does the
accumulated savings justify the latency and the risk?* If the answer
is yes, the number no longer needs watching. If it's no, the project
needs rethinking and it doesn't need watching either. Either way,
gone.

---

## 6. Parsers — decision revised with data

**v0.4 said tree-sitter for extraction. Dropped for PHP.**

Reason: there's a better option we already had in front of us.
`token_get_all()` is PHP's **official** tokenizer — the same one the
interpreter uses — and the `php` binary is already a required
dependency for validation (`php -l`).

| | tree-sitter-php | `token_get_all()` |
|---|---|---|
| Dependencies | Python package + grammar | **none** (the `php` binary is already there) |
| Fidelity | third-party grammar | **the official lexer** |
| Installs on this machine | ❌ no `pip` | ✅ works |
| Tested against the target codebase | — | **128/128 files, 0 failures** |

`calibratge/extract.php` already implements this: it returns name,
class, FQN, byte offsets, lines, indentation and the start of the
preceding docblock. It includes a sanity check that concatenating the
tokens reconstructs the file byte for byte — if it doesn't match, it
aborts instead of returning dubious offsets.

**Validation:** native to each language. `php -l` for PHP,
`ast.parse()` for Python. Here fidelity matters more than uniformity:
the official parser is the only authority on whether a file is valid.

**For Python** (§11), when it arrives: `ast` for both things. Also
stdlib, also official, and with exact `lineno`/`end_lineno`. The
symmetry holds — **each language is analyzed with its own official
tools** — which turns out to be more coherent than forcing a common
third-party grammar.

**What this doesn't change:** the `LanguageAdapter` abstraction is
still necessary (§11). What varies between languages now is the
adapter's *implementation*, not its shape.

**Note on PHP mixed with HTML:** measured, only 1 of 131 files does
this (§2.5). `token_get_all()` tokenizes it correctly anyway
(`T_INLINE_HTML`), so it doesn't even need to be treated as a special
case.

---

## 7. Revised blueprint — PHP first

Reordered around the confirmed real target. **Python is no longer the
starting point**: it was the cheapest route to implement, but it
isn't where the work is. It stays in the design (the parser
abstraction accounts for it) but gets implemented later.

**Phase 0 — The loop, complete and safe, on PHP**
- Tools: `fix_symbol`, `fix_rangee`.
- **The entire data path in `bytes`** (§4.0). Non-negotiable.
- Extraction with **`extract.php` / `token_get_all()`**, not
  tree-sitter (§6 — decision revised with data). Validation with
  `php -l`.
- DeepSeek worker with the spec's synthetic schema, **plus the
  `indent` field** (§4.4b).
- Gates 0, 1 and 2 (§4.1). Gate 3 (substance) is left for Phase 1.
- SQLite log + prior git blob (§4.2, §5).
- **Real test bench**: `src/calc/*.php` (small, uniform, low-risk
  files) before touching `AdminService.php`. Already used in the
  calibration.
- **Exit criterion:** 10 chained fixes against `AdminService.php`
  with not a single corruption, with `offsets_ok` verified at every
  step.

**Phase 1 — Hardening (moved up from Phase 4)**
- Secret detection before sending (§4.5). **Not optional**: there are
  credentials synced inside the repo tree (§2.5).
- **Substance gate** (§4.1, gate 3). It didn't show up in the
  calibration, but it must exist before heavy use and before large
  blocks.
- Serialization of the **write** (not the call) and cost limits
  (§4.6).
- `revert_session()`.
- **Smoke test on the target codebase** (RF-3) — **blocking** for any
  heavy use against `Calc/`, `App/` or `api/`. It's the only semantic
  safety net there will be, and right now it doesn't exist.

**Phase 2 — Generation on PHP**
- `insert_symbol` and `create_file` with generation by analogy
  (§4.4).
- `patch_group` — multi-file transactions. Must land **before** heavy
  use, not after.
- First real test: generate a whole new `Calc` with its wiring.

**Phase 3 — The API**
- `insert_case` for the 199-case switch in `src/api/router.php`.

**Phase 4 — Scale and Python**
- Batch parallelism (§4.6).
- Python support (`ast` to validate, tree-sitter to extract).

**Out of scope (for now)**
- `export_docs` and CHANGELOG generation.
- Java.
- Preview/confirmation of large diffs (Claude is already in the loop
  and can ask for the diff if it wants it).
- Automating branches/PRs on GitHub.

---

## 8. Assumptions — updated verdict

| Assumption | Verdict |
|---|---|
| Reducing Claude's context tokens brings real value | ✅ Confirmed, and more so with a huge project: the bottleneck is context, not intelligence. |
| Claude is good at chunking the work | ✅ Assumable — it's exactly the task it's strong at. But the MCP needs to return **actionable** errors, not an opaque `ERROR`. |
| `ctx` with signatures is enough for the worker | ⚠️ Depends on the piece. With Claude deciding the `ctx`, the control is there; the schema needs to let it send both signatures and whole blocks when needed. |
| DeepSeek as worker | ✅ **Validated with data**: 9/9 valid JSON, 3/3 byte-for-byte identity, 0/9 markdown fences, 2.6s average. Residual risk = privacy (§4.5). |
| A single schema serves both Python and PHP | ✅ Better than expected: only 1 of 131 PHP files mixes HTML (§2.5). The real fragile point isn't PHP, it's indentation — and the calibration already saw it fail **in PHP** (§4.4b). |
| 100% perimeter preservation | ⚠️ **Rethought.** It's free by construction and verifying it proves nothing (RF-1, confirmed in the calibration). What needs verifying is the **offsets** (§4.1, gate 0). |

---

## 9. Questions — resolved and pending

**1. First real target** — ✅ **Resolved.** The PHP code of the
target codebase. Consequence: PHP moves to Phase 0, secret detection
moves up to Phase 1, Python drops to Phase 4 (§11).

**2. Tolerance for worker errors** — pending, with recommendation.
Recommendation: **1 automatic retry**, and only when the error is a
syntax or structural gate error (these are errors the worker can fix
if told exactly what it broke). Perimeter errors → no retry, straight
to Claude: it means the worker has left its lane and insisting is
worse. With DeepSeek credits available, the cost of a retry is
negligible compared to the context cost of bringing Claude back into
the loop.

**3. Log granularity** — pending, with recommendation.
Recommendation: **log everything**, including failed attempts and
which gate rejected them. It's the only way to know, after 200
patches, whether the system is failing because of poor prompts,
insufficient context, or worker limits. Disk cost: irrelevant with
SQLite.

**4. Testing budget** — ✅ **Resolved.** There are DeepSeek credits
available for this project, and they can be spent on unstructured
testing. Direct consequence for the design:

- **Phase 0 is validated against the real API, not against mocks.**
  The biggest risk in this project isn't the MCP's code, it's *what
  the worker actually returns* when facing a method from
  `AdminService.php`. This isn't discovered with a mock.
- A **calibration test before writing the server** is worthwhile:
  take 5-10 real methods from `Calc/*.php`, send them by hand with
  the proposed synthetic schema, and measure hit rate, style
  fidelity, and whether the output JSON is reliable. An afternoon of
  work that can invalidate or confirm the whole premise of the
  project for a cost of cents.
- DeepSeek can also be used as a worker **while building MCP-Bifrost
  itself**, not just afterward.

---

## 10. Iteration history

- **2026-08-23** — v0.1: initial review of the spec. Alerts and MVP
  blueprint.
- **2026-08-23** — v0.2: closed decisions (DeepSeek confirmed,
  Python+PHP, `export_docs` out of scope, SQLite log, rollback via
  git object store). Concrete solutions to the gaps. New gap
  detected: code generation not covered by the original spec.
  `fix_symbol` moves up to the MVP.
- **2026-08-23** — v0.3: real target = PHP of the target codebase.
  Terrain measured (§2.5). Solutions to the generation gap: anchored
  insertion, generation by analogy, adapted gates and `patch_group`
  (§4.4).
- **2026-08-23** — v0.4: blueprint reordered to PHP-first (§7).
  Questions 1 and 4 resolved; DeepSeek credits available → Phase 0 is
  validated against the real API and a prior calibration test is
  added. Analyzed the cost of adding Python (§11).

---

## 11. What adding Python to the mix would take

Short answer: **little, if the design is done right from the start;
a lot, if it's improvised afterward.** The cost isn't the Python
parser — it's not having left the gap prepared for.

### What it actually costs

| Piece | Cost of adding Python | Comment |
|---|---|---|
| Symbol extraction | **Low** | `tree-sitter-python` is one more grammar. If the extractor already speaks tree-sitter for PHP, it's a grammar swap and a node-type map. |
| Syntax validation | **Very low** | `ast.parse()` is stdlib. Cheaper and more reliable than `php -l`, which needs a subprocess. |
| Perimeter and structural gates | **Zero** | They operate on bytes and on lists of symbols. Language-agnostic by construction. |
| Log, git rollback, `patch_group` | **Zero** | They don't even know which language they're touching. |
| Worker prompt | **Low-medium** | Needs a variant per language (conventions, what counts as a signature, how context is declared). Little text, but its own calibration. |
| Secret detection | **Zero** | Patterns over text. |

### Where there's actually real work

**1. Python's indentation is semantic.** This is the only serious
conceptual difference. In PHP you can insert a method with the brace
in place and it works; in Python, if the block returned by the worker
comes with 0 spaces of indentation and has to be fitted inside a
class, it needs **re-indenting** before insertion. And if the worker
mixes tabs and spaces, it breaks the file.

Mitigation: normalize the extracted block's indentation to 0 before
sending it, and reapply the original level on receiving it. That way
the worker always sees top-level code and never has to guess the
indentation context.

✅ **Already solved** — see §4.4b. The calibration saw the worker
fail indentation **already in PHP** (1 of 9 cases), so the
normalization goes into Phase 0, not as prep work for Python. When
Python arrives, this piece will already be there, which is exactly
what this section was asking for.

**2. `insert_symbol` with `position: after`** needs to know where a
Python symbol truly ends. In PHP the closing brace marks it
unambiguously; in Python the end of a function is "the first line
with lower or equal indentation", and trailing comments and blank
lines are disputed territory. Tree-sitter resolves this, but it must
be decided explicitly whether trailing comments belong to the symbol
or to the next one.

**3. `create_file` by analogy works just as well**, but Python has
conventions PHP doesn't (docstrings, `__init__`, type hints). The
per-language prompt has to account for it.

### Estimated cost

If Phases 0-3 are built with the language abstraction as a real
interface (`LanguageAdapter` with `extract_symbol`, `validate`,
`list_symbols`, `normalize_indent`), adding Python is **a new
adapter and a prompt variant**. Small.

If instead PHP gets coded directly into the tools' logic ("we'll
generalize it later, no big deal"), adding Python means refactoring
the whole core, with the system already in use on production code.
Expensive and risky.

### Recommendation

**Define `LanguageAdapter` in Phase 0, with a single implementation
(PHP).** Don't implement Python yet, but don't make any decision that
would block it: keep the `indent` field in the payload from day one
(§4.4b — turns out PHP does use it), always work in `bytes` (§4.0 —
in Python `ast` gives character offsets, and mixing them would be the
same bug in reverse), and don't let any tool assume closing braces.

With this, when Python is needed, it's a day of work. Without it,
it's a rewrite.
- **2026-08-23** — v0.5: calibration run against DeepSeek. Premise
  validated (3/3 byte-for-byte identity). Uncovered a bug of **byte
  offsets vs. character indices** that made 6 of 9 cases fail → new
  §4.0. Validation gates rewritten (§4.1): perimeter gate removed,
  offsets gate added as critical. New §4.4b (indentation
  normalization). Extraction switched from tree-sitter to
  `token_get_all()` (§6). RF-5 refuted (real latency 2.6s), RF-10
  softened (43% cache).
