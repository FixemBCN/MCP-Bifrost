# Changelog

Kept by hand, in the same spirit as the rest of the documentation: what
changed, and why it was wrong before.

Versions follow [semantic versioning](https://semver.org/). Dates are the day
the version was tagged.

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

### Fixed — three defects the change exposed

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

269 tests, coverage 80%.

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
