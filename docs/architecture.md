# Architecture

**Status: Phase 0 built and passing its exit criterion.**

This is the English reference. The design was argued out in Catalan in
`brainstorm/`, kept as the working record; where the two differ, this
document wins.

---

## 1. What it is

An MCP server that receives code work **already analysed and split up** by
an orchestrating model, extracts the exact target block with a real parser,
delegates the writing to a cheaper worker model, validates the result,
applies it atomically, and logs the whole execution outside the
orchestrator's context.

**Roles:**

| Actor | Decides |
|---|---|
| **You** | what you want, in natural language |
| **Orchestrator** (Claude Code) | *what* to do and *how*; splits the work into isolated units; verifies the outcome |
| **MCP-Bifrost** | parses, packs, calls the worker, validates, applies, records |
| **Worker** (DeepSeek) | writes the code for one isolated unit. Decides nothing. |

The worker is the muscle, the orchestrator is the head, and the server is
the nerve between them — plus the only thing standing between a bad
generation and a corrupted file.

---

## 2. Settled decisions

| Topic | Decision |
|---|---|
| Worker | **DeepSeek**, validated by measurement (see [calibration](calibration.md)) |
| Languages | **PHP first.** Python designed for, implemented later |
| Extraction | **`token_get_all()`**, PHP's official tokenizer — not tree-sitter |
| Validation | native per language: `php -l`, `ast.parse()` |
| Rollback | **git object store** |
| Operational log | SQLite, permanent |
| Efficacy log | JSONL, **derived** from SQLite, temporary |
| Doc export | out of scope |
| User interface | the orchestrator. No end-user CLI. |

---

## 3. The target, measured

The first real deployment target is a ~35,000-line PHP business
application. Measured rather than assumed:

| Metric | Value |
|---|---|
| First-party PHP files | 131 |
| Lines of first-party PHP | ~34,900 |
| Largest file | 4,652 lines |
| Methods in it | **202**, one class |
| `case` branches in the API router | **199** |
| Parallel sibling "Calc" classes | **46** |
| Files mixing PHP with HTML | **1** |

What follows from it:

**🟢 The PHP/HTML risk is marginal.** One file mixes HTML. The rest is clean
PHP with well-formed classes and functions.

**🟢 The 4,652-line class is the perfect use case.** 202 methods on a
uniform `public function name(...)` pattern. Symbol addressing will work
almost always, and the gain is large: sending a ~30-line method instead of
a 4,652-line file is a ~99% reduction.

**🟡 The API router's `switch` needs its own addressing.** 199 `case`
branches are the extension point for the whole API, and they are not PHP
symbols.

**🟡 46 sibling classes are a goldmine for generation.** Having 46
implementations of the same shape turns "write a new one" into "copy this
one" (§6).

**🔴 Credentials live inside the repository tree.** The secrets gate (§7) is
not a theoretical precaution: there is sensitive material in the same tree
the tool will operate on.

**🔴 A 4,652-line single class is a concurrency hazard.** If a batch is
split into 15 units and 12 touch that file, strict write serialisation and
the stale-offset guard are critical, not optional.

---

## 4. The rule that overrides everything: bytes

**The entire path — extraction → payload → splice → write — works in
`bytes`.** Decode to text only to hand it to the worker; re-encode on
return.

This is not a style preference. `token_get_all()` returns **byte offsets**;
Python `str` is indexed **by character**. Mixing them shifts the cut by one
position per multi-byte character preceding the symbol:

```
the file:       4,506 bytes / 4,504 characters   ← 2 accented chars

slice on str    → 'blic function closeShift($shiftId) {'
slice on bytes  → 'public function closeShift($shiftId) {'
```

Calibration failed **6 of 9 cases** to this before it was found. The target
codebase has non-ASCII comments throughout: not an edge case, the normal
case. And it fails **silently** — the result still looks like code.

Any `read_text()` on that path is a bug waiting for a file with an accent in
it. Details: [calibration.md](calibration.md), Finding 1.

---

## 5. Validation gates

Nothing reaches disk until these pass, in order.

### Gate 0 — offsets (the critical one)

```
file[start:end] == the_src_we_sent      # in bytes
```

Checked by re-reading the file immediately before writing. Catches the file
having changed since extraction (an earlier patch in the same batch, another
process, the user's editor) and any error in boundary computation.

This is the gate that prevents the worst damage: splicing at wrong offsets
**injects the block into the middle of another method** and destroys code.
Calibration validated its worth — it would have caught all nine corruptions
in the first run.

### Gate 1 — syntax

The whole file is reconstructed in memory and validated: `php -l` for PHP,
`ast.parse()` for Python.

*Known limit:* form only. A call to a non-existent method passes cleanly.
Cheap future mitigation: check that every `$this->x(` and `Class::x(` in the
new block exists in the symbol map `extract.php` already produces.

### Gate 2 — exactly one symbol

The returned block must define exactly one symbol, so the worker cannot
quietly add a sibling function. Two lines using `extract.php`.

### Gate 3 — substance (before bulk use, not day one)

Compare the set of calls, variables and control keywords in the block before
and after. If the new block has lost one the instruction did not ask to
remove, reject and report what vanished.

This attacks the real risk: code that compiles but has lost logic from
*inside* the method. Calibration did not see it occur (3/3 additive tasks
lost nothing), which is why it drops in priority — but nine cases of 5–28
lines do not rule it out, and the risk grows with block size.

### Removed — the perimeter gate

Comparing the bytes outside the target range can never fail. The server
builds the file as `original[:start] + block + original[end:]`, so the
perimeter is preserved **by construction**. The test that verified it was
removed too: a test that cannot fail is worse than no test, because it emits
a positive signal without inspecting anything.

Calibration proved this empirically — the perimeter gate reported 9/9 while
three files were syntactically broken. See [RF-1](critical-review.md).

**On failure:** structured error to the orchestrator naming the reason, no
write, and a log entry recording **which gate** rejected it.

---

## 6. Tools

### Modification

| Tool | Addressing |
|---|---|
| `fix_symbol(file, symbol, instruction)` | by name — immune to line drift, the primary mode |
| `fix_range(file, start, end, instruction)` | by line — escape hatch for code that lives outside any symbol |

Symbol addressing matters because in a batch of 20 units on one file, line
numbers move underfoot. A name does not.

**Hard size limit.** If a symbol exceeds ~150 lines, the tool **refuses**
and tells the orchestrator to use `fix_range` or do it itself. The target
codebase contains a 1,380-line method: for it, symbol extraction yields
nearly the whole file — no saving, and a block too large for a cheap model
to return intact. Failing loudly beats degrading silently.

### Generation

The original spec only covered *replacing* existing code. Half the real use
case is creating it, and in this codebase generation is almost never writing
from a blank page — it is **replicating a pattern that already exists 46
times, at a known structural anchor**.

| Tool | What |
|---|---|
| `insert_symbol(file, anchor, position, instruction)` | insert a method before/after a named symbol, or at end of class/file |
| `insert_case(file, after_case, instruction)` | a new branch in a `switch` router — not a PHP symbol, so it needs its own addressing |
| `create_file(path, model_from, instruction)` | **generation by analogy** |

**Generation by analogy is the strongest idea here.** Rather than describing
a new class from scratch, hand the worker a sibling as a *structural
exemplar* plus a diff instruction. It does not invent architecture; it
traces it. Cheap in tokens (one ~150-line exemplar), high in consistency,
and it turns the worker's weakness — knowing nothing about the project —
into an irrelevance. It does not need to know; it needs to copy well.

**Correction (2026-08-23).** An earlier draft of this document proposed a
"split perimeter" gate for insertions — everything before the insertion
point byte-identical, everything after byte-identical — and claimed that
unlike the removed perimeter gate, this one could fail. **It cannot.** The
server builds an insertion as `original[:at] + block + original[at:]`, so
both halves are copied verbatim, exactly as in the replace case. Writing it
would have repeated the mistake RF-1 exists to prevent.

What insertions actually need is a **symbol-set gate**: the set of symbols
present before must survive intact, plus exactly the N new ones declared.
This one can fail, and for a real reason — a block carrying one brace too
many closes the class early, which restructures everything after it while
still parsing cleanly. `php -l` would not notice; this does.

`create_file` has its own gate: **the file must not exist.** Never overwrite
through it.

### Transactions

Adding one feature to this codebase touches 3–4 files at once, and doing it
halfway leaves the project worse than before starting. A new `Calc` class
without its `require_once` is worse than no class.

`patch_group` — the orchestrator opens a group, sends N operations, closes
it. All are validated before any is applied. Either all land or none do; if
the third of four fails a gate, the first two roll back automatically.

**Batching is the architecture, not an enhancement.** The natural mode of
this tool is the batch, and `patch_group` belongs in the first phase.

### Rollback

| Tool | What |
|---|---|
| `revert_patch(patch_id)` | restore one patch |
| `revert_session()` | restore an entire batch, in reverse order |

`revert_session` matters: when a batch of 20 fails at the 17th, you do not
want to undo one, you want to undo all of them.

---

## 7. Rollback via the git object store

Git is already a content-addressed database, so it is used as one instead of
duplicating snapshots.

Before each patch:
```
git hash-object -w <file>   →  blob_sha
```
This writes the original content into `.git/objects` and returns its SHA,
which goes into the log. **Real added disk cost: near zero** — git
compresses and deduplicates, and if the content was already committed
nothing new is written.

To revert:
```
git cat-file blob <blob_sha>  →  original content  →  rewrite the file
```

Advantages over manual snapshots: works with a dirty working tree, dedup and
compression for free, survives across sessions, auditable with standard git
tooling, and there is no bespoke format to maintain.

**Caveat to document:** these blobs are unreachable until committed, and an
aggressive `git gc --prune` could remove them. Mitigation: anchor them with
a ref under `refs/bifrost/`, or simply never run `gc --prune=now` (the
default expiry is two weeks, far beyond a rollback's useful window).

---

## 8. Indentation normalisation

**The worker must never have to guess indentation.** The server strips it
before sending and restores it on return:

```
extracted  "    public function foo() {\n        return 1;\n    }"
   ↓  normalise (indent="    ")
sent       "public function foo() {\n    return 1;\n}"
   ↓  worker
received   "public function foo() {\n    return 2;\n}"
   ↓  re-indent with indent
applied    "    public function foo() {\n        return 2;\n    }"
```

The payload carries a mandatory `indent` field from day one, even where an
implementation does not use it.

Not a theoretical precaution: in calibration 1 of 9 cases returned a block
at the wrong indentation while being otherwise correct. And this is the same
problem anticipated for Python, where it is worse — Python's indentation is
semantic, so a badly re-indented block is not ugly, it is broken. Solving it
in the payload closes both cases at once.

---

## 9. Heimdall — the gate on the bridge

The worker is a third-party API and the target is production code. Nothing
crosses to it without being looked at first: once a secret has left this
machine it cannot be recalled.

Three rules shape the implementation:

1. **The default is not to leak.** A finding blocks the send. Overriding is
   possible via `allow_secrets`, explicit, and recorded in the log's
   `override` column — never implicit.
2. **Heimdall never repeats what it saw.** Findings carry a pattern name and
   a line number, never the matched text. A gate that logs the secret it
   caught has moved the secret, not stopped it. Verified: after a run that
   blocked a connection string and an API key, neither appears anywhere in
   the log file.

3. **Blocking is the fallback, not the first move.** Where the secret is a
   self-contained token, it is swapped for a placeholder, the worker
   transforms the code around it, and the original goes back before the file
   is written. The work proceeds and the secret never leaves the process.

It stands *before* the worker call, not after — anything checked afterwards
has already crossed.

### Redaction

```
$key = "sk-live-…";   →   $key = "__BIFROST_SECRET_0__";
                          worker transforms around the placeholder
$key = "sk-live-…";   ←   restored before the splice
```

**What makes this safe is the verification, not the substitution.** If a
placeholder does not come back — the worker reformatted the line, split the
string, or helpfully replaced it with a realistic example — restoring a
partial result would write `__BIFROST_SECRET_0__` into production source in
place of a live credential. The code still compiles, so nothing fails until
something stops connecting. That is strictly worse than refusing.

So `restore()` raises rather than returning partial output: every
placeholder must come back **exactly once**. Missing means the worker lost
it; duplicated means it copied the line, which is a change nobody asked for.

**Context is redacted into the same vault but tagged separately.** A
placeholder from `src` belongs back in the file. One from an exemplar
appearing in the worker's *output* means it copied a credential out of a
neighbouring file into a new one — restoring that would be obediently
propagating a secret, so it is refused instead.

**Not everything is redactable**, and the exclusions are deliberate:

| Excluded | Why |
|---|---|
| `private-key` | the pattern matches only the PEM header; redacting that leaves the key body in the payload |
| `gcp-service-account` | matches a type marker, not the credential |
| `high-entropy` | the run may *be* the logic — a base64 constant the code depends on. Substituting it changes the program |

Anything unredactable still blocks. `redact_secrets=False` returns the whole
gate to blocking outright.

**The vault never prints.** Its `__repr__` is overridden, because a Vault
surfacing in a traceback or a debug line would undo the entire gate.

**Coverage:** vendor formats (AWS, Google, Slack, GitHub, Stripe, JWT, PEM
private keys), connection strings carrying passwords, credential-shaped
assignments to string literals, and a Shannon-entropy check for secrets with
no recognisable shape. A pattern list is a floor, not a ceiling.

**False-positive rate, measured:** 2 findings across 1,291 real symbols
(0.2%), both correct refusals of code that *manipulates* keys rather than
holding one, and both overridable. Being wrong cautiously costs a rejection;
being wrong the other way costs a credential.

This is mandatory rather than advisory because the target repository
synchronises credential files inside its own tree.

### Spending limits

A per-session hard stop on calls and tokens (`budget.py`). An orchestration
bug — a retry loop, a batch built from a bad symbol list — must not be able
to run up a bill unattended. It raises rather than returning a flag, so a
forgotten check cannot silently continue.

---

## 10. Cost and concurrency

- Per-session call and token counters with a hard configurable cut-off. A
  bug in orchestration logic must not be able to generate a surprise bill.
- **Parallelise the calls, serialise the writes.** Worker calls are
  independent — each symbol is processed in isolation — and only disk
  application needs ordering. Serialising *per file*, as first designed,
  never engages where it matters: the main target is one file with 200
  symbols, so a batch of 30 would run entirely in series.

  Measured latency is 2.6 s per call. Thirty calls in series ≈ 78 s; six-way
  parallel ≈ 13 s. Per-call latency is not a problem — it is comparable to
  the orchestrator's own edit — but multiplied across a batch it is.

---

## 11. Logging

Two purposes, two lifetimes, **one write**.

| | Operational log | Efficacy log |
|---|---|---|
| Purpose | know what happened, be able to undo it | prove the project is worth it |
| Lifetime | permanent | **temporary — deleted once the premise is proven or refuted** |
| Read as | targeted queries | whole, in one pass |
| Format | **SQLite** `.bifrost/history.db` | **JSONL** `.bifrost/efficacy.jsonl` |

That the second has an expiry date is exactly what justifies keeping it
separate. A measuring instrument should not live inside the system it
measures.

**The JSONL is derived, never written in parallel.** Writing both on every
operation looks harmless and is not: if the process dies between the two
writes, they diverge and there is **no way to tell which is right** — and
that happens precisely when the log matters most. SQLite is the only write;
the JSONL is an export.

```
operation → SQLite (single, transactional write)
                │
                └── export ──> efficacy.jsonl ──> registre.py
```

### Schema

```sql
CREATE TABLE patches (
  id           TEXT PRIMARY KEY,   -- uuid4
  ts           TEXT NOT NULL,      -- ISO-8601 UTC
  session      TEXT,               -- groups a batch
  grup         TEXT,               -- patch_group, if any
  op           TEXT NOT NULL,      -- fix_symbol | fix_range | create_file…
  fitxer       TEXT NOT NULL,
  simbol       TEXT,
  start_byte   INTEGER,
  end_byte     INTEGER,
  estat        TEXT NOT NULL,      -- ok | rejected | error
  porta        TEXT,               -- which gate rejected it, if any
  blob_abans   TEXT,               -- git hash-object → rollback
  head_sha     TEXT,               -- repo HEAD at the time
  instruccio   TEXT,
  rationale    TEXT,               -- the worker's `why`
  src_b        INTEGER,            -- ── efficacy measurements ──
  out_b        INTEGER,
  in_b         INTEGER,
  resp_b       INTEGER,
  tin          INTEGER,
  tout         INTEGER,
  cache_hit    INTEGER,            -- 43% measured
  ms           INTEGER
);
```

Everything is recorded, including rejected attempts and which gate rejected
them. That is where the information to calibrate the system lives.

### Measurements, not conclusions

Nowhere is `saving: 1850` stored. The bytes are stored and the saving is
derived at read time, so when the formula turns out optimistic it can be
corrected without invalidating history.

```
would_have_cost ≈ (src_b + out_b) / 3.5     # orchestrator reads + writes
did_cost        ≈ (in_b + resp_b) / 3.5     # instruction + "OK"
saving          = would_have_cost - did_cost
```

**An upper bound, and labelled as one:** if the orchestrator had already
read the block to formulate the instruction, `src_b` was paid regardless.

---

## 12. Parsers

**Extraction: `token_get_all()`, not tree-sitter.**

| | tree-sitter-php | `token_get_all()` |
|---|---|---|
| Dependencies | Python package + grammar | **none** (the `php` binary is already required) |
| Fidelity | third-party grammar | **the official lexer** |
| Tested on the target | — | **128/128 files, zero failures** |

`calibratge/extract.php` implements it: name, class, FQN, byte offsets,
lines, indentation, and the start of any preceding docblock. It includes a
sanity check that concatenating the tokens reconstructs the file byte for
byte — if that fails it aborts rather than returning doubtful offsets.

**Validation: native per language.** `php -l`, `ast.parse()`. Here fidelity
matters more than uniformity: the official parser is the only authority on
whether a file is valid.

For Python, `ast` covers both jobs — also stdlib, also official, with exact
`lineno`/`end_lineno`. The symmetry holds: **each language is analysed with
its own official tooling**, which turns out more coherent than forcing a
common third-party grammar.

`LanguageAdapter` is still needed as an abstraction. What varies between
languages is the adapter's *implementation*, not its shape.

---

## 13. Roadmap

**Phase 0 — the loop, complete and safe, on PHP — ✅ DONE**

Exit criterion met 2026-08-23: 10 chained corrections against a 4,652-line,
200-symbol production file, 10/10 applied, `php -l` clean, symbol count
unchanged, `revert_session` restoring it byte for byte.

Two bugs were found getting there, both ours rather than the worker's:
`apply_indent` padded the first line (which would have doubled indentation
invisibly), and the extractor truncated any symbol containing `"{$var}"`
string interpolation — PHP tokenises that opening brace as a named token but
its closing brace as a bare character, so counting only bare characters
closed the symbol early. The second one produced a genuinely corrupt patch
before it was caught. Both are covered by mutation-verified regression tests.

- `fix_symbol`, `fix_range`
- the whole data path in `bytes` (§4) — non-negotiable
- extraction via `extract.php`, validation via `php -l`
- the spec's compact schema plus the `indent` field
- gates 0, 1, 2
- SQLite log + git blob before each patch
- test bench: the small uniform sibling classes before the 4,652-line file
- **exit criterion:** 10 chained corrections on the large file with zero
  corruption and `offsets_ok` verified at every step

**Phase 1 — hardening**
- secrets gate (§9) — not optional
- substance gate (§5, gate 3)
- write serialisation and cost limits (§10)
- `revert_session()`
- ✅ **a safety net for the target codebase** — done. A structural smoke
  test (syntax, class loading, 319 router cases resolving to real handlers)
  and a behavioural runner (15 tests, database snapshotted and restored with
  the restore verified by hash). ~24s for a single green or red. Building it
  exposed two tests that had never been able to fail. See
  [RF-3](critical-review.md).

**Phase 2 — generation on PHP**
- `insert_symbol`, `create_file` with generation by analogy
- `patch_group`

**Phase 3 — the API router — ✅ DONE**
- `insert_case`, anchored on a neighbouring case label.
- Gate: a duplicate label is refused. This is the failure worth naming — PHP
  takes the first matching `case` and ignores the rest without a word, so a
  duplicate is dead code that looks alive. The file parses, the symbol set is
  untouched, the old branch's tests still pass, and the new endpoint simply
  never runs.
- Case boundaries end after the branch's last statement, not at the next
  `case`, so the blank line and section comment between them stay with what
  they introduce.
- A fall-through branch (a bare label dropping into the one below) is
  refused as an anchor: inserting after it cuts the chain, and the result
  still parses.

**Phase 4 — scale and Python — ✅ DONE**
- `fix_symbols`: one instruction across many symbols. **Calls in parallel,
  writes in series.** Serialising per file — the obvious design — never
  engages where it matters, because most of a batch lands in the same large
  file. Measured 2.4× on a 12-symbol batch.
  What makes it safe is symbol addressing: earlier writes move later offsets
  but not their content, so each symbol is re-resolved by name immediately
  before its write and gate 0 compares content, not position.
- Python adapter: `ast` for both extraction and validation, mirroring PHP's
  use of its own official tooling.
- **Indent normalisation is now self-verifying.** `apply_indent` pads every
  non-blank line while `strip_indent` only removes padding from lines that
  carry it. Those are inverses until a line inside the block sits shallower
  than the block itself — ordinary in Python, where a triple-quoted string's
  body can start at column zero. Re-indenting that injects whitespace INTO a
  string literal, changing the program's data rather than its layout, and no
  gate can see it because nothing structural is wrong. So `normalise()`
  performs the round trip and compares; when it does not hold, the block is
  sent with its real indentation instead. Verified across 317 Python symbols.

**Out of scope for now:** documentation export, Java, diff preview for large
changes, automated branches and PRs.

---

## 14. What adding Python would take

Short answer: **little, if prepared for now; a lot, if improvised later.**

| Piece | Cost |
|---|---|
| Symbol extraction | **low** — `ast` with exact `lineno`/`end_lineno` |
| Syntax validation | **very low** — `ast.parse()` is stdlib, cheaper than `php -l` |
| Perimeter and structural gates | **zero** — they operate on bytes and symbol lists |
| Log, git rollback, `patch_group` | **zero** — they do not know what language they touch |
| Worker prompt | **low-medium** — one variant per language |
| Secrets gate | **zero** |

**Where the real work is:**

1. **Python's indentation is semantic.** Already solved by §8, which is why
   that decision had to be made up front.
2. **`insert_symbol` with `position: after`** must know where a Python
   symbol truly ends. A brace makes this unambiguous in PHP; in Python the
   end is "the first line with indentation less than or equal", and trailing
   comments and blank lines are contested territory. `ast` resolves it, but
   the ownership of trailing comments must be decided explicitly.
3. **Character offsets again, in reverse.** Python's `ast` reports character
   columns while `token_get_all` reports bytes. §4's rule applies in both
   directions.

**Recommendation:** define `LanguageAdapter` in Phase 0 with a single
implementation. Do not implement Python yet, but make no decision that
prevents it. Then Python is a day's work rather than a rewrite of the core
with the system already running against production code.
