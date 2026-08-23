# Calibration results — 2026-08-23

9 cases, real methods from `src/calc/` in the target codebase,
`deepseek-chat`, `temperature=0`, synthetic schema from the spec.

## Verdict

**The premise holds up.** DeepSeek does the job the project wants to hand
it, and it does it well. But the calibration uncovered **a serious design
bug that had nothing to do with the worker**, and refuted two of my own
claims in the critical review.

## Final run

```
json_ok        9/9      valid JSON with out/why/diff_stat
offsets_ok     9/9      the block on disk is what we sent
php_ok         9/9      the resulting file passes php -l
sig_ok         9/9      signatura intacta
perimetre_ok   9/9      (see the note — this means nothing)
indent_ok      8/9
identitat_ok   3/3      ← the question that decided the project
cap_perdua     3/3      no original line lost on additive tasks
fences         0/9      never wrapped output in markdown
latency        mean 2,567 ms, range 1,304–3,615 ms
tokens         in 6,190 (43% served from cache) / out 2,979
```

---

## 🔴 Finding 1 — Byte offsets vs character indexes

**The first run failed 6 of 9 cases. None of the failures were DeepSeek's
fault.**

PHP's `token_get_all()` returns **byte offsets**. Python was applying them
to a `str`, which is indexed **by characters**. Every Catalan accent
before the symbol shifts the cut:

```
extract.php says:  start_byte=1965
ShiftCalc.php:  4,506 bytes  /  4,504 characters   <- 2 accented chars

slice on str    → 'blic function closeShift($shiftId) {'
slice on bytes  → 'public function closeShift($shiftId) {'
```

Two characters eaten. In files with more accents before the symbol, the
shift goes as far as erasing the whole word `function` — and then not
even the signature check knew what to look at.

**Why it's serious and not anecdotal:**

- the target codebase has **Catalan comments everywhere**. It's not an
  edge case: it's the normal case. Practically any file in the project
  triggers it.
- It's **silent**. The code doesn't blow up: it trims the start of the
  block and adds bytes from the end. The result often still looks like
  code.
- It happens **before** any call to the worker. No prompt, no model, and
  no retry can fix it.

**Rule for the MCP server: the entire path extraction → payload → splice
→ write works in `bytes`.** It's decoded to `str` only to send it to the
worker and re-encoded on receipt. Any `read_text()` in this path is a bug
waiting for a file with accents.

**And it confirms RF-1 empirically**, better than any argument could
have: in the first run, `perimetre_ok` gave **9/9 while 3 files were left
syntactically broken**. The gate said "perfect" about destroyed code. It's
exactly what RF-1 predicted, observed.

And the gate that does detect it —`offsets_ok`, the §4.3 guard— is the
one I added after writing RF-1. It's now validated with data: the first
run would have given `offsets_ok` false on all 9 cases.

---

## 🟢 Finding 2 — The question that decided the project: affirmative answer

> When explicitly asked, does DeepSeek know how to return a real block of
> code from the target codebase byte-for-byte intact?

**Yes. 3/3, byte for byte, without a single difference.**

Also, in the additive tasks, **3/3 without losing a single original
line** — the failure mode RF-2 identified as the real risk didn't
manifest in this sample. This doesn't rule it out (9 cases is few, and
they were methods of 5-28 lines), but it takes the urgency off the
substance gate: it goes from "essential on day 1" to "needed before mass
use".

Other observations:
- **0/9 with markdown fences.** The system prompt rule works.
- **9/9 with `why` and `diff_stat`.** The synthetic schema is fully
  honored.
- **9/9 valid JSON.** `response_format=json_object` is reliable.

---

## 🟡 Finding 3 — Indentation does slip (1/9)

`ShiftCalc::createShift`, task `phpdoc`: everything correct —compiles, no
losses, signature intact— except the returned block doesn't start with
the same indentation as the original.

It's cosmetic, but in a 35,000-line project accumulated cosmetics is
debt. And it's worth noting that it's **the same problem §11 anticipated
for Python**, showing up in PHP.

**Mitigation confirmed as correct:** normalize the block's indentation to
0 before sending it and reapply it on receipt. This way the worker never
has to get it right. The decision to keep the `indent` field in the
payload from day 1 is now justified with data, not by caution.

---

## ❌ Refutation 1 — RF-5 was false: latency is not a problem

I wrote: "5-30s per call, Bifrost is slower than doing it directly, so it
has to be a batch tool".

**Measured: average 2.6s, maximum 3.6s.** It's practically the same as
Claude takes to do an `Edit`. The latency argument against interactive
use **does not hold up** and must be withdrawn.

RF-4's argument (context savings are only large at volume) remains valid
and still recommends the batch. But now it's for economy, not speed — and
that's weaker. A tool that doesn't penalize time can also be used
interactively at no cost.

---

## ❌ Refutation 2 — RF-10 was too harsh on prompt caching

I wrote that caching would be "marginal" because it only covers the
system prompt.

**Measured: 2,688 of 6,190 input tokens served from cache — 43%.**

Much more than I anticipated. The cacheable prefix includes more than I
thought, and in a batch where the system prompt repeats N times the
saving is real. It doesn't change any architecture decision, but the
sentence "don't design anything assuming a gain from it" was excessive.

---

## What needs to be done differently in the plan

| # | Change | Source |
|---|---|---|
| 1 | **The entire data path in `bytes`.** `read_text()` forbidden in extraction/splice. | Finding 1 |
| 2 | `offsets_ok` (the §4.3 guard) is **the** critical gate, not an improvement. | Finding 1 |
| 3 | Remove the perimeter gate and the test that verifies it. | Finding 1, confirms RF-1 |
| 4 | Normalize indentation to 0 in the payload; `indent` field mandatory. | Finding 3 |
| 5 | Withdraw RF-5. Latency is not an argument. | Refutation 1 |
| 6 | Soften RF-10. Caching delivers ~43%. | Refutation 2 |
| 7 | Substance gate: downgraded from "day 1" to "before mass use". | Finding 2 |

---

## Cost

9 calls, 6,190 input tokens and 2,979 output. Cents.

The run that uncovered the bytes bug cost the same, and it would have
been a whole day of confusing debugging if the bug had been found with
the MCP server already written and patching production files.

## Reproduce

```bash
export DEEPSEEK_API_KEY=...
python3 calibratge/calibra.py --casos 9
```
