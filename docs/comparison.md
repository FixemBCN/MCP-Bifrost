# Where this sits

**All third-party claims on this page were verified on 2026-08-23.** Sources
are linked. This kind of page rots — models ship new versions, tools add
features — so treat anything here as a snapshot of that date and check the
links before relying on it. If something has gone stale,
[open an issue](https://github.com/FixemBCN/MCP-Bifrost/issues); a wrong
comparison is worse than none.

## The question every tool in this space answers differently

What does the expensive model have to produce, and does that text pass
through its context?

| | Orchestrator produces | Edit text in its context? | What guarantees the write |
|---|---|---|---|
| [Aider](https://aider.chat) | the code itself | yes | search/replace matching, a git commit per change |
| [Serena](https://github.com/oraios/serena) | the code itself | yes | symbol resolution through a language server |
| Fast-apply ([Morph](https://morphllm.com), [Relace](https://relace.ai)) | a lazy edit snippet | yes, minus unchanged regions | a small merge model |
| **MCP-Bifrost** | an instruction | **no** | parser + gates, deterministic splice |

The axis that matters is the third column. Aider and Serena reduce what the
model has to **read**. Fast-apply reduces what it has to **write out**.
Bifrost is built so the replacement code never enters the orchestrator's
context at all: it says "make every one of these 202 methods return a typed
DTO" and never sees a line of the result unless it asks for one.

We are not claiming that property is unique — this is a fast-moving space
and we have not surveyed all of it. We are claiming it is the property
Bifrost is designed around, and that it is what the trade-offs below buy.

This is also not really a competition. Fast-apply solves the **merge**;
Bifrost solves the **delegation**. Those are different halves of the same
problem, and nothing stops you using both.

## On guarantees

A fast-apply model merges a snippet into a file, so the merge itself is
probabilistic. Morph publishes ~96% for V3 Fast and claims 98% first-pass
accuracy — **their own figures, from their own benchmark**
([source](https://www.morphllm.com/fast-apply-model)). We have not
independently tested them and are not disputing them.

The structural point does not depend on those numbers. Bifrost's merge
cannot fail because there is no merge: the file is rebuilt as
`original[:start] + block + original[end:]`, with offsets from the
language's own parser. Everything outside the target range is preserved by
construction rather than by a check — which is exactly why the check that
was originally specified for it was deleted
([RF-1](critical-review.md)).

The probabilistic part is confined to one place: the worker writing the
block. In front of it:

| Gate | Default |
|---|---|
| byte-identity of the source region against what was sent | **on** |
| syntax of the rebuilt file (`php -l`, `ast.parse`) | **on** |
| exactly one symbol in the returned block | **on** |
| substance — calls, variables and control flow that vanished silently | **off** |

**Three are on by default, not four.** The substance gate is a coarse
regex-based check that never fired during calibration, and a gate that
rejects good patches is worse than one waiting to be armed. Turn it on with
`substance_gate=True` before bulk work. Saying "four gates protect you" when
the fourth ships disabled would be the same overstatement RF-1 exists to
prevent.

## Where the others are better

**Language coverage.** Serena supports 40+ languages through the language
server protocol. Bifrost supports two, because it insists on each language's
own official parser rather than a common grammar. Adding one means writing
an adapter, not rewriting the core — but that is a promise, not a feature.

**Latency.** Fast-apply is an order of magnitude quicker, and the comparison
is not apples to apples: their number is a merge, ours (~2.6 s) is a
generation. If your loop is latency-sensitive, this is the wrong shape of
tool regardless.

**Maturity.** Aider and Serena have real user bases and years of edge cases
found the hard way. This project is days old and its test suite, however
carefully built, has been exercised against one production codebase.

## When not to use Bifrost

**Exploratory work.** "Find why this crashes" is not an instruction Bifrost
can execute. It needs to know which symbols to touch before it starts.

**Single small edits.** The token arithmetic is marginal here and we say so
([RF-4](critical-review.md)). Use your agent's normal edit tool.

**Latency-sensitive loops.** ~2.6 s per block.

**Anything that is not PHP or Python.**

**Coordinated cross-file refactors where the shape of one edit depends on
the outcome of another.** `patch_group` gives you atomicity, not sequencing.

**Codebases with no way of telling you something broke.** Every gate here
checks form. None understands meaning. See
[the manual](manual.md#the-part-you-should-not-skip).

---

Bifrost is for one shape of work: **one instruction, many symbols, at a
volume where reading them all would exceed the context window.** Outside
that shape, something else on this page is probably the better tool.
