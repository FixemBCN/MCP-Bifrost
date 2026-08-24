# Changelog

Kept by hand, in the same spirit as the rest of the documentation: what
changed, and why it was wrong before.

Versions follow [semantic versioning](https://semver.org/). Dates are the day
the version was tagged.

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
