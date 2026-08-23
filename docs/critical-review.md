# Critical review — fresh eyes

**Premise of this review:** arrive from outside, having taken part in none
of the earlier decisions, and find the reasons this fails. Nothing is
accepted as sound merely because it is written in the plan.

**Contrasted against data:** [calibration.md](calibration.md). RF-1
confirmed empirically; **RF-5 refuted**; **RF-10 softened**; RF-2 did not
manifest across nine cases.

**Short verdict:** the project is buildable and has a real use case, but the
original plan **justified it with the wrong argument** and **three of its
safety guarantees guaranteed nothing**. Neither problem is fatal; both
change what gets built.

---

## 🔴 RF-1 — The perimeter gate can never fail

The plan claimed: "byte-for-byte comparison of the whole file outside the
target range. This is the 100% preservation guarantee."

### Why it cannot fail

The server does not receive a file from the worker. It receives **a block**,
and builds the new file itself:

```python
new = original[:start] + worker_block + original[end:]
#     └──────┬───────┘                  └──────┬─────┘
#      left perimeter                   right perimeter
#      copied verbatim                  copied verbatim
```

The gate then checks:

```python
assert new[:start] == original[:start]                    # always true
assert new[-(len(original)-end):] == original[end:]       # always true
```

Both perimeter sides are the very strings just concatenated. Comparing them
compares `x` with `x`. No worker output — broken code, empty response,
50,000 lines of garbage — can make this fail, because the worker **has no
way to influence bytes outside the block**.

Seen from the other side: for the gate to fail, `original[:start]` would
have to differ from `original[:start]`. That is not unlikely; it is
impossible.

### Where the mistake comes from

The gate makes sense — **in a different architecture**. The original spec
was conceived around the usual "the model rewrites the file and we save it"
pattern:

```
worker → whole file → server saves it
```

There the worker *does* touch the perimeter, and comparing it is **the**
essential defence: the only way to detect that the model invented a function
in the middle, reordered imports, or ate the end of the file.

We chose the other pattern:

```
worker → only the block → server splices
```

And that pattern **eliminates the threat structurally**. Perimeter
preservation stops being something verified and becomes something
*guaranteed by design*. The gate is an inherited defence against an attack
our architecture already made impossible.

This is not bad — **it is the signal that the architecture is right**. The
problem is only that the plan claimed the credit twice: once for the design,
then again as if it were an additional validation.

### Why leaving it written is dangerous

1. **False confidence.** A reader concludes there is an active check
   protecting the file. There is not: there is a property of `+`.
2. **A green test that proves nothing.** The plan asked for a test verifying
   perimeter preservation byte for byte. That test passes always, including
   over broken code. A test that cannot fail is worse than no test.
3. **It diverts attention.** While the perimeter is watched, nobody watches
   inside the block, which is the only place the worker can do harm (RF-2).

### What is worth checking instead

There is **one** check in this family that is not tautological, and it
concerns the **offsets**, not the perimeter:

```python
assert original[start:end] == the_src_we_sent
```

This **can** fail, for real reasons: the file changed between extraction and
write (an earlier patch in the same batch, another process, the user's
editor); boundaries were computed wrongly; operations got crossed.

And the damage it prevents is total: splicing at wrong offsets **injects the
block into the middle of another method** and destroys code, without
`php -l` necessarily noticing.

So the conclusion is not "a gate is missing" but **"the useful gate in this
family already exists, and the one in the plan should go"**.

### Note on the structural-integrity gate

Same defect, same reason. The plan justified it as catching "the classic
case of the worker swallowing neighbouring code".

With symbol extraction, **the worker never sees neighbouring code**. It
receives one isolated method. It cannot delete the method next door, any
more than it can delete a file we never sent it.

What survives is the inverse: check that the returned block contains **no
more than one** symbol definition, in case the worker adds a sibling of its
own accord. That is possible and worth checking — but it is a check on the
**block**, two lines with `extract.php`, not a comparison of the whole file.

### Summary

| Check | Can it fail? | Worth it? |
|---|---|---|
| Perimeter identical outside the range | ❌ never | No. Remove. |
| Whole-file symbol list before/after | ❌ practically never | No. Remove. |
| Block contains exactly 1 symbol | ✅ yes | **Yes**, and cheap. |
| `original[start:end]` == sent `src` | ✅ yes | **Yes**, and critical. |
| `php -l` on the resulting file | ✅ yes | **Yes.** The only real gate in the plan. |
| Block has not lost substance | ✅ yes | **Yes** → RF-2, and it does not exist yet. |

> **Confirmed by measurement.** In the first calibration run the perimeter
> gate reported **9/9 while three files were left syntactically broken**.

---

## 🔴 RF-2 — The real risk has no gate

If the worker cannot touch anything outside the block, what can it get
wrong?

1. Return code that does not compile → **`php -l` catches it.** ✅
2. Return code that compiles but **has lost logic from inside the method** —
   an `if` branch dropped, a call forgotten, a `try/catch` simplified away.
   → **No gate catches it.** ❌
3. Return correct code with changed semantics → no gate. ❌

Case 2 is the most likely failure mode of a cheap model with reduced
context, and it is exactly what the plan did not cover.

**Proposal: a substance gate.** Before and after, extract from the block the
set of function/method calls, variable names, and control keywords. If the
new block has **lost** one the instruction did not ask to remove, reject and
report what vanished. Cheap (`token_get_all` is already available) and it
attacks the real risk instead of the imaginary one.

This replaces gates 2 and 3; it does not add to them.

> **Not observed in calibration** — 3/3 additive tasks lost no original
> line. Nine cases of 5–28 lines do not rule it out, and the risk grows with
> block size, so this moves to "required before bulk use" rather than "day
> one".

---

## 🔴 RF-3 — There is no semantic safety net underneath any of this

The plan delegates semantic validation to "the orchestrator runs the test
suite". What actually exists in the target project:

| | |
|---|---|
| First-party PHP | ~34,900 lines |
| Test files | 15 |
| Lines of test | 1,845 |
| Framework | **none** (ad-hoc scripts, no PHPUnit) |
| Dev dependencies declared | **no** |

**Roughly 1 line of test per 19 lines of code, with no runner.** There is no
way to execute it all and get a reliable pass/fail.

Which means the only barrier against a semantic regression introduced by a
cheap model is **somebody noticing while using the application** — in code
handling invoices, contractor payments and time tracking.

**This is by far the gravest finding in the review**, and it is not a
MCP-Bifrost problem: it is a target-codebase problem that MCP-Bifrost
**amplifies**. Automating bulk changes over a base with no test net
multiplies risk surface exactly as fast as it multiplies productivity.

**Hard recommendation:** do not deploy in bulk mode against business logic
until a minimal net exists. Full coverage is not required — an executable
smoke test that loads the classes, exercises the main paths and fails loudly
is. It can be built *with* Bifrost, against low-risk code, as the project's
first real job. That is how to get immediate value without betting the
business.

---

## 🟠 RF-4 — The plan sells context saving, and the saving is small

The plan advertised 85–95% (the spec) or ~99% reduction. Those numbers
compare the extracted block against **the whole file**. Nobody proposed
sending the whole file. The honest comparison is against **what the
orchestrator would spend doing the edit itself**, which with a targeted edit
tool is old-string plus new-string, not the file.

For a 30-line method (~400 tokens):

| Path | Orchestrator context consumed |
|---|---|
| Orchestrator edits directly | read ~400 + write ~400 ≈ **800 tok** |
| Via Bifrost | instruction ~60 + response ~15 ≈ **75 tok** |

Real saving: ~725 tokens per edit. **True, but modest** — and with a catch:
if the orchestrator had to **read the method** to know what to ask for, it
already paid the 400 tokens and the saving halves.

**Where the argument does hold, and strongly:**

When the orchestrator **does not need to read the code** to formulate the
instruction. "Add PHPDoc to all 202 methods", "change every call to the old
API", "add error logging to every method in this directory".

| | Orchestrator does it all | Via Bifrost |
|---|---|---|
| 202 methods × ~800 tok | **~161,000 tok** — exceeds a context window | 202 × ~75 = **~15,000 tok** |

**Here the 90% is real, and it makes possible something that literally did
not fit before.**

**Consequence:** the crown jewel is **not "fix this bug"** — for a one-off
bug the orchestrator has to read the code anyway. It is **mechanical
transformation at volume**, where nothing is read and the gain is an order
of magnitude. The plan should say so plainly, because it changes priorities:
the most valuable tool is not `fix_symbol` one at a time, but **batched
application of one instruction across N symbols**.

---

## 🟠 RF-5 — ❌ REFUTED — Interactively, Bifrost is slower than not using it

> **Measured: 2.6 s mean, 3.6 s max.** Effectively the same as the
> orchestrator's own edit. This finding does not hold and is withdrawn. The
> recommendation to work in batches survives, but on economics (RF-4), not
> speed.

*Original argument, kept for the record:* a worker call was assumed to take
5–30 s against ~2 s for a direct edit, making a one-off correction a bad
trade. The assumption was simply wrong.

---

## 🟠 RF-6 — The orchestrator goes blind after the first patch

The server returns `OK` plus a diff stat. The orchestrator **never sees the
resulting code**.

Across a batch this compounds: after 10 patches on one file, the
orchestrator's mental model corresponds to the **initial** state. If job 11
depends on what job 3 did, it decides on false information — and does so
confidently, because every response was `OK`.

This is the direct side effect of the "zero tokens in the orchestrator's
context" goal: the blindness **is** the saving.

**Mitigation:** return a compact unified diff (not the whole block) when the
change exceeds a threshold, and bare `OK` below it. A three-line diff costs
~40 tokens and keeps the orchestrator in sync. "Zero tokens always" is too
absolute; what is actually wanted is "few tokens", which is not the same
thing.

---

## 🟡 RF-7 — Symbol addressing assumes symbols are small

Measured in the target codebase:

- The 4,652-line class: 200 symbols, ~23 lines average. ✅
- Another file: **5 symbols across 1,509 lines**. One method is **1,380
  lines by itself**.

For that method, `fix_symbol` does not extract a manageable unit — it
extracts nearly the whole file. Zero saving, and a block too large for a
cheap model to return intact, which is precisely RF-2.

**A hard limit is required**: above ~150 lines, the tool **refuses** and
tells the orchestrator to use range addressing or do it itself. Failing
clearly beats degrading silently.

---

## 🟡 RF-8 — The parallelism does not help the main case

The plan proposes parallel batches with **per-file serialisation**. But the
main target is a single file with 200 symbols. A batch of 30 corrections
would land entirely in that file and, with per-file serialisation, run
**completely in series**.

At the measured 2.6 s per call, 30 calls ≈ 78 s for one batch. The
parallelism as designed never engages where it is most needed.

**Alternative:** serialise the **write**, not the **call**. Worker calls are
independent — each symbol is processed in isolation — and only disk
application needs ordering. Fire the N calls in parallel, collect, validate
all, apply in series over recomputed offsets. 78 s becomes ~13 s.

This fits naturally with `patch_group` — and in fact suggests that
**`patch_group` is not a later phase but the correct architecture from day
one**: the natural mode of this tool is the batch, not the single call.

---

## 🟡 RF-9 — Bare signatures as context are probably useless

The spec sends `ctx: ["dep_signature_1", ...]`. For a task like "add a guard
if `$data` is empty", knowing that some sibling method exists helps not at
all. The worker needs to know **what `$data` contains**, and that is not a
signature.

It is cheap to send, which is why nobody will notice it does nothing. But
sending useless context is worse than sending none: it costs tokens and can
distract the model.

**Calibration should measure it**: same task with and without `ctx`,
compared. If there is no difference, drop it from the schema.

---

## 🟡 RF-10 — ⚠️ SOFTENED — Prompt caching contributes less than the spec implies

> **Measured: 2,688 of 6,190 input tokens served from cache — 43%.**
> Considerably more than this section predicted. It changes no decision, but
> "design nothing assuming a gain from it" was overstated.

*Original argument:* caching acts on the common prefix, which here is the
~250-token system prompt, while the variable part (`src`) dominates and is
never cached. Directionally true, but the measured share is material.

---

## 🟡 RF-11 — Retries without idempotency

The plan recommends one automatic retry. If the failure is a network timeout
but the worker *did* process the request, the retry duplicates it: paid
twice and, worse, if the first result arrives late it could be applied
twice.

An idempotency key per operation is needed, and disk application must verify
the target block is still what was expected — the offset guard already does
this; it just has to cover the retry path too.

---

## 🟡 RF-12 — `php -l` is syntax only

`php -l` does not detect non-existent methods, wrong arity or type errors. A
worker that invents a method call passes gate 1 cleanly and breaks in
production.

Acceptable for an MVP, but **it must be stated in the plan**, because the
gate is currently presented as validating the code when it validates only
its form. Cheap future mitigation: check that every `$this->x(` and
`Class::x(` in the new block exists in the symbol map `extract.php` already
produces.

---

## Things that are right and should not be touched

- **Rollback via `git hash-object`** — the best decision in the plan. Dedup
  and compression free, works with a dirty tree, no bespoke format.
- **Generation by analogy** — with 46 sibling classes, the best
  value-for-effort idea in the whole document.
- **The stale-offset guard** — solves a real problem, not an imaginary one.
  *(Since validated: it would have caught all nine corruptions in the first
  calibration run.)*
- **PHP first** — correct, and the measured terrain confirms it.

---

## Proposed changes

| # | Change | Impact |
|---|---|---|
| 1 | Drop gates 2 and 3; replace with the **substance gate** | High — current safety is apparent, not real |
| 2 | Rewrite the justification: **volume, not context** | High — changes what gets built first |
| 3 | Batched application as the primary tool | High |
| 4 | `patch_group` moves to Phase 0 — the batch is the architecture | High |
| 5 | Parallelise calls, serialise writes | Medium |
| 6 | Compact diff in the response above a threshold | Medium |
| 7 | Hard symbol-size limit with explicit refusal | Medium |
| 8 | A written criterion for **when not to use Bifrost**, in the tool description | Medium |
| 9 | Build a smoke test for the target **before** bulk use | **Blocking** |
| 10 | Calibration should measure `ctx` with and without | Low, but cheap |
| 11 | Idempotency key on retries | Low |
| 12 | Document `php -l`'s limits and add the symbol-existence check | Low |

---

## The question that decided the project

Nothing here matters if the answer to this is bad:

> **When explicitly asked, can the worker return a real block of code from
> this codebase byte for byte intact?**

> ✅ **Answered: yes, 3/3.** See [calibration.md](calibration.md).
