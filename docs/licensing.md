# Licensing

Two separate questions: what MCP-Bifrost **consumes**, and what MCP-Bifrost
**grants**.

---

## 1. What we consume

### Model Context Protocol

| Component | License | Holder |
|---|---|---|
| MCP Python SDK (`modelcontextprotocol/python-sdk`) | **MIT** | Anthropic, PBC (2024) |
| MCP specification | MIT | Anthropic, PBC |

*Verified 2026-08-23 against the SDK's `LICENSE` file.*

**MIT is permissive and imposes no copyleft.** It places no restriction on
the license we choose for our own code, and no obligation to publish
anything. The only requirement is that we **retain the MIT notice and
copyright** when we distribute anything containing the SDK.

There is therefore **no conflict** between depending on the MCP SDK and
releasing MCP-Bifrost under a source-available, non-resale license.

Obligation: ship `NOTICE` with the MIT text alongside any distribution.

### A choice worth making deliberately

MCP is a **specification**, not a mandatory library. The protocol is
JSON-RPC 2.0 over stdio, and a minimal server is implementable with the
Python standard library alone.

Right now this repository has **zero third-party dependencies** —
`calibratge/` runs on stdlib plus the `php` binary. Adopting the SDK adds
exactly one MIT dependency.

| | With the SDK | Hand-rolled JSON-RPC |
|---|---|---|
| Dependencies | 1 (MIT, permissive) | 0 |
| Protocol conformance | maintained upstream | our problem |
| Effort | low | moderate, and ongoing |
| Compliance burden | one notice file | none |

**Recommendation: use the SDK.** MIT is the lowest-friction license that
exists; one notice file is not a burden worth writing a protocol
implementation to avoid, and tracking spec revisions by hand is real
recurring cost. Zero dependencies is a nice property, not a goal.

### Other components

| Component | Relationship | License impact |
|---|---|---|
| PHP binary (`php -l`, `token_get_all`) | invoked as a subprocess | **None.** Calling a program is not linking. |
| DeepSeek API | network service | Not a code-license matter. Governed by their terms of service — see the data-handling note in the architecture doc. |
| the target codebase codebase | the target of patching | Ours. No third-party obligation. |

### On the name

The project is named for what it is: an MCP server. Building on the Model
Context Protocol is deliberate and stated openly, not something to be
downplayed.

"MCP" and "Model Context Protocol" originate with Anthropic. Descriptive use
is normal and expected; what matters is that the naming never reads as an
official component or an endorsement. The protective measure is therefore
not a rename but an explicit disclaimer, carried in `README.md` and `NOTICE`:

> MCP-Bifrost is an independent project. It is not affiliated with,
> endorsed by, or sponsored by Anthropic, PBC.

---

## 2. What we grant

**[Apache License 2.0](../LICENSE).** Open source, OSI-approved.

Use it, modify it, redistribute it, build a business on it, sell it. The
only obligations are to keep the notices and to state what you changed.

### How this was decided

The original requirement was: *modification and use permitted, company use
fine, but it must not be legally sellable.*

**No OSI-approved open source license does that.** Every one of them — MIT,
Apache-2.0, GPL, AGPL — explicitly permits sale. The freedom to sell is part
of the definition of open source, not an oversight in it. A no-resale
license is **source-available**, a different category (MariaDB, Sentry,
Elastic and HashiCorp all sit there).

So this was a genuine either/or, and the trade was made deliberately:

| | Source-available (PolyForm Shield) | Permissive (Apache-2.0) |
|---|---|---|
| Resale | blocked | **allowed** |
| GitHub license badge | none — shows "Other" | yes |
| Corporate legal review | some departments block non-OSI outright | no friction |
| External contributions | fewer, some decline on principle | normal |
| Package ecosystems requiring OSI | excluded | included |

**Reach won.** For a developer tool whose value grows with the number of
people using and improving it, obscurity is a worse outcome than someone
else selling it.

### Why Apache-2.0 rather than MIT

Both give identical reach. Apache-2.0 adds two things MIT lacks:

1. **An explicit patent grant** (§3). Contributors license their patent
   claims to users, and anyone who sues over patents loses their license.
   MIT is silent on patents, which leaves a real ambiguity.
2. **Trademark protection** (§6). It grants no rights to the licensor's
   names or marks — which matters here, because the project name is meant to
   stay "MCP-Bifrost" and a fork should not inherit the right to use it.

It is also the de facto standard for infrastructure and developer tooling,
which is exactly this category.

### What this means in practice

| Scenario | Permitted |
|---|---|
| Read, study, fork, modify | ✅ |
| Run it commercially, internally or otherwise | ✅ |
| Redistribute, with or without changes | ✅ |
| Sell it, or a rebranded version | ✅ — this is the accepted cost |
| Offer it as a paid hosted service | ✅ |
| Use the "MCP-Bifrost" name for a fork | ❌ trademark, §6 |
| Remove the copyright and NOTICE | ❌ §4 |

### Obligations when redistributing

- Include a copy of `LICENSE`.
- Include `NOTICE` (Apache-2.0 §4(d) makes this binding once a NOTICE file
  exists).
- State significant changes in modified files.

### Before publishing

- [ ] ~~Confirm the copyright holder~~ — done: Arnau Ferrerons Manich.
- [ ] Add an `Apache-2.0` license header to source files, or decide
      deliberately not to. The `LICENSE` file alone is sufficient; per-file
      headers are convention, not requirement.
- [ ] **Rewrite or squash git history.** Earlier commits contain fragments
      of the target production codebase (method signatures, paths) and, in
      one document, the filenames of its credential store. Both were removed
      from the working tree, but history still carries them and this
      repository is going public.
- [ ] No CLA needed. Apache-2.0 contributions arrive under Apache-2.0 by
      §5, so there is nothing to reconcile.
