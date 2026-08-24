# Changelog

Kept by hand, in the same spirit as the rest of the documentation: what
changed, and why it was wrong before.

Versions follow [semantic versioning](https://semver.org/). Dates are the day
the version was tagged.

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

Coverage: 57% → 66% measured in-process, and higher in truth — the
end-to-end tests run the server in a child process, where the collector does
not follow.

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
