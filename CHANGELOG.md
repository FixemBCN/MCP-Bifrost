# Changelog

Kept by hand, in the same spirit as the rest of the documentation: what
changed, and why it was wrong before.

Versions follow [semantic versioning](https://semver.org/). Dates are the day
the version was tagged.

---

## 0.1.8 — 2026-08-29

### Changed — the hook gates the case the reminder kept losing: editing an adapted file

0.1.7 denied one shape, a `Write` creating a new file among established
siblings. Everything else — every `Edit` — still got `additionalContext`
and nothing more. A second field session showed what that costs. Working in
a project with this server configured, the orchestrator was asked directly
whether it was using the tools, admitted it was not, loaded the schemas,
made exactly one `fix_symbols` call, and then hand-edited a new switch
case, two new functions beside existing ones and two full method rewrites
in `.php` files — with the reminder firing on every one of those calls.

The lesson is not that the reminder was badly worded. It had already been
read, agreed with, and acted on once in that same session. Advisory text
does not survive the next decision, so the gate stops depending on it.

`hook_guard` now denies a second trigger: any `Edit` or `Write` to an
**existing** file whose extension this server adapts (`ADAPTED_EXTENSIONS`,
currently `.php` and `.py`), when the file sits inside a project whose
`.mcp.json` registers a server named `bifrost`. The
`permissionDecisionReason` maps each edit shape to the tool that does it —
`insert_case`, `insert_symbol`, `fix_symbol`, `fix_symbols`, `fix_range`,
`patch_group` — rather than saying "use bifrost", and reminds the reader
the gate applies to every later edit in the session, not just this one.

Two things keep this from being a nuisance:

- **Scope is read off disk.** `_bifrost_project_root` walks up from the
  target for a `.mcp.json` that actually declares this server. A `.php` or
  `.py` file in a project that never configured mcp-bifrost is never
  blocked, so the hook is safe to install globally (`init-hook --global`).
  Detection is by configuration rather than a live connection because a
  `PreToolUse` hook is a short-lived process with no view of the client's
  MCP session.
- **There is an exit.** `touch <project>/.bifrost/hook-override` lets the
  next manual edit through and is consumed on use, so it buys one edit and
  not a silent session-wide opt-out. A gate with no exit is one that gets
  removed wholesale the first time it is wrong.

`ADAPTED_EXTENSIONS` is duplicated in `hooks.py` rather than imported from
`engine.ADAPTERS`, because this module loads on every `Edit` and `Write` in
the session and `engine` pulls the worker, the gates and the patcher in
behind it. `AdaptedExtensionsSyncTests` asserts the two stay equal, so
adding an adapter and forgetting the constant fails a test instead of
quietly leaving the new language ungated.

### Fixed — the new-file gate denied extensions `create_file` cannot produce

0.1.7's sibling gate keyed on "three or more files share this extension"
without asking whether the extension was one this server adapts. Writing a
new `.md` file into a directory of other `.md` files was therefore denied
outright and told to use `create_file`, which has no Markdown adapter and
would have rejected it — a block with no way through it. Found while
testing the 0.1.8 gate against a real project, where it blocked an ordinary
planning document. The gate now requires `ADAPTED_EXTENSIONS` as well.

---

## 0.1.7 — 2026-08-27

### Changed — RF-13: the hook gates the one mechanical case, and stops arguing against itself

Field evidence from a full build session on another project (Argos,
2026-08-27): 54 files created, 2 via Bifrost (3.7%), zero
`fix_symbol`/`fix_symbols`/`patch_group`/`insert_symbol`, with 0.1.6's hook
installed and firing 50+ times without blocking anything. Same failure the
hook was shipped to fix — see docs/critical-review.md, RF-13, for the full
finding.

`hook-guard` now reads the `PreToolUse` payload from stdin and denies one
narrow, mechanical case outright instead of only nudging: a `Write` to a
file that does not exist yet, in a directory where three or more siblings
already share its extension. The denial names the suggested
`create_file(model_from=<nearest sibling>)` in `permissionDecisionReason`.
Everything else stays advisory — the trigger only fires where the hook
payload alone proves the case, with no judgment call involved.

`fix_symbol` and `fix_symbols`' descriptions previously told the model to
hand-edit in exactly the case that matters most: "you already read the
code, so edit it yourself." Since the orchestrator's workflow is always
read-then-edit, that exclusion swallowed nearly every real case. Reworded to
argue from what a manual edit gives up (offset re-resolution, a syntax
check before writing, a rollback blob) rather than from context already
spent.

### Added — optional post-write `verify` on `create_file` and `fix_symbol`

Every gate before this one checks that a write is syntactically sound, not
that it is correct. In the same Argos session, a `create_file`-generated
test invented function signatures that did not exist and asserted the
opposite of the specified contract — caught only because the orchestrator
happened to run the suite afterwards. `verify` closes that gap on purpose:
pass a shell command, run with the repo root as its working directory, and
a nonzero exit reverts the write (deletes the file for `create_file`,
restores the pre-patch blob for `fix_symbol`) and returns the command's
output instead of reporting success.

---

## 0.1.6 — 2026-08-25

### Added — `mcp-bifrost init-hook`, so the deferred-tool nudge ships with the package

A client that defers a tool's schema shows it to the model as a bare name
until something asks for it, and repeating the same mechanical edit several
times in a row does not, by itself, prompt that ask. That is exactly what
happened over a full session of real use: `insert_case` sat unused while the
same switch-case pattern got hand-edited over and over, and two of those
hand-edits collided on the same file — the exact failure `insert_case`'s
label-anchoring exists to prevent. The tool was only found because someone
asked what it did.

The fix so far lived only in one person's global Claude Code config: a
`PreToolUse` hook on `Edit`/`Write` that reminds the client to check for a
fitting Bifrost tool before hand-editing. `mcp-bifrost init-hook` (project-
level by default, `--global` for the user-level file) now writes that hook
into `.claude/settings.json` directly — additively, so any hooks already
there are left alone, and idempotently, so running it twice is a no-op.
`mcp-bifrost hook-guard` is what the hook itself invokes. README's Quick
start now points at it right next to the `.mcp.json` snippet.

---

## 0.1.5 — 2026-08-24

### Fixed — the package did not import on Python 3.11

`patcher.py` built two strings as `f"+{block.count(b'\n') + 1}/-0"`. A
backslash inside an f-string expression is a `SyntaxError` before Python
3.12, which PEP 701 changed, and this package declares 3.11 as its minimum
and lists it in its classifiers. So `mcp_bifrost` did not fail a test on
3.11 — it did not load at all, for anyone, on the oldest version it claims
to support.

Nothing running locally could have found it: the author's interpreter is
3.14, where the line is ordinary. The CI matrix added in 0.1.2 found it on
its first run, which is the entire argument for a badge that reports a
workflow rather than a number typed by hand.

`ast.parse(..., feature_version=(3, 11))` does not detect it either — the
3.12 tokenizer reads f-strings its own way whatever version is asked of it —
so the guard checks the construct instead, walking each module's tree and
reading every interpolated expression back out of the source. It names both
lines that caused this one.

**0.1.4 was tagged but never published.** It carries the same defect; use
this one.

---

## 0.1.4 — 2026-08-24

### Changed — a class is a symbol

The PHP symbol map listed methods and nothing else. A class had no address,
so `insert_symbol` could not add one: the `symbol_set` gate saw a class
arrive with its methods, counted names rather than declarations, refused,
and rolled the write back — which made the refusal look like a gate catching
something real. The tool description had promised classes the whole time,
and the Python adapter had emitted `ClassDef` from the start, so the two
languages disagreed about what could be addressed.

`extract.php` now emits `class`, `interface`, `trait` and `enum` as symbols
in their own right, each spanning its whole declaration from the first
modifier to the closing brace, with its methods still addressable underneath
as `Class::method`. `Symbol` carries a `kind` to say which it is.

What that makes possible:

- `insert_symbol` can add a class, as documented.
- `fix_symbol` can rewrite one, because gate 2 now counts what a block
  declares at its own level rather than every name it contributes. A class
  with eight methods is nine names and one symbol.
- `end_of_class` anchored to a class appends a member to *that* class — the
  direct way to say "add a method to Foo". It lands after the last member,
  at the indentation its siblings use; an empty class is filled against the
  top of its body, including the one-line `class Marker {}` shape, where the
  insertion has to break the line itself.

### Fixed — `fix_range` produced code at column zero

The escape hatch, and the one tool whose offsets are computed by hand from
line numbers rather than handed over by a parser. It had no test.

A range began at the start of its first line, and so included that line's
indentation; a symbol's `start_byte` does not — it points at
`public function`, not at the whitespace before it. `apply_indent` pads every
line *but* the first for exactly that reason, so the strip-and-restore round
trip did not hold, `normalise` gave up as designed, and the block went to the
worker with its real indentation and an empty `indent` field. The worker,
following the system prompt, returned it at column zero, and that is where it
was spliced.

In Python gate 1 caught it every time — `expected an indented block` — which
means `fix_range` simply did not work on indented code. In PHP, where
indentation carries no meaning to the parser, it passed the gates and
flattened the range. The range now starts after the first line's indentation,
as a symbol does.

Overshooting the end of the file also reported one line more than the file
has: splitting on the newline leaves a phantom element after a trailing one,
and it was being counted.

### Fixed — three defects the container change exposed

- **The container stack never popped.** The condition was `depth > $depth`,
  which is never true for a class declared at the top level, so the stack
  only ever grew and the newest class shadowed the rest. It looked right for
  classes in sequence and was wrong for everything after the last one: a
  top-level function following a class was reported as a method of it, and a
  method of an anonymous class was adopted by whichever named class preceded
  it. Containers are now tracked by extent, which is exact.

- **PHP 8 attributes were not attached to anything.** `#[Route('/users')]`
  above a method is the routing table, not decoration, and an insertion
  `before` that method went between the two — passing every gate, because
  the file still parses and the symbol set is unchanged, and silently giving
  the attribute to a different method. Attributes now join the docblock in
  the declaration's preamble: outside the symbol's own bytes, so the worker
  never sees them and cannot lose them, and insertions go above them.

- **A class with no methods counted as no symbols**, so `create_file`
  refused a file whose class held only constants as "defining no symbols".

### Added

- `tests/test_containers.py` — the symbol map for every container kind,
  extents proved by slicing each one out and running `php -l` on it, the two
  attribution regressions, gate 2's counting, inserting a class, and
  `end_of_class` against a container in both languages.
- `tests/test_range_revert.py` — `fix_range` and `revert_patch`, the last two
  tools nothing exercised. Including the case the tool exists for: code that
  lives outside any symbol, where a parser offers no address at all.

285 tests, coverage 82%.

---

## 0.1.3 — 2026-08-24

### Fixed — three defects found by driving the server instead of reading it

Line coverage before this release was 57%, and the gap was not spread
evenly: `server.py` (the only thing a client ever talks to), `worker.py`,
`languages/python.py` and `vcs.py` had no test that executed them at all.
The engine-level tests inject a fake worker, which skips both ends of the
stack. Driving the real server over stdio, with a stub OpenAI-compatible
endpoint in place of the model, turned up three bugs in the first twenty
minutes.

- **Notifications were answered.** Only `notifications/initialized` was
  recognised as a notification; every other one — `cancelled`, `progress`,
  `roots/list_changed` — fell through to the unknown-method branch and got
  an error object back with `"id": null`. JSON-RPC 2.0 forbids replying to
  a notification, and the consequence is worse than noise: a client reading
  one response per request takes the stray error as the answer to its next
  call, and from there every tool result is attributed to the wrong
  request. Any request without an `id` is now silently accepted.

- **Every patcher refusal surfaced as an internal `TypeError`.** From the
  point the worker answers, `common` carries a `rationale`; four error
  handlers passed a second one as a keyword, so `log.record()` raised
  `TypeError: got multiple values for keyword argument 'rationale'` from
  inside the handler. The message that was being swallowed is the one a new
  user is most likely to hit — *"not inside a git repository. Rollback
  depends on git's object store, so patching outside one is refused."* All
  nine `record()` calls of that shape now merge instead of colliding.

- **`insert_symbol` could not insert a class.** The `symbol_set` gate
  required exactly one new name in the symbol map, and a class arrives with
  its methods: nine names for one insertion. The write was rolled back
  afterwards, so the refusal looked like a gate catching something real. It
  now counts roots rather than names, which still catches both things the
  gate exists for — a block that closes its enclosing class early re-parents
  the symbols below it and registers as a loss, and two siblings where one
  was asked for are two roots.

- **Every Python insertion violated PEP 8.** New symbols were spliced in
  with a single newline on each side, the same for every language and every
  nesting level, so a new top-level class arrived welded to the line above
  it. Each adapter now states its own gap — two blank lines between
  top-level definitions and one between methods for Python, one at every
  level for PHP — and the gap is measured against what is already at the
  seam, since `end_of_file` sits just past a newline while `after` sits at
  the end of a line's text. Adding a fixed count to both gave one of them a
  blank line too many.

- **`state().dirty` truncated the first filename it reported.** `_git()`
  stripped whitespace from every result, which is right for the one-line
  answers it mostly returns and wrong for `git status --porcelain`, whose
  first column is the status code and legitimately begins with a space for
  an unstaged modification. Stripping ate that space, and the fixed-width
  slice that follows then removed the first character of the path: `a.txt`
  came back as `.txt`. Only the first line was affected, which is the shape
  of bug that survives casual testing.

- **`open_pr` shelled out to `which` to find out whether `gh` exists.**
  On a machine without `which` — minimal containers have none — that failed
  with the same errno 2 it was trying to report. `shutil.which` needs no
  subprocess at all.

- **`strip_fences` removed one fence and not the other.** A fence tagged
  with anything but php/python/py kept its opening line and lost its
  closing one, which is a worse block than the one that arrived.

- **A trailing slash on the worker endpoint produced `//chat/completions`.**
  `http://localhost:11434/v1/` is how the documentation for most local
  servers writes it, urllib sends the doubled path verbatim, and a good many
  gateways answer 404. The base URL is normalised now.

- **`HTTPError` was read but never closed**, leaking a socket once per
  failed worker call.

- **Python adapter, two address bugs.** A decorator written `@ deco` (legal
  Python) left the `@` outside the symbol, handing the worker a block that
  began with a stray space. And a method of a nested class was addressed as
  `Inner.method`, the same address a top-level `Inner` would produce in the
  same file; it is now `Outer.Inner.method`.

### Added

- `tests/test_e2e.py` — a real server subprocess over stdio, a stub
  OpenAI-compatible worker endpoint served from the test process, and a real
  git repository. Includes the first test of `fix_symbols`, the parallel
  batch path: three symbols in one file, each replacement a different length
  from the original, so every write moves the offsets of the ones after it.
  That is the most dangerous code in the project and it had none.
- `tests/test_server.py` — the protocol layer: version negotiation, the
  notification regression, and a check that every tool in `TOOLS` is
  dispatchable and marshals its arguments in the declared order.
- `tests/test_python_adapter.py` — byte offsets under a multibyte prefix,
  decorators, qualified names, what is addressable and what is deliberately
  not.
- `tests/support.py` grows `StubWorkerServer`, which records the payloads it
  is sent, making the worker contract testable in both directions.
- `tests/test_vcs.py` — the layer that decides what goes into a commit, and
  the first code in the project that leaves the machine. Real repositories
  throughout, including a real bare remote for `push`, because a mocked push
  proves only that the mock ran.
- `tests/test_docgen.py` — the log's filters and the three documents it
  produces. The whole argument for recording a rationale per patch rests on
  it coming back out intact.
- `tests/test_worker.py` — fence stripping, key discovery, endpoint
  configuration and every HTTP failure path, against a real socket rather
  than a mocked `urlopen`: the failures worth catching here are HTTP
  failures, and a mock cannot have them.

Coverage: 57% → 74% measured in-process, and higher in truth — the
end-to-end tests run the server in a child process, where the collector does
not follow. `server.py` 0% → 80%, `languages/python.py` 20% → 93%,
`vcs.py` 28% → 84%, `docgen.py` 20% → 81%, `worker.py` 28% → 99%.
241 tests. What remains under-covered is `engine.py` at 57%: `fix_range`
and the revert paths have no test of their own.

---

## 0.1.2 — 2026-08-24

### Fixed — a clean clone reported 53 failures on any machine without PHP

**The defect.** The suite shelled out to two binaries that are not Python
dependencies and therefore cannot be declared in `pyproject.toml`: `php`,
which *is* the PHP parser (`extract.php` runs on `token_get_all()`, PHP's own
tokenizer), and `git`, which the VCS tools drive for real. Neither had a
guard. On the author's machine both were installed and all 128 tests passed,
which is exactly why this survived to release.

Anyone cloning the repository without the PHP CLI — that is, most people who
work in Python or JavaScript — got:

```
Ran 128 tests in 0.612s
FAILED (failures=15, errors=38)
```

53 failures, a wall of `FileNotFoundError: [Errno 2] ... 'php'`, next to a
README badge that read **128 passing**. The reasonable conclusion from that
screen is that the project is broken or the badge is dishonest. Neither was
true, and neither was discoverable without reading the tracebacks.

The worst single case was `tests/test_heimdall.py`, which failed with
`AssertionError: 'resolve' != 'heimdall'` and no mention of PHP anywhere. The
fixture is a `config.php`; extraction died before the secret gate was ever
reached. It read as a broken security gate. It was a missing binary.

**The fix.**

- `tests/support.py` provides `requires_php` and `requires_git`, two
  `skipUnless` guards. 53 tests carry the first, 37 the second (27 carry
  both). Without PHP the suite now ends in `OK (skipped=53)` — the honest
  statement that the PHP half was not proven on this machine, rather than a
  false claim in either direction.
- A missing binary now says so. `PhpBinaryMissing` and `GitBinaryMissing`
  replace the bare errno at the five `php` call sites and the `git` funnel,
  with the install command in the message. Previously the engine surfaced
  `[Errno 2] No such file or directory: 'php'` as a `resolve` gate failure,
  which named the wrong culprit.
- CI (`.github/workflows/tests.yml`) runs two jobs: Python 3.11/3.12/3.13
  **with** `php`, asserting that nothing is skipped, and Python 3.13 **with
  php removed from PATH**, asserting that the skips are present. The second
  job is what stops this regressing: a guard that quietly stopped firing
  would make the PHP half look tested on a machine with no PHP.
- The README badge now reports that workflow instead of a number typed by
  hand. A badge that cannot go red is decoration.

### Added

- `VersionConsistencyTest`: the version is declared in four places by hand
  (`pyproject.toml`, `server.json` twice, `serverInfo`) and the registry only
  complains days later. Two tests now assert that the four agree and that
  `CHANGELOG.md` leads with the declared version.

**Not changed.** No test was weakened or deleted, and no behaviour of the
server changed beyond the error messages. With both binaries present the
result is what it was, plus the two version checks below: 130 passing.

**Credit.** Reported by a reader who did the one thing the author could not:
cloned it onto a machine that was not the author's.

---

## 0.1.1 — 2026-08-23

- Registry ownership marker (`mcp-name`) in the README, required for PyPI
  verification.
- `server.json` on the current schema, description within the registry's
  100-character limit.
- Absolute links in the README so the PyPI project page renders correctly.

## 0.1.0 — 2026-08-23

First release. AST-driven targeted patching over MCP: symbol extraction
through each language's own parser (`token_get_all()` for PHP, `ast` for
Python), a chain of gates between the worker model and the disk, atomic
application, an operational log outside the orchestrator's context, and the
Heimdall secret gate in front of every outbound call. PHP and Python targets.
Packaged for PyPI and listed in the official MCP registry.
