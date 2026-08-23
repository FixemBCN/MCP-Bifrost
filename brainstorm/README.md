# The working record

This is not documentation. `docs/` is documentation — the distilled, current,
correct account of what the system does.

This directory is the journal the design was argued out in: what was
proposed, what was measured, what turned out to be wrong, and what replaced
it. It is kept because a project that only publishes its conclusions hides
the part worth learning from.

| Document | What it is |
|---|---|
| [original-spec.md](original-spec.md) | the starting proposal, left unedited |
| [plan.md](plan.md) | the design, v0.1 → v0.5, with the reasoning behind each revision |
| [critical-review.md](critical-review.md) | an adversarial pass hunting for reasons this fails — twelve findings |
| [calibration-results.md](calibration-results.md) | what the worker actually did when handed real code |

## Why keep it

Three things in here are only visible because the record survived:

**A guarantee that guaranteed nothing.** The original spec's central safety
claim was a byte-for-byte comparison of everything outside the patched
range. The review (RF-1) showed it could never fail — the server builds the
file by concatenating those bytes, so comparing them compares a string with
itself. Calibration then demonstrated it: the check reported 9/9 while three
files were left syntactically broken. It was deleted, along with the test
that verified it.

**Two findings the author got wrong.** RF-5 claimed latency made the tool
unusable interactively; measurement said 2.6 s, and it was withdrawn. RF-10
called prompt caching marginal; it was 43%, and the claim was softened. Both
corrections sit above the original text rather than replacing it.

**A bug found by building the measuring instrument.** Calibration failed 6
of 9 cases before anyone touched the worker — PHP reports byte offsets,
Python indexes strings by character, and every accented comment shifted the
cut. Silently. It would have corrupted production files had the server been
written first.

## The convention this produced

> **Every test must be able to fail.**

An assertion that holds by construction is worse than no assertion: it emits
a positive signal without inspecting anything. That rule came directly from
RF-1, and it is now the one thing this project asks of contributors.

## A note on language

These documents were written in Catalan and translated afterwards. The
arguments, the reversals and the bluntness are preserved as written —
including passages later contradicted elsewhere in the same file. Where
`docs/` and this directory disagree, `docs/` is correct and this is the
record of how it got there.
