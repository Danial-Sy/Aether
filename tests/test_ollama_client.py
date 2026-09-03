# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
import unittest

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
