# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
import json
import unittest
from unittest.mock import patch

import ollama_client as oc


class OllamaMessageContractTests(unittest.TestCase):
    def test_tool_results_use_ollama_tool_name_field(self):
        normalized = oc.normalize_chat_messages([
            {"role": "tool", "name": "read_file", "content": "file contents"},
        ])

        self.assertEqual(normalized, [{
            "role": "tool",
            "content": "file contents",
            "tool_name": "read_file",
        }])

    def test_assistant_thinking_is_replayed_with_tool_calls(self):
        calls = [{
            "type": "function",
            "function": {"index": 0, "name": "read_file", "arguments": {"path": "app.py"}},
        }]
        normalized = oc.normalize_chat_messages([{
            "role": "assistant",
            "content": "",
            "thinking": "I need the file.",
            "tool_calls": calls,
        }])

        self.assertEqual(normalized[0]["thinking"], "I need the file.")
        self.assertEqual(normalized[0]["tool_calls"], calls)


if __name__ == "__main__":
    unittest.main()


class ThinkingCapabilityTests(unittest.IsolatedAsyncioTestCase):
    """Ollama 400s on think=true for a model without the capability.

    Verified live: {"error": "\"qwen2.5:0.5b\" does not support thinking"}.
    """

    def _fake_client(self, responses):
        calls = []

        class Resp:
            def __init__(self, status, body):
                self.status_code = status
                self._body = body

            async def aread(self):
                return self._body.encode()

            async def aiter_lines(self):
                yield json.dumps({"message": {"content": "ok"}, "done": True})

        class Stream:
            def __init__(self, payload):
                calls.append(json.loads(json.dumps(payload)))
                self._resp = Resp(*responses[min(len(calls) - 1, len(responses) - 1)])

            async def __aenter__(self):
                return self._resp

            async def __aexit__(self, *a):
                return False

        class Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def stream(self, method, url, json=None):
                return Stream(json)

        return Client, calls

    async def _run(self, responses, **kwargs):
        Client, calls = self._fake_client(responses)
        with patch.object(oc.httpx, "AsyncClient", Client):
            out = [c async for c in oc.stream_chat("m", [{"role": "user", "content": "hi"}], **kwargs)]
        return out, calls

    async def test_a_thinking_rejection_is_retried_without_thinking(self):
        out, calls = await self._run(
            [(400, '{"error":"\\"m\\" does not support thinking"}'), (200, "")],
            think=True,
        )

        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["think"])
        self.assertNotIn("think", calls[1])
        self.assertTrue(out)

    async def test_the_thinking_key_is_dropped_once_not_retried_forever(self):
        Client, calls = self._fake_client([(400, "does not support thinking")])
        with patch.object(oc.httpx, "AsyncClient", Client), self.assertRaises(RuntimeError):
            [c async for c in oc.stream_chat("m", [{"role": "user", "content": "hi"}], think=True)]

        # First call carries think; nothing after it does, so the branch is not a loop.
        self.assertTrue(calls[0]["think"])
        self.assertFalse(any("think" in c for c in calls[1:]))

    async def test_think_false_is_still_sent_since_every_model_accepts_it(self):
        _, calls = await self._run([(200, "")], think=False)

        self.assertIs(calls[0]["think"], False)

    async def test_an_unrelated_400_still_grows_the_window_instead(self):
        _, calls = await self._run([(400, "context too small"), (200, "")],
                                   options={"num_ctx": 8192})

        self.assertEqual(calls[1]["options"]["num_ctx"], 12288)
