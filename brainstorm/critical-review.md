# Critical review — fresh eyes

**Date:** 2026-08-23 · on `plan.md` v0.4
📊 **Checked against data:** [`./calibration-results.md`](./calibration-results.md).
RF-1 confirmed empirically; **RF-5 refuted** (latency 2.6s, not 5-30s);
**RF-10 softened** (43% cache, not marginal); RF-2 not manifesting
in 9 cases.
**Premise of this review:** I come from outside, I took no part in any
prior decision, and my job is to find why this will fail. I don't take
anything as given just because it's written in the plan.

**Short verdict:** the project is buildable and has a real use case, but
**the plan justifies the project with the wrong argument** and **three of
its safety guarantees guarantee nothing**. Neither problem is fatal, but
both change what needs to be built.

---

## 🔴 RF-1 — The perimeter gate can never fail

**The plan says** (§4.1, gate 3): "byte-by-byte comparison of the entire
file outside the target rangee. It must be identical. This is the 100%
preservation guarantee."

### Why it can never fail

The MCP does not receive a file from the worker. It receives **a block**,
and it builds the new file itself:

```python
new = original[:start] + worker_block + original[end:]
#     └──────┬──────┘                      └──────┬──────┘
#      left perimeter               right perimeter
#      copied verbatim               copied verbatim
```

The gate then checks this:

```python
assert nou[:start] == original[:start]          # always true
assert nou[-(len(original)-end):] == original[end:]  # always true
```

Both perimeter bands are **the same object strings** we just concatenated.
Comparing them is comparing `x` with `x`. There is no worker input — not
broken code, not an empty response, not 50.000 lines of garbage — that can
make this check fail, because the worker **has no way to influence the
bytes outside the block**.

It's worth seeing this from the other side: for the gate to fail,
`original[:start] != original[:start]` would have to hold. This is not
improbable, it is impossible.

### Where the mistake comes from

The gate makes sense — but **in a different architecture**. The original
spec was born thinking of the usual pattern "the model rewrites the file
and we save it":

```
worker -> whole file -> MCP saves it
```

Here the worker really does touch the perimeter, and comparing it is
**the** essential defense: it's the only way to detect that the model has
invented a function in the middle, reordered imports, or eaten the end of
the file.

But we chose the other pattern:

```
worker -> block only -> MCP splices
```

And this pattern **eliminates the threat structurally**. Perimeter
preservation stops being something that gets verified and becomes
something that is *guaranteed by design*. The gate is an inherited defense
against an attack our architecture already made impossible.

This is not bad — **it's the signal that we chose the architecture well**.
The only problem is that the plan claims credit for it twice: once for the
design, and then again as if it were an additional validation.

### Why it's dangerous to leave it written down

1. **False confidence.** The plan presents this gate as "the 100%
   preservation guarantee that the spec promises". Anyone reading the plan
   will conclude there is an active check protecting the file. There
   isn't one: there is a property of `+`.
2. **A green test that proves nothing.** The plan asks for (§7, Phase 0) a
   "test that verifies byte-by-byte perimeter preservation". This test
   will always pass, even with broken code. A test that can never fail is
   worse than no test: it gives a positive signal without looking at
   anything.
3. **It diverts attention.** While the perimeter is being watched, nobody
   watches inside the block, which is the only place the worker can do
   harm (→ RF-2).

### What is actually worth checking

There is **one** check in this family that is not tautological, and it
matters. It doesn't refer to the perimeter but to **the offsets**:

```python
assert original[start:end] == the_src_we_sent
```

This **can actually fail**, and for real reasons:

- The file changed between extraction and writing (an earlier patch from
  the same batch, another process, the user's editor).
- `extract.php` miscalculated the symbol's boundaries.
- An operation from another file got mixed in.

And when it fails, the harm it prevents is total: splicing with the wrong
offsets **injects the block into the middle of another method** and
destroys code without `php -l` necessarily noticing.

This check already exists in the plan: it's the stale rangee guard (§4.3).
**The conclusion, then, is not "a gate is missing" but "the useful gate in
this family we already have, and the one in §4.1 is redundant".**

### Note on gate 2 (structural integrity)

It has the same flaw, for the same reason. The plan justifies it like
this: "catches the classic case of the worker taking neighboring code
along with it".

With extraction by symbol, **the worker never sees neighboring code**. It
receives a single isolated method. It cannot delete the method next door
because it doesn't know it exists, just as it cannot delete a file we
haven't sent it.

The only part that retains value is the inverse: checking that the
returned block **does not contain more than one symbol definition** (that
the worker hasn't added a sibling function on its own). This is indeed
possible and does need checking — but it's a check on the **block**, two
lines with `extract.php`, not a comparison of the whole file.

### Summary

| Check | Can it fail? | Worth it? |
|---|---|---|
| Identical perimeter outside the rangee | ❌ Never | No. Remove it. |
| Symbols of the whole file before/after | ❌ Practically never | No. Remove it. |
| The block contains exactly 1 symbol | ✅ Yes | **Yes**, and it's cheap. |
| `original[start:end]` == `src` sent | ✅ Yes | **Yes**, and it's critical. Already §4.3. |
| `php -l` on the resulting file | ✅ Yes | **Yes.** The only real gate in the plan. |
| The block hasn't lost substance | ✅ Yes | **Yes** → RF-2, and it doesn't exist yet. |

---

## 🔴 RF-2 — The real risk has no gate

If the worker can't touch what's outside the block, what can go wrong?

1. Return code that doesn't compile → **`php -l` catches it.** ✅
2. Return code that compiles but **has lost logic from inside the
   method** — dropped an `if` branch, left out a call, simplified a
   `try/catch`. → **No gate catches it.** ❌
3. Return correct code but with changed semantics → no gate. ❌

Case 2 is the most likely failure mode for a cheap model with reduced
context, and it is exactly what the plan doesn't cover.

**Proposal: substance gate.** Before and after, extract from the block the
set of:
- function/method calls (`->foo(`, `Foo::bar(`, `bar(`)
- variable names
- control keywords (`if`, `foreach`, `try`, `throw`, `return`)

If the new block has **lost** any that the instruction did not ask to
remove, it is rejected and Claude is told what disappeared. It's cheap
(regex over the block, or better: `token_get_all`, which we already have)
and it attacks the real risk instead of the imaginary one.

This replaces gates 2 and 3, it doesn't add to them.

---

## 🔴 RF-3 — There is no semantic safety net under any of this

> **Resolved 2026-08-23.** A structural smoke test and a test runner with
> database isolation were built for the target codebase; together ~24s for
> one green or red. Building them found two of the fifteen existing tests
> had never been able to fail. The finding below still stands as an
> assessment: 15 behavioural tests over ~35,000 lines is a net, not
> coverage.

The plan delegates semantic validation to "Claude runs the test suite"
(§3 of the original workflow). I looked at what's actually there:

| | |
|---|---|
| Own PHP code | ~34,900 lines |
| Test files | 15 |
| Test lines | 1,845 |
| Framework | **None** (ad-hoc scripts, not PHPUnit) |
| `composer.json` with dev-dependencies | **No** |

**The ratio is ~1 test line for every 19 of code, with no runner.** There
is no way to run it all and get a reliable green/red.

This means the only barrier against a semantic regression introduced by a
cheap model is **someone noticing while using the application**. In code
that handles invoices, payments to contractors, and time clocking.

**This is by far the most serious finding of the review**, and it isn't a
problem of MCP-Bifrost: it's a problem of the target codebase that
MCP-Bifrost **amplifies**. Automating mass changes over a codebase with no
test safety net multiplies the risk surface at the same rate it
multiplies productivity.

**Hard recommendation:** do not deploy Bifrost in mass mode over business
logic (`Calc/`, `App/`, `api/`) until there is a minimal safety net. Full
coverage isn't needed — an executable *smoke test* is needed that loads
the classes, calls the main paths, and fails loudly. It can be built
*with* Bifrost, over low-risk code, as the project's first real job. It's
the way to get immediate value without betting the business.

---

## 🟠 RF-4 — The plan sells context savings, and the savings are small

**The plan says**: 85-95% reduction (the spec) or ~99% (§2.5). These
numbers compare the extracted block against **the whole file**. But
nobody was proposing to send the whole file. The honest comparison is
against **what Claude would spend doing the edit itself**, which with the
`Edit` tool is `old_string` + `new_string`, not the file.

Let's do the numbers for a 30-line method (~400 tokens):

| Path | Claude's context consumed |
|---|---|
| Claude edits directly | reading ~400 + writing ~400 ≈ **800 tok** |
| Via Bifrost | instruction ~60 + response ~15 ≈ **75 tok** |

Real savings: ~725 tokens per edit. **True, but modest** — and with a
catch: if Claude had to **read the method** to know what to ask for, it
has already paid the 400 reading tokens and the savings drop by half.

**Where the argument really does hold up, and by a lot:**

When Claude **doesn't need to read the code** to formulate the
instruction. "Add PHPDoc to AdminService's 202 methods", "change all calls
to the old API", "add error logging to every method in `Calc/`".

| | Claude does it all | Via Bifrost |
|---|---|---|
| 202 methods × ~800 tok | **~161,000 tok** — doesn't fit in a window | 202 × ~75 = **~15,000 tok** |

**Here there really is a real 90%, and it also makes possible something
that literally didn't fit before.**

**Consequence for the project:** Bifrost's crown jewel **is not "fix this
bug"** — for a one-off bug Claude has to read the code anyway, and the
savings don't compensate for the latency or the risk. The jewel is
**mechanical transformation at volume**, where Claude reads nothing and
the gain is an order of magnitude.

The plan should say this openly, because it changes the priorities: the
most valuable tool is not `fix_symbol` one at a time, but **`fix_symbols`
in batch** with a single instruction applied to N symbols.

---

## 🟠 RF-5 — ❌ REFUTED — In interactive use, Bifrost is slower than not using it

> **Measured on 2026-08-23: average 2.6s, maximum 3.6s.** Practically the
> same as a Claude `Edit`. This finding does not hold up and is
> withdrawn. The recommendation to work in batches still stands, but for
> economy (RF-4), not for speed.

A call to DeepSeek with a code block takes seconds — typically 5-30s
depending on size and load. Claude doing the `Edit` directly: ~2s.

For a one-off fix, **the user waits longer and takes on more risk in
exchange for saving a few hundred tokens**. It's a bad trade.

Reinforces RF-4: Bifrost must be a **batch** tool, not an interactive one.
And the plan should make the criterion explicit so Claude doesn't use it
when it shouldn't. Without this criterion written down, Claude will tend
to use the tool because it exists.

**Proposed criterion, to write into the tool's description:**
> Use Bifrost when you have **≥5 similar transformations** or when the
> instruction can be formulated **without having read the code**. For a
> one-off fix you already have in front of you, edit it yourself.

---

## 🟠 RF-6 — Claude goes blind after the first patch

The MCP returns `OK` + `diff_stat`. Claude **does not see the resulting
code**.

In a batch this accumulates: after 10 patches to `AdminService.php`, the
mental model Claude has of the file corresponds to the **initial** state.
If job 11 depends on what job 3 did, it will decide based on false
information — and it will do so with total confidence, because every
response was `OK`.

It's the direct side effect of the "0 tokens in Claude's context" goal:
the blindness **is** the saving.

**Proposed mitigation:** have the return include a **compact unified
diff** (not the whole block) when the change exceeds a threshold, and
just `OK` below it. A 3-line diff costs ~40 tokens and keeps Claude in
sync. The "always 0 tokens" decision is too absolute; what's wanted is
"few tokens", which is not the same thing.

---

## 🟡 RF-7 — Addressing by symbol assumes symbols are small

Measured on the target codebase:

- `AdminService.php`: 200 symbols in 4,653 lines → average ~23 lines. ✅
- `Database.php`: **5 symbols in 1,509 lines**. `runMigrations` is
  **1,380 lines all by itself**.

For `runMigrations`, `fix_symbol` doesn't extract a manageable unit: it
extracts almost the whole file. Zero savings, and a block too large for a
cheap model to return whole without losing something in it (which is
exactly RF-2).

**A hard limit is needed**: if the symbol exceeds N lines (proposal:
150), the tool **rejects** and tells Claude to use `fix_rangee` or to do
it itself. Failing clearly is much better than silently degrading.

---

## 🟡 RF-8 — Parallelism doesn't help the main case

The plan proposes processing batches in parallel with **per-file
serialization** (§4.6). But the main target is `AdminService.php`: **a
single file with 200 symbols**. A batch of 30 fixes would all land on the
same file and, with per-file serialization, would run **entirely in
series**.

30 calls × 15s = **7 and a half minutes** per batch. Parallelism, as
designed, never kicks in where it's needed most.

**Alternative:** serialize the **write**, not the **call**. Calls to the
worker are independent (each symbol is processed in isolation); only disk
application needs order. Launch the N calls in parallel, collect the
results, validate them all, and apply them in series over recalculated
offsets. This goes from 7 minutes to under one.

This fits naturally with `patch_group` (§4.4) — and in fact suggests that
**`patch_group` is not a phase 2, it is the correct architecture from day
1**: Bifrost's natural mode is the batch, not the standalone call.

---

## 🟡 RF-9 — `ctx` with bare signatures probably does nothing

The spec sends `ctx: ["dep_signature_1", ...]`. For the task "add a guard
if `$data` is empty", knowing that `ClientCalc::getClients()` exists
doesn't help at all. The worker needs to know **what `$data` contains**,
and that is not a signature.

It's cheap to send, and that's precisely why nobody will notice it's
useless. But sending useless context is worse than sending none: it
consumes tokens and can distract the model.

**The calibration needs to measure this**: do the same task with `ctx`
and without, and compare. If there's no difference, out of the schema.

---

## 🟡 RF-10 — ⚠️ SOFTENED — Prompt caching delivers less than the spec implies

> **Measured: 2,688 of 6,190 input tokens served from cache (43%).** Much
> more than this section anticipated. It doesn't change any decision, but
> the sentence "don't design anything assuming a gain from it" was
> excessive.

The spec highlights "leveraging DeepSeek Prompt Caching" as an
optimization. Caching acts on the **common prefix**, which here is the
system prompt: ~250 tokens. The variable part (`src`) is what dominates
and **is never cached**, because it changes on every call.

It isn't false, but it's marginal. It shouldn't appear as a pillar of
optimization; nothing should be designed assuming a relevant gain from
it.

---

## 🟡 RF-11 — Retries without idempotency

§9 recommends 1 automatic retry. If the failure is a network *timeout*
but DeepSeek **did** process the request, the retry duplicates it: it
gets paid for twice and, worse, if the first result arrives late it can
get applied twice.

**An idempotency key is needed** per operation, and disk application
needs to check that the destination block is still what was expected (the
§4.3 guard already does this — it just needs to be ensured it also covers
the retry path).

---

## 🟡 RF-12 — `php -l` is syntax only

`php -l` doesn't detect nonexistent methods, wrong arity, or types. A
worker that invents `$this->getClientByIdSafe()` passes gate 1 with no
problems and blows up in production.

For an MVP it's acceptable, but **it has to be said in the plan**,
because right now gate 1 is presented as if it validated the code, and it
only validates its form. Cheap future mitigation: check that every
`$this->x(` and `Classe::x(` in the new block exists in the symbol map we
already generate with `extract.php`. It's nearly free and closes the most
likely hole.

---

## 🟢 Things that are fine and should not be touched

- **Rollback via `git hash-object`** (§4.2) — it's the best decision in
  the plan. Free dedup and compression, works with a dirty working tree,
  no custom format.
- **SQLite instead of JSONL** (§5) — correct for the reasons it gives.
- **Generation by analogy** (§4.4, solution 2) — with 46 sibling `Calc`
  files, it's the idea with the best value-to-cost ratio in the whole
  document.
- **Stale rangee guard** (§4.3) — solves a real problem, not an imaginary
  one.
- **PHP-first** (§7) — correct, and the measured ground confirms it.

---

## Changes I'm proposing to the plan

| # | Change | Impact |
|---|---|---|
| 1 | Remove gates 2 and 3; replace them with the **substance gate** (RF-1, RF-2) | High — current security is apparent only |
| 2 | Rewrite the project's justification: **volume, not context** (RF-4) | High — changes what gets built first |
| 3 | `fix_symbols` **in batch** as the main tool, not standalone `fix_symbol` (RF-4, RF-8) | High |
| 4 | `patch_group` moves up to Phase 0 — the batch is the architecture, not an improvement (RF-8) | High |
| 5 | Parallelize calls, serialize writes (RF-8) | Medium — 7 min → <1 min |
| 6 | Return with compact diff above a threshold (RF-6) | Medium |
| 7 | Hard symbol-size limit with explicit rejection (RF-7) | Medium |
| 8 | Written criterion for **when NOT to use Bifrost**, inside the tool's description (RF-5) | Medium |
| 9 | Build a smoke test for the target codebase **before** mass use (RF-3) | **Blocking** |
| 10 | Calibration must measure `ctx` with and without (RF-9) | Low, but cheap |
| 11 | Idempotency key on retries (RF-11) | Low |
| 12 | Document `php -l`'s limits and add the symbol check (RF-12) | Low |

---

## The question that decides the project

Nothing here matters if the answer to this is bad:

> **When explicitly asked, does DeepSeek know how to return a real block
> of code from the target codebase byte-for-byte intact?**

It's the `identitat` case in the calibration. If it fails here, the
entire architecture rests on a worker that doesn't know how to leave
things as they are, and no validation gate will fix it.

**This test must be run before writing a single line of the MCP
server.** The harness is already written and only needs the key:

```bash
export DEEPSEEK_API_KEY=sk-...
python3 calibratge/calibra.py --casos 9
```
