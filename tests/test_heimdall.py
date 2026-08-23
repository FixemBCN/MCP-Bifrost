"""
Test suite for MCP-Bifrost's secret gate (heimdall.py), spending limits
(budget.py), and how the engine wires both in front of the worker.

stdlib unittest only. Every assertion here is meant to be able to fail: no
check is included that would hold purely by construction of the code path
under test.

Run: python3 -m unittest discover tests -v   (from the repo root)
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp_bifrost import heimdall  # noqa: E402
from mcp_bifrost.heimdall import Finding  # noqa: E402
from mcp_bifrost.budget import Budget, BudgetExceeded  # noqa: E402
from mcp_bifrost.engine import Engine, Outcome  # noqa: E402
from mcp_bifrost.worker import WorkerResult  # noqa: E402


# ===================================================================== #
#  Fixtures / helpers shared by the whole file
# ===================================================================== #

# One realistic, structurally-valid, obviously-fake positive sample per
# pattern in heimdall.PATTERNS. Keyed by pattern name so tests can be
# driven from this single table instead of duplicating literals.
POSITIVE_SAMPLES: dict[str, bytes] = {
    "private-key": b"-----BEGIN RSA PRIVATE KEY-----",
    "aws-access-key": b'$id = "AKIAABCDEFGHIJKLMNOP";',
    "google-api-key": b'$k = "AIzaSyD-FAKE1234567890fakefake123456789";',
    "gcp-service-account": b'{"type": "service_account", "project_id": "x"}',
    "slack-token": b'$t = "xoxb-1234567890-abcdefghijklmnop";',
    "github-token": b'$t = "gh' + b"p_" + b"a" * 36 + b'";',
    "stripe-key": b'$t = "sk_live_' + b"a" * 16 + b'";',
    "openai-style-key": b'$t = "sk-' + b"a" * 32 + b'";',
    "jwt": b"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature",
    "bearer-token": b"Authorization: Bearer " + b"a" * 20,
    "connection-string": b"postgres://dbuser:hunter2xyzzy@dbhost:5432/prod",
    "credential-literal": b'$password = "hunter2xyzzy";',
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
    )


def make_git_repo(root: Path) -> Path:
    """Init a git repo at `root` with a local identity."""
    proc = _git(root, "init", "-q")
    assert proc.returncode == 0, proc.stderr
    _git(root, "config", "user.email", "bifrost-tests@example.com")
    _git(root, "config", "user.name", "Bifrost Tests")
    return root


def commit_file(repo: Path, rel_path: str, content: bytes) -> Path:
    path = repo / rel_path
    path.write_bytes(content)
    proc = _git(repo, "add", rel_path)
    assert proc.returncode == 0, proc.stderr
    proc = _git(repo, "commit", "-q", "-m", "fixture")
    assert proc.returncode == 0, proc.stderr
    return path


def ok_result(out: str, tokens_in: int = 5, tokens_out: int = 5) -> WorkerResult:
    return WorkerResult(
        ok=True, out=out, why="stub", diff_stat="+0/-0", error=None, ms=1,
        tokens_in=tokens_in, tokens_out=tokens_out, cache_hit=0,
        request_bytes=0, response_bytes=0,
    )


class EchoWorker:
    """
    Records every payload it was asked to run and returns it verbatim as a
    successful WorkerResult (out == payload['src']). Never touches the
    network. Used to test gate wiring, not the worker's own transform.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, payload: dict) -> WorkerResult:
        self.calls.append(payload)
        return ok_result(payload["src"])


# ===================================================================== #
#  1 + 2. Pattern coverage, and the property that findings never leak
# ===================================================================== #

class PatternDetectionTest(unittest.TestCase):
    def test_every_pattern_has_a_covering_sample(self):
        # If a pattern is added to heimdall.PATTERNS without a matching
        # entry here, this catches the coverage gap directly instead of
        # silently skipping it.
        pattern_names = {name for name, _, _ in heimdall.PATTERNS}
        self.assertEqual(pattern_names, set(POSITIVE_SAMPLES))

    def test_each_pattern_fires_on_its_sample(self):
        for name, sample in POSITIVE_SAMPLES.items():
            with self.subTest(pattern=name):
                findings = heimdall.scan(sample, entropy=False)
                fired = [f.pattern for f in findings]
                self.assertIn(name, fired,
                              f"{name} did not fire on {sample!r} -> {fired}")

    def test_patterns_do_not_cross_fire_on_the_wrong_sample(self):
        # A sample for one pattern should not silently satisfy an unrelated
        # pattern's regex too, which would mask a pattern that is actually
        # broken.
        for name, sample in POSITIVE_SAMPLES.items():
            with self.subTest(pattern=name):
                findings = heimdall.scan(sample, entropy=False)
                fired = {f.pattern for f in findings}
                # jwt and bearer-token can legitimately co-occur with other
                # shapes in principle, but none of our fixed samples should
                # trip more than the two adjacent, expected patterns.
                self.assertLessEqual(len(fired), 2, f"{name} -> {fired}")


class SecretsNeverLeakTest(unittest.TestCase):
    """
    The critical property. A gate that logs what it caught has moved the
    secret, not stopped it.
    """

    def test_finding_fields_never_contain_the_secret(self):
        for name, sample in POSITIVE_SAMPLES.items():
            with self.subTest(pattern=name):
                findings = heimdall.scan(sample, entropy=False)
                self.assertTrue(findings, f"{name} produced no findings at all")
                secret_text = sample.decode("utf-8")
                for f in findings:
                    self.assertIsInstance(f, Finding)
                    self.assertNotIn(secret_text, f.pattern)
                    self.assertNotIn(secret_text, f.hint)
                    self.assertNotIn(secret_text, str(f))

    def test_describe_never_contains_the_secret(self):
        for name, sample in POSITIVE_SAMPLES.items():
            with self.subTest(pattern=name):
                findings = heimdall.scan(sample, entropy=False)
                message = heimdall.describe(findings)
                self.assertNotIn(sample.decode("utf-8"), message)

    def test_scan_payload_findings_and_describe_never_leak_via_context(self):
        secret = POSITIVE_SAMPLES["aws-access-key"]
        findings = heimdall.scan_payload(
            "return 1;", context=[secret.decode("utf-8")], entropy=False
        )
        self.assertTrue(findings)
        message = heimdall.describe(findings)
        secret_text = secret.decode("utf-8")
        for f in findings:
            self.assertNotIn(secret_text, f.pattern)
            self.assertNotIn(secret_text, f.hint)
            self.assertNotIn(secret_text, str(f))
        self.assertNotIn(secret_text, message)


# ===================================================================== #
#  3. Line numbers
# ===================================================================== #

class LineNumberTest(unittest.TestCase):
    def test_secret_on_line_four_is_reported_at_line_four(self):
        content = "\n".join([
            "// line 1",
            "// line 2",
            "// line 3",
            '$id = "AKIAABCDEFGHIJKLMNOP";',
            "// line 5",
        ]).encode("utf-8")
        findings = heimdall.scan(content, entropy=False)
        aws_findings = [f for f in findings if f.pattern == "aws-access-key"]
        self.assertEqual(len(aws_findings), 1)
        self.assertEqual(aws_findings[0].line, 4)

    def test_two_secrets_on_different_lines_report_different_lines(self):
        content = "\n".join([
            '$a = "AKIAABCDEFGHIJKLMNOP";',
            "// filler",
            "// filler",
            '$b = "sk-' + "a" * 32 + '";',
        ]).encode("utf-8")
        findings = heimdall.scan(content, entropy=False)
        lines = sorted(f.line for f in findings)
        self.assertEqual(lines, [1, 4])


# ===================================================================== #
#  4. _INNOCENT suppression
# ===================================================================== #

class InnocentSuppressionTest(unittest.TestCase):
    def test_example_and_placeholder_style_lines_are_not_flagged(self):
        # Suppression applies to the HEURISTIC patterns only. See the
        # fail-open test below for why the vendor formats are exempt.
        innocent_lines = [
            b'$password = "placeholder";',
            b"$secret = 'changeme';",
            b"$secret = 'xxxxxxxxxxxx';",
            b'$secret = "your_secret_here";',
        ]
        for line in innocent_lines:
            with self.subTest(line=line):
                self.assertEqual(heimdall.scan(line, entropy=False), [])

    def test_same_shape_without_the_innocent_marker_is_still_flagged(self):
        # Guards against a suppression regex broad enough to eat everything:
        # dropping the marker word must bring the finding back.
        self.assertEqual(
            heimdall.scan(b"$secret = 'changeme';", entropy=False), []
        )
        findings = heimdall.scan(b"$secret = 'hunter2xyzzy';", entropy=False)
        self.assertTrue(findings)

    def test_placeholder_word_cannot_hide_a_high_confidence_secret(self):
        """
        Suppression silences the whole LINE, so without the HIGH_CONFIDENCE
        exemption a real key could ride along behind the word "example":

            $prod = "AKIA...."; // see example below

        That is fail-open — noise reduction hiding a real credential — and it
        is the one direction this gate must never be wrong in.
        """
        riding_along = [
            b'$prod = "AKIAIOSFODNN7XYZABCD"; // see example below',
            b'$dsn = "mysql://user:realpw@db.internal/app"; # example',
            b'$k = "-----BEGIN PRIVATE KEY-----"; // placeholder',
        ]
        for line in riding_along:
            with self.subTest(line=line):
                self.assertTrue(
                    heimdall.scan(line, entropy=False),
                    "a placeholder word suppressed a high-confidence pattern",
                )

    def test_high_confidence_set_covers_every_vendor_pattern(self):
        """A new vendor pattern added without joining HIGH_CONFIDENCE would
        be silently suppressible. Heuristics are the only exemptions."""
        heuristic = {"credential-literal"}
        names = {n for n, _, _ in heimdall.PATTERNS}
        self.assertEqual(names - heimdall.HIGH_CONFIDENCE, heuristic)


# ===================================================================== #
#  5. Entropy check
# ===================================================================== #

class EntropyTest(unittest.TestCase):
    # A long, dense, random-looking token with no recognised shape.
    HIGH_ENTROPY_LINE = (
        b'$blob = "u8jzPde0IgxLd6GncfBAepfJBd0Kh8oOOL8dKLzdocJ2isAj";'
    )
    ORDINARY_CODE_LINE = (
        b"function calculateTotalPriceForShoppingCartItemsList($items) {"
    )
    ORDINARY_PROSE_LINE = (
        b"// This function totals every item currently in the shopping cart."
    )

    def test_high_entropy_string_is_flagged_when_entropy_enabled(self):
        findings = heimdall.scan(self.HIGH_ENTROPY_LINE, entropy=True)
        self.assertTrue(
            any(f.pattern == "high-entropy" for f in findings),
            findings,
        )

    def test_high_entropy_string_is_not_flagged_when_entropy_disabled(self):
        findings = heimdall.scan(self.HIGH_ENTROPY_LINE, entropy=False)
        self.assertFalse(any(f.pattern == "high-entropy" for f in findings))

    def test_ordinary_code_does_not_trip_the_threshold(self):
        # Guards against a threshold set so low it flags everything with a
        # long identifier.
        findings = heimdall.scan(self.ORDINARY_CODE_LINE, entropy=True)
        self.assertEqual(findings, [])

    def test_ordinary_prose_does_not_trip_the_threshold(self):
        findings = heimdall.scan(self.ORDINARY_PROSE_LINE, entropy=True)
        self.assertEqual(findings, [])


# ===================================================================== #
#  6. scan_payload / context labeling
# ===================================================================== #

class ContextLabelingTest(unittest.TestCase):
    def test_finding_from_context_is_labelled_with_its_index(self):
        secret = '$id = "AKIAABCDEFGHIJKLMNOP";'
        findings = heimdall.scan_payload(
            "return 1;",
            context=["// nothing here", secret, "// nothing here either"],
            entropy=False,
        )
        self.assertTrue(findings)
        for f in findings:
            self.assertIn("[context 1]", f.hint)
            self.assertNotIn("[context 0]", f.hint)
            self.assertNotIn("[context 2]", f.hint)

    def test_finding_from_src_carries_no_context_label(self):
        secret = '$id = "AKIAABCDEFGHIJKLMNOP";'
        findings = heimdall.scan_payload(secret, context=["// clean"],
                                         entropy=False)
        self.assertTrue(findings)
        for f in findings:
            self.assertNotIn("[context", f.hint)


# ===================================================================== #
#  7. Clean code
# ===================================================================== #

class CleanCodeTest(unittest.TestCase):
    def test_clean_php_produces_no_findings(self):
        clean = b"""<?php

class Greeter
{
    public function greet(string $name): string
    {
        $msg = "hello " . $name;
        return $msg;
    }
}
"""
        self.assertEqual(heimdall.scan(clean, entropy=True), [])


# ===================================================================== #
#  8-10. Budget
# ===================================================================== #

class BudgetCheckTest(unittest.TestCase):
    def test_passes_below_both_limits(self):
        b = Budget(max_calls=5, max_tokens=1000)
        b.spend(100, 100)
        b.check()  # must not raise

    def test_raises_at_call_limit(self):
        b = Budget(max_calls=2, max_tokens=10_000)
        b.spend(1, 1)
        b.spend(1, 1)
        with self.assertRaises(BudgetExceeded) as cm:
            b.check()
        self.assertIn("call", str(cm.exception).lower())

    def test_raises_at_token_limit(self):
        b = Budget(max_calls=1000, max_tokens=50)
        b.spend(30, 20)  # total == 50, at the limit
        with self.assertRaises(BudgetExceeded) as cm:
            b.check()
        self.assertIn("token", str(cm.exception).lower())

    def test_call_limit_and_token_limit_are_independent(self):
        # High token budget must not mask a tripped call limit, and vice
        # versa.
        calls_only = Budget(max_calls=1, max_tokens=1_000_000)
        calls_only.spend(0, 0)
        with self.assertRaises(BudgetExceeded):
            calls_only.check()

        tokens_only = Budget(max_calls=1_000_000, max_tokens=10)
        tokens_only.spend(10, 0)
        with self.assertRaises(BudgetExceeded):
            tokens_only.check()


class BudgetSpendTest(unittest.TestCase):
    def test_spend_accumulates(self):
        b = Budget()
        b.spend(100, 50)
        b.spend(20, 5)
        self.assertEqual(b.calls, 2)
        self.assertEqual(b.tokens_in, 120)
        self.assertEqual(b.tokens_out, 55)

    def test_spend_tolerates_none_token_counts(self):
        b = Budget()
        b.spend(None, None)
        self.assertEqual(b.calls, 1)
        self.assertEqual(b.tokens_in, 0)
        self.assertEqual(b.tokens_out, 0)
        b.spend(10, None)
        self.assertEqual(b.tokens_in, 10)
        self.assertEqual(b.tokens_out, 0)


class BudgetSummaryTest(unittest.TestCase):
    def test_summary_reflects_actual_usage(self):
        b = Budget(max_calls=10, max_tokens=5000)
        b.spend(100, 50)
        b.spend(25, 25)
        self.assertEqual(b.summary(), "2/10 calls, 200/5,000 tokens")


# ===================================================================== #
#  11-13. Engine wiring
# ===================================================================== #

CONFIG_PHP = b"""<?php

class Config
{
    public function secretHolder(): string
    {
        $key = "AKIAABCDEFGHIJKLMNOP";
        return $key;
    }
}
"""

# The literals planted in CONFIG_PHP, used to assert none of them reaches
# the worker once redaction is on.
SECRET_LITERALS = ["AKIAABCDEFGHIJKLMNOP"]

COUNTER_PHP = b"""<?php

class Counter
{
    public function methodOne(): int
    {
        return 1;
    }

    public function methodTwo(): int
    {
        return 2;
    }
}
"""


class EngineWiringTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = make_git_repo(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def make_engine(self, worker, **kwargs) -> Engine:
        db_path = Path(self._tmp.name) / "log.sqlite3"
        return Engine(worker=worker, db_path=db_path, **kwargs)


class SecretBlocksSendTest(EngineWiringTestBase):
    def test_secret_blocks_before_worker_is_ever_called(self):
        """With redaction disabled, a finding blocks outright."""
        path = commit_file(self.repo, "config.php", CONFIG_PHP)
        worker = EchoWorker()
        engine = self.make_engine(worker, redact_secrets=False)

        outcome = engine.fix_symbol(
            str(path), "Config::secretHolder", "add a docblock"
        )

        self.assertIsInstance(outcome, Outcome)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.gate, "heimdall")
        # The whole point of the gate: blocking must happen before the send,
        # not after it.
        self.assertEqual(worker.calls, [])
        engine.close()

    def test_redaction_lets_the_work_through_without_the_secret(self):
        """
        With redaction on (the default) the patch proceeds, and the property
        that actually matters is unchanged: the worker never sees the secret.
        """
        path = commit_file(self.repo, "config.php", CONFIG_PHP)
        before = path.read_bytes()
        worker = EchoWorker()
        engine = self.make_engine(worker)

        outcome = engine.fix_symbol(
            str(path), "Config::secretHolder", "add a docblock"
        )

        self.assertTrue(outcome.ok, outcome.message)
        self.assertTrue(worker.calls, "the worker should have been called")
        sent = json.dumps(worker.calls[-1])
        for secret in SECRET_LITERALS:
            self.assertNotIn(secret, sent,
                             "a secret reached the worker despite redaction")
        self.assertIn("__BIFROST_SECRET_", sent,
                      "nothing was actually redacted — test is not armed")
        # And the secret is back on disk, byte for byte.
        self.assertEqual(path.read_bytes(), before)
        engine.close()

    def test_worker_dropping_the_placeholder_is_refused(self):
        """
        The failure mode that makes redaction dangerous if unchecked: a
        placeholder that does not come back would be written to disk in place
        of a live credential, silently, since the file still compiles.
        """
        path = commit_file(self.repo, "config.php", CONFIG_PHP)
        before = path.read_bytes()

        class DropsPlaceholder:
            def run(self, payload):
                import re as _re
                out = _re.sub(r"__BIFROST_SECRET_\d+__", "REDACTED",
                              payload["src"])
                return WorkerResult(
                    ok=True, out=out, why="w", diff_stat="+0/-0", error=None,
                    ms=1, tokens_in=1, tokens_out=1, cache_hit=0,
                    request_bytes=1, response_bytes=1)

        engine = self.make_engine(DropsPlaceholder())
        outcome = engine.fix_symbol(
            str(path), "Config::secretHolder", "add a docblock")

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.gate, "heimdall-restore")
        self.assertEqual(path.read_bytes(), before,
                         "the file must be untouched when restore fails")
        engine.close()


class AllowSecretsOverrideTest(EngineWiringTestBase):
    def test_allow_secrets_lets_it_through_and_logs_the_override(self):
        path = commit_file(self.repo, "config.php", CONFIG_PHP)
        worker = EchoWorker()
        engine = self.make_engine(worker)

        outcome = engine.fix_symbol(
            str(path), "Config::secretHolder", "add a docblock",
            allow_secrets=True,
        )

        self.assertTrue(outcome.ok, outcome.message)
        self.assertEqual(len(worker.calls), 1)
        self.assertIsNotNone(outcome.patch_id)

        row = engine.log.get(outcome.patch_id)
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["override"])
        self.assertIn("heimdall", row["override"])
        engine.close()


class BudgetGateTest(EngineWiringTestBase):
    def test_second_call_is_rejected_once_max_calls_is_reached(self):
        path = commit_file(self.repo, "counter.php", COUNTER_PHP)
        worker = EchoWorker()
        engine = self.make_engine(worker, budget=Budget(max_calls=1))

        first = engine.fix_symbol(str(path), "Counter::methodOne",
                                  "no-op change")
        self.assertTrue(first.ok, first.message)
        self.assertEqual(len(worker.calls), 1)

        second = engine.fix_symbol(str(path), "Counter::methodTwo",
                                   "no-op change")
        self.assertFalse(second.ok)
        self.assertEqual(second.gate, "budget")
        # Rejected before reaching the worker a second time.
        self.assertEqual(len(worker.calls), 1)
        engine.close()


if __name__ == "__main__":
    unittest.main()
