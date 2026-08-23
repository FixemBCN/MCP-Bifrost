# Calibration results — 2026-08-23

9 cases, real methods from the target PHP codebase, `deepseek-chat`,
`temperature=0`, the compact schema from the original spec.

## Verdict

**The premise holds.** The worker does the job the project wants to hand it,
and does it well. But calibration exposed **a serious design bug that had
nothing to do with the worker**, and refuted two claims from the
[critical review](critical-review.md).

## Final run

```
json_ok        9/9      valid JSON with out/why/diff_stat
offsets_ok     9/9      the block on disk is what we sent
php_ok         9/9      resulting file passes php -l
sig_ok         9/9      signature unchanged
perimeter_ok   9/9      (see note — this means nothing)
indent_ok      8/9
identity_ok    3/3      ← the question that decided the project
no_loss        3/3      no original line lost on additive tasks
fences         0/9      never wrapped output in markdown
latency        mean 2,567 ms, range 1,304–3,615 ms
tokens         in 6,190 (43% served from cache) / out 2,979
```

---

## 🔴 Finding 1 — Byte offsets vs character indices

**The first run failed 6 of 9 cases. Not one failure was the worker's.**

PHP's `token_get_all()` returns **byte offsets**. Python was applying them
to a `str`, which is indexed **by character**. Every accented character
before the symbol shifts the cut:

```
extract.php says:  start_byte=1965
the file:          4,506 bytes / 4,504 characters   ← 2 accented chars

slice on str    → 'blic function closeShift($shiftId) {'
slice on bytes  → 'public function closeShift($shiftId) {'
```

Two characters eaten. In files with more accented characters before the
symbol, the drift swallows the word `function` entirely — at which point
even the signature check no longer knew what it was looking at.

**Why this is serious rather than anecdotal:**

- The target codebase has **comments in Catalan throughout**. This is not an
  edge case; it is the normal case. Nearly every file in the project
  triggers it.
- It is **silent**. Nothing crashes: it trims the start of the block and
  appends bytes from past the end. The result often still looks like code.
- It happens **before** any worker call. No prompt, no model and no retry
  can fix it.

**Rule for the server: the entire path — extraction → payload → splice →
write — works in `bytes`.** Decode to text only to hand it to the worker,
re-encode on the way back. Any `read_text()` on that path is a bug waiting
for a file with an accent in it.

**And it confirms RF-1 empirically**, better than any argument could: in the
first run `perimeter_ok` reported **9/9 while three files were left
syntactically broken**. The gate declared "perfect" over destroyed code.
Exactly what RF-1 predicted, observed.

The gate that *does* catch it — `offsets_ok`, the stale-range guard — was
added after RF-1 was written. It is now validated against data: the first
run would have failed `offsets_ok` on all nine cases.

---

## 🟢 Finding 2 — The deciding question: yes

> When explicitly asked, can the worker return a real block of code from
> this codebase byte for byte intact?

**Yes. 3/3, byte for byte, not one difference.**

Also, on the additive tasks, **3/3 with no original line lost** — the
failure mode RF-2 identified as the real risk did not appear in this
sample. That does not rule it out (nine cases, methods of 5–28 lines), but
it does take the urgency out of the substance gate: it moves from
"indispensable on day one" to "required before bulk use".

Other observations:

- **0/9 wrapped in markdown fences.** The system-prompt rule works.
- **9/9 returned `why` and `diff_stat`.** The compact schema is respected in
  full.
- **9/9 valid JSON.** `response_format=json_object` is reliable.

---

## 🟡 Finding 3 — Indentation does slip (1/9)

One `phpdoc` case: everything correct — compiles, nothing lost, signature
intact — except the returned block does not start at the original
indentation.

Cosmetic, but in a 35,000-line codebase accumulated cosmetic drift is debt.
And it is worth noting this is **the same problem the plan anticipated for
Python**, showing up first in PHP.

**Mitigation confirmed as correct:** normalise the block's indentation to
zero before sending, re-apply it on return. The worker then never has to
get it right. Keeping an `indent` field in the payload from day one is now
justified by data rather than by caution.

---

## ❌ Refutation 1 — RF-5 was wrong: latency is not a problem

The review claimed "5–30 s per call, so Bifrost is slower than doing it
directly, and therefore must be a batch-only tool".

**Measured: 2.6 s mean, 3.6 s max.** That is roughly what an orchestrator's
own edit costs. The latency argument against interactive use **does not
hold** and is withdrawn.

RF-4's argument (the context saving is only large in volume) still stands
and still recommends batching. But it now rests on economics rather than
speed — which is a weaker case. A tool that costs nothing in time can be
used interactively too.

---

## ❌ Refutation 2 — RF-10 was too harsh on prompt caching

The review called caching "marginal" because it only covers the system
prompt.

**Measured: 2,688 of 6,190 input tokens served from cache — 43%.**

Considerably more than predicted. The cacheable prefix covers more than
assumed, and across a batch where the system prompt repeats N times the
saving is real. It changes no architectural decision, but "design nothing
assuming a gain from it" was overstated.

---

## What the plan has to do differently

| # | Change | Source |
|---|---|---|
| 1 | **The whole data path in `bytes`.** No `read_text()` in extraction or splice. | Finding 1 |
| 2 | `offsets_ok` is **the** critical gate, not an enhancement. | Finding 1 |
| 3 | Drop the perimeter gate and the test that verified it. | Finding 1, confirms RF-1 |
| 4 | Normalise indentation to zero in the payload; `indent` field mandatory. | Finding 3 |
| 5 | Withdraw RF-5. Latency is not an argument. | Refutation 1 |
| 6 | Soften RF-10. Caching contributes ~43%. | Refutation 2 |
| 7 | Substance gate drops from "day one" to "before bulk use". | Finding 2 |

All seven are incorporated in [the architecture](architecture.md).

---

## Cost

9 calls, 6,190 input tokens and 2,979 output. Pennies.

The run that exposed the byte bug cost the same, and would have been a full
day of confused debugging had the bug surfaced with the MCP server already
written and patching production files.

## Reproducing

```bash
export DEEPSEEK_API_KEY=...
python3 calibratge/calibra.py --cases 9
```

Raw output is written to `calibratge/resultats/`, which is git-ignored: it
embeds fragments of the target codebase.
