"""
Tests for the worker client.

`worker.py` is the other end of the stack the engine-level tests skip: they
inject a fake worker object, so the HTTP client, the response parsing, the
fence stripping and the token accounting were never executed. 30 of 108
statements.

The client half runs against `StubWorkerServer` — a real socket on
localhost, real JSON over the wire — because the failures worth catching
here are HTTP failures, and a mocked `urlopen` cannot have them.

What this deliberately does not test is whether a model returns good code.
That is `calibratge/`, and it is a different question with a different
method.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp_bifrost.worker import (  # noqa: E402
    API_URL_BASE,
    MODEL_DEFAULT,
    DeepSeekWorker,
    _key_from_file,
    strip_fences,
)
from tests.support import StubWorkerServer  # noqa: E402

PAYLOAD = {"lang": "python", "sym": "K.a", "intent": "do it",
           "ctx": [], "src": "def a(self):\n    return 1", "indent": "    "}


class StripFencesTest(unittest.TestCase):
    """The worker is told not to use fences. Models do it anyway."""

    def test_a_tagged_fence_is_removed(self):
        for tag in ("python", "py", "php"):
            with self.subTest(tag=tag):
                self.assertEqual(
                    "code", strip_fences(f"```{tag}\ncode\n```"))

    def test_an_untagged_fence_is_removed(self):
        self.assertEqual("code", strip_fences("```\ncode\n```"))

    def test_an_unrecognised_tag_is_left_whole(self):
        """Stripping the closing fence without the opening one leaves a
        worse block than the one that arrived."""
        text = "```ruby\nputs 1\n```"
        self.assertEqual(text, strip_fences(text))

    def test_unfenced_text_is_returned_unchanged(self):
        text = "def a(self):\n    return 1"
        self.assertEqual(text, strip_fences(text))

    def test_a_fence_inside_the_code_is_not_a_fence(self):
        text = 'def a(self):\n    return "```"'
        self.assertEqual(text, strip_fences(text))


class KeyFromFileTest(unittest.TestCase):
    """A missing MCP server does not announce itself; it fails to appear."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.cwd)

    def write(self, text: str, name: str = ".bifrost.env") -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_plain_assignment_is_read(self):
        self.write("DEEPSEEK_API_KEY=sk-plain\n")
        os.chdir(self.dir)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual("sk-plain", _key_from_file())

    def test_export_and_quotes_and_comments_are_handled(self):
        self.write('# a comment\n\nexport DEEPSEEK_API_KEY="sk-quoted"\n')
        os.chdir(self.dir)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual("sk-quoted", _key_from_file())

    def test_other_keys_are_ignored(self):
        self.write("OTHER_KEY=nope\n")
        os.chdir(self.dir)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(_key_from_file())

    def test_an_explicit_env_file_wins(self):
        explicit = self.write("DEEPSEEK_API_KEY=sk-explicit\n", "elsewhere.env")
        self.write("DEEPSEEK_API_KEY=sk-cwd\n")
        os.chdir(self.dir)
        with mock.patch.dict(os.environ,
                             {"BIFROST_ENV_FILE": str(explicit)}, clear=True):
            self.assertEqual("sk-explicit", _key_from_file())

    def test_the_search_walks_up_from_the_working_directory(self):
        self.write("DEEPSEEK_API_KEY=sk-parent\n")
        deep = self.dir / "a" / "b"
        deep.mkdir(parents=True)
        os.chdir(deep)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual("sk-parent", _key_from_file())

    def test_no_file_anywhere_is_not_an_error(self):
        os.chdir(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(_key_from_file())


class ConfigurationTest(unittest.TestCase):
    def setUp(self):
        os.chdir(tempfile.mkdtemp())   # away from any real .bifrost.env

    def test_a_remote_endpoint_without_a_key_refuses_and_says_how(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as caught:
                DeepSeekWorker()
        message = str(caught.exception)
        self.assertIn("DEEPSEEK_API_KEY", message)
        self.assertIn(".bifrost.env", message)
        self.assertIn("BIFROST_WORKER_BASE_URL", message)

    def test_a_local_endpoint_needs_no_key(self):
        """Demanding a credential the local server will ignore would block
        the one configuration where the code never leaves the machine."""
        with mock.patch.dict(os.environ, {}, clear=True):
            worker = DeepSeekWorker(base_url="http://localhost:11434/v1")
        self.assertEqual("", worker.api_key)

    def test_the_environment_is_consulted_in_order(self):
        with mock.patch.dict(os.environ,
                             {"BIFROST_WORKER_API_KEY": "sk-first",
                              "DEEPSEEK_API_KEY": "sk-second"}, clear=True):
            self.assertEqual("sk-first", DeepSeekWorker().api_key)
        with mock.patch.dict(os.environ,
                             {"DEEPSEEK_API_KEY": "sk-second"}, clear=True):
            self.assertEqual("sk-second", DeepSeekWorker().api_key)

    def test_the_defaults_are_deepseek(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk"}, clear=True):
            worker = DeepSeekWorker()
        self.assertEqual(API_URL_BASE, worker.base_url)
        self.assertEqual(MODEL_DEFAULT, worker.model)

    def test_a_trailing_slash_on_the_base_url_is_dropped(self):
        """`http://localhost:11434/v1/` is how the documentation for half
        the local servers writes it, and concatenating would produce
        `//chat/completions`."""
        with mock.patch.dict(os.environ, {}, clear=True):
            worker = DeepSeekWorker(base_url="http://localhost:11434/v1/")
        self.assertEqual("http://localhost:11434/v1", worker.base_url)


class RunTest(unittest.TestCase):
    """Against a real socket: the failures worth catching are HTTP ones."""

    def worker(self, stub, **kwargs):
        return DeepSeekWorker(base_url=stub.base_url, **kwargs)

    def test_a_good_answer_comes_back_whole(self):
        block = "def a(self):\n    return 2"
        with StubWorkerServer([{"out": block, "why": "bumped it",
                                "diff_stat": "+1/-1"}]) as stub:
            result = self.worker(stub).run(PAYLOAD)

        self.assertTrue(result.ok, result.error)
        self.assertEqual(block, result.out)
        self.assertEqual("bumped it", result.why)
        self.assertEqual("+1/-1", result.diff_stat)
        self.assertIsNone(result.error)
        self.assertEqual((100, 50, 0),
                         (result.tokens_in, result.tokens_out, result.cache_hit))
        self.assertGreater(result.request_bytes, 0)
        self.assertGreater(result.response_bytes, 0)
        self.assertGreaterEqual(result.ms, 0)

    def test_the_request_carries_the_contract_the_system_prompt_describes(self):
        with StubWorkerServer([{"out": "x"}]) as stub:
            self.worker(stub, model="a-model").run(PAYLOAD)
            body = stub.requests[0]["body"]

        self.assertEqual("a-model", body["model"])
        self.assertEqual(0.0, body["temperature"])
        self.assertEqual({"type": "json_object"}, body["response_format"])
        self.assertEqual("system", body["messages"][0]["role"])
        self.assertIn("OUTPUT KEYS", body["messages"][0]["content"])
        self.assertEqual("/chat/completions", stub.requests[0]["path"])

    def test_the_key_travels_as_a_bearer_token(self):
        with StubWorkerServer([{"out": "x"}]) as stub:
            self.worker(stub, api_key="sk-secret").run(PAYLOAD)
        self.assertEqual("Bearer sk-secret",
                         stub.requests[0]["authorization"])

    def test_fences_are_stripped_from_the_answer(self):
        with StubWorkerServer([{"out": "```python\ndef a(self):\n    return 2\n```"}]) as stub:
            result = self.worker(stub).run(PAYLOAD)
        self.assertEqual("def a(self):\n    return 2", result.out)

    def test_an_http_error_keeps_the_body_for_the_diagnosis(self):
        """Rate limits and bad keys explain themselves in the body, and
        throwing it away turns a five-second fix into a guess."""
        with StubWorkerServer(['{"error": {"message": "rate limit exceeded"}}'],
                              status=429) as stub:
            result = self.worker(stub).run(PAYLOAD)

        self.assertFalse(result.ok)
        self.assertIn("429", result.error)
        self.assertIn("rate limit exceeded", result.error)
        self.assertIsNone(result.out)

    def test_an_unreachable_endpoint_is_reported_not_raised(self):
        worker = DeepSeekWorker(base_url="http://127.0.0.1:1", timeout=2)
        result = worker.run(PAYLOAD)
        self.assertFalse(result.ok)
        self.assertTrue(result.error)
        self.assertIsNone(result.out)

    def test_content_that_is_not_json_is_refused(self):
        with StubWorkerServer([{"__content__": "I am afraid I cannot do that"}]) as stub:
            result = self.worker(stub).run(PAYLOAD)
        self.assertFalse(result.ok)
        self.assertIn("not parseable", result.error)

    def test_an_answer_without_out_is_refused(self):
        with StubWorkerServer([{"why": "I forgot the code"}]) as stub:
            result = self.worker(stub).run(PAYLOAD)
        self.assertFalse(result.ok)
        self.assertIn("out", result.error)

    def test_an_out_that_is_not_a_string_is_refused(self):
        """A model returning `{"out": ["line", "line"]}` would otherwise
        reach the gates as a list and fail somewhere less informative."""
        with StubWorkerServer([{"__content__": '{"out": ["a", "b"]}'}]) as stub:
            result = self.worker(stub).run(PAYLOAD)
        self.assertFalse(result.ok)
        self.assertIn("not a string", result.error)
        self.assertIn("list", result.error)

    def test_usage_figures_survive_a_bad_answer(self):
        """The call was still paid for, so it still has to be accounted."""
        with StubWorkerServer([{"__content__": "not json"}]) as stub:
            result = self.worker(stub).run(PAYLOAD)
        self.assertFalse(result.ok)
        self.assertEqual((100, 50), (result.tokens_in, result.tokens_out))
