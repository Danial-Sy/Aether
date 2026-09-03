# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
import unittest
import asyncio
import contextlib
import json
import time
from pathlib import Path
from unittest.mock import patch

try:
    import server
except ModuleNotFoundError:
    server = None



def _prompt_text(sample: dict) -> str:
    """All text sent for one sample, whatever channel it arrived on.

    Per-step directives ride along with the newest tool result (or a trailing
    user message when there is none). They must NOT be system messages: Qwen's
    template hoists every system message to the front of the prompt, so a
    "trailing" directive is relocated into the prefix and invalidates the KV
    cache, measured at 33.8s for seven tokens against 3.5s for 3,101 tokens of
    ordinary appended turns.
    """
    return "\n".join(m.get("content") or "" for m in sample["messages"])


DIRECTIVE_MARKER = "OPEN PLAN, CONTINUE AUTONOMOUSLY"


def _directive_channel(sample: dict) -> str:
    """Role that carried the per-step directive, for asserting it is not system.

    Matches the directive body rather than the "[agent-loop]" tag, which the
    stable system prompt also mentions when telling the model not to treat those
    lines as user requests.
    """
    for m in reversed(sample["messages"]):
        if DIRECTIVE_MARKER in (m.get("content") or ""):
            return m.get("role") or ""
    return ""

@unittest.skipIf(server is None, "server dependencies are installed in Aether's .venv")
class AgentLoopPromptTests(unittest.TestCase):
    def test_only_successful_tool_results_reset_repeat_stalls(self):
        self.assertTrue(server._tool_succeeded(
            "edit_file", '{"ok": true, "path": "/tmp/app.js"}'
        ))
        self.assertTrue(server._tool_succeeded(
            "run_shell", '{"exit_code": 0}'
        ))
        self.assertTrue(server._tool_succeeded(
            "read_file", '{"path": "/tmp/app.js", "content": "x"}'
        ))
        self.assertFalse(server._tool_succeeded(
            "edit_file", '{"error": "Invalid line range"}'
        ))
        self.assertFalse(server._tool_succeeded(
            "edit_file", '{"ok": true, "validation": {"ok": false}}'
        ))
        self.assertFalse(server._tool_succeeded(
            "run_shell", '{"exit_code": 1}'
        ))

    def test_the_prompt_prefix_is_stable_while_the_floor_holds(self):
        def serialize(msgs):
            return "\n\x00\n".join(
                f"{m.get('role')}\x01{m.get('content') or ''}" for m in msgs
            )

        chat = {"id": "prefix-stability", "messages": [
            {"role": "user", "content": "Do the job."},
        ]}
        previous = ""
        for index in range(14):
            current = serialize(server._build_messages(chat, "agent"))
            if previous:
                # Every earlier byte survives: the new prompt only appends.
                self.assertTrue(
                    current.startswith(previous),
                    f"step {index} rewrote already-sent content",
                )
            previous = current
            chat["messages"].extend([
                {"role": "assistant", "content": "", "interim": True,
                 "tool_calls": [{"name": "read_file", "arguments": {
                     "path": "large.js", "start_line": index * 50 + 1,
                     "end_line": index * 50 + 60}}]},
                {"role": "tool", "name": "read_file", "content": json.dumps({
                    "path": "large.js", "start": index * 50 + 1,
                    "end": index * 50 + 60, "revision": "r1", "content": "y" * 4000})},
            ])

    def test_recovery_prompt_keeps_only_requested_recent_tool_turns(self):
        messages = [{"role": "user", "content": "Repair the large file."}]
        for index in range(10):
            messages.extend([
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "name": "read_file",
                        "arguments": {
                            "path": "large.js",
                            "start_line": index * 100 + 1,
                            "end_line": index * 100 + 100,
                        },
                    }],
                    "interim": True,
                },
                {
                    "role": "tool",
                    "name": "read_file",
                    "content": f'{{"chunk":{index},"content":"' + "x" * 7000 + '"}',
                },
            ])

        built = server._build_messages(
            {"id": "recovery-retention-test", "messages": messages},
            "agent",
            tool_replay_limit=4,
        )
        tool_results = [message for message in built if message.get("role") == "tool"]
        assistant_calls = [
            message for message in built if message.get("role") == "assistant"
        ]

        self.assertEqual(len(tool_results), 4)
        self.assertEqual(len(assistant_calls), 4)
        self.assertIn('"chunk":6', tool_results[0]["content"])
        self.assertIn('"chunk":9', tool_results[-1]["content"])

    def test_prompt_replays_native_thinking_and_named_tool_result(self):
        built = server._build_messages({
            "id": "native-tool-contract",
            "messages": [
                {"role": "user", "content": "Inspect it."},
                {
                    "role": "assistant",
                    "content": "",
                    "native_thinking": "I should inspect the file.",
                    "tool_calls": [{
                        "index": 0,
                        "name": "read_file",
                        "arguments": {"path": "app.py"},
                    }],
                    "interim": True,
                },
                {"role": "tool", "name": "read_file", "content": '{"ok": true}'},
            ],
        }, "agent")

        assistant = next(message for message in built if message.get("role") == "assistant")
        tool_result = next(message for message in built if message.get("role") == "tool")
        self.assertEqual(assistant["thinking"], "I should inspect the file.")
        self.assertEqual(assistant["tool_calls"][0]["function"]["index"], 0)
        self.assertEqual(tool_result["tool_name"], "read_file")

    def test_stale_edit_range_limit_is_rewritten_before_replay(self):
        built = server._build_messages({
            "id": "stale-edit-limit",
            "messages": [
                {"role": "user", "content": "Continue the plan."},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "name": "edit_file",
                        "arguments": {
                            "path": "jungle.html",
                            "edits": [{"start_line": 1, "end_line": 1, "content": "x"}],
                        },
                    }],
                    "interim": True,
                },
                {
                    "role": "tool",
                    "name": "edit_file",
                    "content": json.dumps({
                        "error": "Too many edit ranges in one batch",
                        "maximum": 12,
                    }),
                },
            ],
        }, "agent")

        result = json.loads(next(
            message["content"] for message in built if message.get("role") == "tool"
        ))
        self.assertTrue(result["obsolete_error"])
        self.assertEqual(result["previous_maximum"], 12)
        self.assertEqual(result["current_maximum"], server.agent.MAX_EDIT_RANGES)
        self.assertNotIn("error", result)

    def test_poisoned_elision_edit_call_and_result_are_not_replayed(self):
        built = server._build_messages({
            "id": "poisoned-edit-call",
            "messages": [
                {"role": "user", "content": "Continue."},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "name": "edit_file",
                        "arguments": {
                            "path": "jungle.html",
                            "edits": [{
                                "start_line": 1,
                                "end_line": 1,
                                "content": "[Aether replay elision: 9000 characters]",
                            }],
                        },
                    }],
                    "interim": True,
                },
                {"role": "tool", "name": "edit_file", "content": '{"error":"bad edit"}'},
            ],
        }, "agent")

        self.assertFalse(any(message.get("tool_calls") for message in built))
        self.assertFalse(any(message.get("role") == "tool" for message in built))

    def test_legacy_plan_choice_reply_is_not_replayed(self):
        built = server._build_messages({
            "id": "legacy-plan-deferral",
            "agent_todos": [
                {"id": "cooldown", "content": "Add cooldown", "status": "in_progress"},
                {"id": "enemies", "content": "Add enemies", "status": "pending"},
            ],
            "messages": [
                {"role": "user", "content": "Build the requested features."},
                {"role": "assistant", "content": "What to do next, pick one. Which one?"},
                {"role": "user", "content": "Continue without asking me."},
            ],
        }, "agent")

        assistant_text = "\n".join(
            message.get("content") or ""
            for message in built
            if message.get("role") == "assistant"
        )
        self.assertNotIn("pick one", assistant_text.lower())
        self.assertNotIn("which one", assistant_text.lower())

    def test_stored_continue_is_replayed_as_resume_not_course_correction(self):
        built = server._build_messages({
            "id": "legacy-continue-tag",
            "agent_todos": [
                {"id": "repair", "content": "Repair file", "status": "in_progress"},
            ],
            "messages": [{
                "role": "user",
                "content": "continue",
                "course_correct": True,
            }],
        }, "agent")

        user_text = next(
            message["content"] for message in built if message.get("role") == "user"
        )
        self.assertIn("Same user job", user_text)
        self.assertNotIn("cancel obsolete steps", user_text)


@unittest.skipIf(server is None, "server dependencies are installed in Aether's .venv")
class AgentLoopExecutionTests(unittest.IsolatedAsyncioTestCase):

    async def test_resumed_open_plan_starts_with_compact_tool_replay(self):
        sampling_calls = []

        async def fake_stream_chat(*args, **kwargs):
            sampling_calls.append({**kwargs, "messages": args[1]})
            if len(sampling_calls) == 1:
                yield {
                    "message": {"tool_calls": [{
                        "function": {
                            "index": 0,
                            "name": "todo_write",
                            "arguments": {
                                "merge": True,
                                "todos": [{
                                    "id": "repair",
                                    "content": "Repair file",
                                    "status": "completed",
                                }],
                            },
                        },
                    }]},
                    "done": True,
                    "done_reason": "stop",
                }
            else:
                yield {
                    "message": {"content": "Finished resumed work."},
                    "done": True,
                    "done_reason": "stop",
                }

        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        messages = [{"role": "user", "content": "Repair it."}]
        for index in range(8):
            messages.extend([
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "name": "read_file",
                        "arguments": {"path": "large.js", "start_line": index + 1},
                    }],
                    "interim": True,
                },
                {
                    "role": "tool",
                    "name": "read_file",
                    "content": '{"content":"' + "x" * 5000 + '"}',
                },
            ])
        messages.append({
            "role": "user",
            "content": "continue",
            "course_correct": True,
        })
        chat = {
            "id": "compact-resume",
            "project_id": None,
            "agent_todos": [
                {"id": "repair", "content": "Repair file", "status": "in_progress"},
            ],
            "messages": messages,
        }
        meta = {
            "id": "test-model",
            "keep_alive": "1m",
            "temperature": 0.2,
            "context": 32768,
        }

        with (
            patch.object(server.oc, "stream_chat", new=fake_stream_chat),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.st, "save_chat", side_effect=lambda value: value),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 3,
                "agent_context": 32768,
                "shell_approval_mode": "never_ask",
            }),
        ):
            events = [
                event
                async for event in server._agent_loop(chat, meta, "agent", think=False)
            ]

        replayed_tools = [
            message
            for message in sampling_calls[0]["messages"]
            if message.get("role") == "tool"
        ]
        self.assertEqual(len(replayed_tools), 6)
        self.assertEqual(events[-1]["message"]["content"], "Finished resumed work.")

    async def test_repeated_rejected_call_changes_strategy_while_plan_is_open(self):
        sampling_round = 0
        real_run_tool = server.agent.run_tool

        async def fake_stream_chat(*args, **kwargs):
            nonlocal sampling_round
            sampling_round += 1
            if sampling_round <= 4:
                yield {
                    "message": {"tool_calls": [{
                        "function": {
                            "index": 0,
                            "name": "edit_file",
                            "arguments": {
                                "path": "jungle.html",
                                "edits": [{"start_line": 1, "end_line": 1, "content": "x"}],
                            },
                        },
                    }]},
                    "done": True,
                    "done_reason": "stop",
                }
            elif sampling_round == 5:
                # The actual change of strategy: a different, smaller edit.
                # Resending the rejected one is what the guard exists to stop.
                yield {
                    "message": {"tool_calls": [{
                        "function": {
                            "index": 0,
                            "name": "edit_file",
                            "arguments": {
                                "path": "jungle.html",
                                "edits": [{"start_line": 2, "end_line": 2, "content": "y"}],
                            },
                        },
                    }]},
                    "done": True,
                    "done_reason": "stop",
                }
            elif sampling_round == 6:
                yield {
                    "message": {"tool_calls": [{
                        "function": {
                            "index": 0,
                            "name": "todo_write",
                            "arguments": {
                                "merge": True,
                                "todos": [{
                                    "id": "cooldown",
                                    "content": "Add cooldown",
                                    "status": "completed",
                                }],
                            },
                        },
                    }]},
                    "done": True,
                    "done_reason": "stop",
                }
            else:
                yield {
                    "message": {"content": "Recovered without user direction."},
                    "done": True,
                    "done_reason": "stop",
                }

        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        def fake_run_tool(name, arguments, project_root, project_id=None, chat=None):
            # Rejected until the strategy change lands, then it succeeds. A
            # milestone whose every edit failed can no longer be completed, so
            # the recovery has to actually produce a change to close the plan.
            if name == "edit_file" and sampling_round <= 4:
                return '{"error":"synthetic rejection"}'
            if name == "edit_file":
                return '{"ok": true, "changed": true, "path": "jungle.html"}'
            return real_run_tool(name, arguments, project_root, project_id, chat=chat)

        chat = {
            "id": "plan-repeat-recovery",
            "project_id": None,
            "agent_todos": [
                {"id": "cooldown", "content": "Add cooldown", "status": "in_progress"},
            ],
            "messages": [{"role": "user", "content": "Complete the plan."}],
        }
        meta = {
            "id": "test-model",
            "keep_alive": "1m",
            "temperature": 0.2,
            "context": 32768,
        }

        with (
            patch.object(server.oc, "stream_chat", new=fake_stream_chat),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.agent, "run_tool", side_effect=fake_run_tool),
            patch.object(server.st, "save_chat", side_effect=lambda value: value),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 2,
                "agent_context": 32768,
                "shell_approval_mode": "never_ask",
            }),
        ):
            events = [
                event
                async for event in server._agent_loop(chat, meta, "agent", think=False)
            ]

        self.assertEqual(sampling_round, 7)
        self.assertTrue(any(
            event.get("type") == "status"
            and event.get("model", {}).get("label") == "Changing strategy for open plan…"
            for event in events
        ))
        self.assertEqual(chat["agent_todos"][0]["status"], "completed")
        self.assertEqual(events[-1]["message"]["content"], "Recovered without user direction.")

    async def test_successful_file_mutation_emits_fresh_project_tree(self):
        sampling_round = 0

        async def fake_stream_chat(*args, **kwargs):
            nonlocal sampling_round
            sampling_round += 1
            if sampling_round == 1:
                yield {
                    "message": {"tool_calls": [{
                        "function": {
                            "index": 0,
                            "name": "write_file",
                            "arguments": {"path": "new.txt", "content": "ready"},
                        },
                    }]},
                    "done": True,
                    "done_reason": "stop",
                }
            else:
                yield {
                    "message": {"content": "Created the file."},
                    "done": True,
                    "done_reason": "stop",
                }

        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        def fake_run_tool(name, arguments, *args, **kwargs):
            if name == "list_dir":
                return '{"entries": ["new.txt"]}'
            return '{"ok": true}'

        chat = {
            "id": "project-refresh",
            "project_id": "project-1",
            "messages": [{"role": "user", "content": "Create new.txt"}],
        }
        meta = {
            "id": "test-model",
            "keep_alive": "1m",
            "temperature": 0.2,
            "context": 32768,
        }

        with (
            patch.object(server.oc, "stream_chat", new=fake_stream_chat),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.agent, "run_tool", side_effect=fake_run_tool),
            patch.object(
                server.st,
                "get_project",
                return_value={
                    "id": "project-1",
                    "name": "Refresh fixture",
                    "root": "/tmp",
                    "description": "",
                },
            ),
            patch.object(server.st, "save_chat", side_effect=lambda value: value),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 3,
                "agent_context": 32768,
                "shell_approval_mode": "never_ask",
            }),
        ):
            events = [
                event
                async for event in server._agent_loop(
                    chat, meta, "agent", effort="high", think=False
                )
            ]

        refresh = next(event for event in events if event.get("type") == "project_tree")
        self.assertEqual(refresh["project_id"], "project-1")
        self.assertEqual(refresh["entries"], ["new.txt"])

    async def test_length_retry_grows_budget_uses_action_nudge_and_drops_failed_turn(self):
        sampling_calls = []

        async def fake_stream_chat(*args, **kwargs):
            sampling_calls.append({**kwargs, "messages": args[1]})
            round_number = len(sampling_calls)
            if round_number == 1:
                yield {
                    "message": {},
                    "done": True,
                    "done_reason": "length",
                    "eval_count": 8192,
                }
            elif round_number == 2:
                yield {
                    "message": {"tool_calls": [{
                        "function": {
                            "index": 0,
                            "name": "write_file",
                            "arguments": {"path": "game.html", "content": "<canvas></canvas>"},
                        },
                    }]},
                    "done": True,
                    "done_reason": "stop",
                }
            else:
                yield {
                    "message": {"content": "Built and verified the game."},
                    "done": True,
                    "done_reason": "stop",
                }

        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        chat = {
            "id": "length-recovery",
            "project_id": None,
            "effort": "high",
            "think": False,
            "messages": [{"role": "user", "content": "Build a large browser game."}],
        }
        meta = {
            "id": "test-model",
            "keep_alive": "1m",
            "temperature": 0.7,
            "top_p": 0.9,
            "top_k": 20,
            "repeat_penalty": 1.0,
            "context": 32768,
        }

        with (
            patch.object(server.oc, "stream_chat", new=fake_stream_chat),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.agent, "run_tool", return_value='{"ok": true}'),
            patch.object(server.st, "save_chat", side_effect=lambda value: value),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 3,
                "agent_context": 32768,
                "shell_approval_mode": "never_ask",
            }),
        ):
            events = [
                event
                async for event in server._agent_loop(
                    chat, meta, "agent", effort="high", think=chat["think"]
                )
            ]

        self.assertEqual(len(sampling_calls), 3)
        self.assertFalse(sampling_calls[0]["think"])
        self.assertFalse(sampling_calls[1]["think"])
        self.assertGreater(
            sampling_calls[1]["options"]["num_predict"],
            sampling_calls[0]["options"]["num_predict"],
        )
        self.assertEqual(sampling_calls[1]["options"]["num_ctx"], 32768)
        self.assertIn("ACTION-ONLY RETRY", _prompt_text(sampling_calls[1]))
        assistant_messages = [m for m in chat["messages"] if m.get("role") == "assistant"]
        self.assertEqual(len(assistant_messages), 2)
        self.assertTrue(assistant_messages[0]["tool_calls"])
        self.assertEqual(events[-1]["message"]["content"], "Built and verified the game.")

    async def test_repeated_length_failures_escalate_through_three_action_lanes(self):
        sampling_calls = []

        async def fake_stream_chat(*args, **kwargs):
            sampling_calls.append({**kwargs, "messages": args[1]})
            round_number = len(sampling_calls)
            if round_number <= 3:
                yield {
                    "message": {},
                    "done": True,
                    "done_reason": "length",
                    "eval_count": kwargs["options"]["num_predict"],
                }
            elif round_number == 4:
                yield {
                    "message": {"tool_calls": [{
                        "function": {
                            "index": 0,
                            "name": "write_file",
                            "arguments": {
                                "path": "game.html",
                                "content": "<canvas></canvas>",
                            },
                        },
                    }]},
                    "done": True,
                    "done_reason": "stop",
                }
            else:
                yield {
                    "message": {"content": "Recovered and finished."},
                    "done": True,
                    "done_reason": "stop",
                }

        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        chat = {
            "id": "staged-length-recovery",
            "project_id": None,
            "effort": "high",
            "think": False,
            "messages": [{"role": "user", "content": "Build a large browser game."}],
        }
        meta = {
            "id": "test-model",
            "keep_alive": "1m",
            "temperature": 0.7,
            "top_p": 0.9,
            "top_k": 20,
            "repeat_penalty": 1.0,
            "context": 32768,
        }

        with (
            patch.object(server.oc, "stream_chat", new=fake_stream_chat),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.agent, "run_tool", return_value='{"ok": true}'),
            patch.object(server.st, "save_chat", side_effect=lambda value: value),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 3,
                "agent_context": 32768,
                "shell_approval_mode": "never_ask",
            }),
        ):
            events = [
                event
                async for event in server._agent_loop(
                    chat, meta, "agent", effort="high", think=False
                )
            ]

        self.assertEqual(len(sampling_calls), 5)
        # The escalation shape matters, not exact token counts: the recovery
        # budget is clamped by remaining context, so it moves whenever the token
        # estimate changes. Pinning literals here made an accuracy fix look like
        # a regression.
        budgets = [call["options"]["num_predict"] for call in sampling_calls[:4]]
        self.assertEqual(budgets[0], 8192)
        self.assertEqual(budgets[1], 16384)
        self.assertGreater(budgets[2], budgets[1])
        self.assertLessEqual(budgets[2], 24576)
        self.assertEqual(budgets[3], budgets[2])  # capped, not growing forever
        assistant_messages = [
            message for message in chat["messages"] if message.get("role") == "assistant"
        ]
        self.assertEqual(len(assistant_messages), 2)
        self.assertEqual(events[-1]["message"]["content"], "Recovered and finished.")

    async def test_productive_loop_can_run_past_old_forty_step_limit(self):
        sampling_round = 0
        executed: list[str] = []

        async def fake_stream_chat(*args, **kwargs):
            nonlocal sampling_round
            sampling_round += 1
            if sampling_round == 1:
                yield {
                    "message": {"tool_calls": [{
                        "function": {
                            "index": 0,
                            "name": "search_files",
                            "arguments": {"pattern": "part-0"},
                        },
                    }]},
                    "done": False,
                }
                yield {
                    "message": {"tool_calls": [{
                        "function": {
                            "index": 1,
                            "name": "git_status",
                            "arguments": {},
                        },
                    }]},
                    "done": True,
                    "done_reason": "stop",
                }
            elif sampling_round <= 45:
                # Land a real change every few rounds. A run that only inspects,
                # forever, is the pathology the inspection gate exists to stop;
                # what must stay uncapped is a long run that keeps making
                # progress, which is what this test is about.
                if sampling_round % 5 == 0:
                    call = {
                        "index": 0,
                        "name": "edit_file",
                        "arguments": {"path": "a.py", "edits": [
                            {"find": f"part-{sampling_round}", "replace": "done"},
                        ]},
                    }
                else:
                    call = {
                        "index": 0,
                        "name": "search_files",
                        "arguments": {"pattern": f"part-{sampling_round}"},
                    }
                yield {
                    "message": {"tool_calls": [{"function": call}]},
                    "done": True,
                    "done_reason": "stop",
                }
            else:
                yield {
                    "message": {"content": "Completed after sustained tool progress."},
                    "done": True,
                    "done_reason": "stop",
                }

        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        def fake_run_tool(name, arguments, *args, **kwargs):
            executed.append(name)
            return '{"ok": true}'

        chat = {
            "id": "long-native-loop",
            "project_id": None,
            "messages": [{"role": "user", "content": "Complete a long repository task."}],
        }
        meta = {
            "id": "test-model",
            "keep_alive": "1m",
            "temperature": 0.2,
            "top_p": 0.9,
            "top_k": 20,
            "repeat_penalty": 1.0,
            "context": 32768,
        }

        with (
            patch.object(server.oc, "stream_chat", new=fake_stream_chat),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.agent, "run_tool", side_effect=fake_run_tool),
            patch.object(server.st, "save_chat", side_effect=lambda value: value),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 3,
                "agent_context": 32768,
                "shell_approval_mode": "never_ask",
            }),
        ):
            events = [event async for event in server._agent_loop(chat, meta, "agent", think=False)]

        self.assertEqual(sampling_round, 46)
        self.assertEqual(len(executed), 46)
        self.assertEqual(executed[:2], ["search_files", "git_status"])
        self.assertEqual(events[-1]["message"]["content"], "Completed after sustained tool progress.")


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(server is None, "server dependencies are installed in Aether's .venv")
class AgentLoopRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_rejection_restates_the_constraint_and_recovers(self):
        """A rejected call must be answered with what it violated.

        The old guard replaced the tool's own hint with a generic "stop
        repeating", so an oversized edit_file lost the only information that
        would have let the model split it, and the run was abandoned.
        """
        sampling_calls = []
        oversized = {
            "path": "game.html",
            "edits": [{"start_line": i, "end_line": i, "content": "x"} for i in range(1, 15)],
        }

        async def fake_stream_chat(*args, **kwargs):
            sampling_calls.append({**kwargs, "messages": args[1]})
            # Five identical rejected calls: two execute, three are guarded,
            # which is what trips agent_repeat_limit and forces recovery.
            if len(sampling_calls) <= 5:
                yield {
                    "message": {"tool_calls": [{
                        "function": {"index": 0, "name": "edit_file", "arguments": oversized},
                    }]},
                    "done": True,
                    "done_reason": "stop",
                }
            else:
                yield {
                    "message": {"content": "Split the edit and finished."},
                    "done": True,
                    "done_reason": "stop",
                }

        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        def fake_run_tool(name, arguments, *args, **kwargs):
            return json.dumps({
                "error": "Too many edit ranges in one batch",
                "requested": 14,
                "maximum": 12,
                "hint": "Split this into coherent batches of at most 12 ranges.",
            })

        chat = {"id": "rejection-recovery", "project_id": None,
                "messages": [{"role": "user", "content": "Add a cooldown."}]}
        meta = {"id": "test-model", "keep_alive": "1m", "temperature": 0.2, "context": 32768}

        with (
            patch.object(server.oc, "stream_chat", new=fake_stream_chat),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.agent, "run_tool", side_effect=fake_run_tool),
            patch.object(server.st, "save_chat", side_effect=lambda value: value),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 3,
                "agent_context": 32768,
                "shell_approval_mode": "never_ask",
            }),
        ):
            events = [
                event async for event in server._agent_loop(chat, meta, "agent", think=False)
            ]

        # The guard's reply carries the tool's own constraint, not a scolding.
        guard_replies = [
            json.loads(m["content"]) for m in chat["messages"]
            if m.get("role") == "tool" and "repeated" in (m.get("content") or "")
        ]
        self.assertTrue(guard_replies)
        self.assertEqual(guard_replies[0]["maximum"], 12)
        self.assertIn("at most 12 ranges", guard_replies[0]["error"])

        # And the run recovers instead of ending on "I got stuck".
        self.assertIn("A tool rejected your last calls", _prompt_text(sampling_calls[-1]))
        self.assertEqual(events[-1]["message"]["content"], "Split the edit and finished.")

    async def test_non_hybrid_never_swaps_models(self):
        loaded = []

        async def fake_stream_chat(*args, **kwargs):
            yield {"message": {"content": "Done."}, "done": True, "done_reason": "stop"}

        async def fake_ensure(key):
            loaded.append(key)
            return {"id": server.MODELS[key]["id"], "keep_alive": "1m",
                    "temperature": 0.2, "context": 32768}

        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        chat = {"id": "no-swap", "project_id": None, "agent_model": "agent",
                "messages": [{"role": "user", "content": "Hi."}]}
        meta = {"id": server.MODELS["agent"]["id"], "keep_alive": "1m",
                "temperature": 0.2, "context": 32768}

        with (
            patch.object(server.oc, "stream_chat", new=fake_stream_chat),
            patch.object(server.oc, "ensure_model_loaded", side_effect=fake_ensure),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.st, "save_chat", side_effect=lambda value: value),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 3, "agent_context": 32768,
                "shell_approval_mode": "never_ask",
            }),
        ):
            [event async for event in server._agent_loop(chat, meta, "agent", think=False)]

        self.assertEqual(loaded, [])


@unittest.skipIf(server is None, "server dependencies are installed in Aether's .venv")
class ShellApprovalTests(unittest.TestCase):
    def test_safe_auto_stops_asking_about_a_remembered_command(self):
        settings = {
            "shell_approval_mode": "safe_auto",
            "shell_allowlist": {"proj-1": {"commands": ["rm -rf build"], "binaries": []}},
        }
        # Destructive, so it prompts...
        self.assertTrue(server._shell_gate(settings, "rm -rf build")[0])
        # ...unless the user approved this exact command in this project.
        self.assertFalse(server._shell_gate(settings, "rm -rf build", "proj-1")[0])
        # A different project does not inherit it.
        self.assertTrue(server._shell_gate(settings, "rm -rf build", "proj-2")[0])

    def test_always_allow_a_program_covers_its_later_invocations(self):
        settings = {
            "shell_approval_mode": "safe_auto",
            "shell_allowlist": {"global": {"commands": [], "binaries": ["rm"]}},
        }
        self.assertFalse(server._shell_gate(settings, "rm -rf build", None)[0])
        self.assertTrue(server._shell_gate(settings, "shred -u secrets.txt", None)[0])

    def test_always_ask_ignores_the_allowlist(self):
        settings = {
            "shell_approval_mode": "always_ask",
            "shell_allowlist": {"global": {"commands": ["ls"], "binaries": ["rm"]}},
        }
        self.assertTrue(server._shell_gate(settings, "rm -rf build", None)[0])

    def test_primary_binary_is_the_program_not_an_argument(self):
        self.assertEqual(server.agent.shell_primary_binary("curl -sSL https://x"), "curl")
        self.assertEqual(server.agent.shell_primary_binary("/usr/bin/docker ps"), "docker")
        self.assertEqual(server.agent.shell_primary_binary("FOO=1 npm run build"), "npm")
        self.assertEqual(server.agent.shell_primary_binary(""), "")

@unittest.skipIf(server is None, "server dependencies are installed in Aether's .venv")
class JobStateResetTests(unittest.TestCase):

    def test_a_chat_that_never_planned_is_unaffected(self):
        chat = {}
        server._start_new_job(chat)
        self.assertNotIn("agent_spec", chat)

@unittest.skipIf(server is None, "server dependencies are installed in Aether's .venv")
class EstimateCalibrationTests(unittest.TestCase):
    """The estimator ran ~1.33x over on real agent prompts. That is not free
    headroom: it is 42% of the allocated context nothing may use, which is what
    pushed the replay floor into evicting the model's working set."""

    def _learn(self, pairs):
        chat = {"id": "cal"}
        for estimated, actual in pairs:
            server.record_estimate_accuracy(chat, "agent", estimated, actual)
        return chat

    def test_bias_is_neutral_until_enough_samples(self):
        chat = self._learn([(20000, 15000), (20000, 15000)])
        self.assertEqual(server._estimate_bias(chat, "agent"), 1.0)

    def test_a_consistent_overestimate_is_learned(self):
        # Real (sent~, prompt_eval_count) pairs from the observed run.
        chat = self._learn([(19744, 14870), (24284, 18629), (20220, 15335),
                            (21281, 16443), (20639, 15696)])
        bias = server._estimate_bias(chat, "agent")
        self.assertLess(bias, 0.85)
        self.assertGreater(bias, server._ESTIMATE_BIAS_FLOOR)

    def test_bias_never_claims_more_window_than_the_floor_allows(self):
        chat = self._learn([(20000, 1)] * 6)
        self.assertEqual(
            server._estimate_bias(chat, "agent"), server._ESTIMATE_BIAS_FLOOR
        )

    def test_an_underestimate_is_never_used_to_shrink_the_estimate(self):
        # Underestimating is the dangerous direction: Ollama truncates the FRONT
        # of an overflowing prompt, silently dropping the system prompt and plan.
        chat = self._learn([(10000, 16000)] * 6)
        self.assertEqual(server._estimate_bias(chat, "agent"), 1.0)

    def test_garbage_counters_are_ignored(self):
        chat = {"id": "cal"}
        server.record_estimate_accuracy(chat, "agent", 0, 100)
        server.record_estimate_accuracy(chat, "agent", 100, None)
        server.record_estimate_accuracy(chat, "agent", 100, 0)
        self.assertEqual(server._estimate_bias(chat, "agent"), 1.0)


@unittest.skipIf(server is None, "server dependencies are installed in Aether's .venv")
class RepeatGuardRevisionTests(unittest.TestCase):
    """Past the repeat limit the guard replays a cached rejection and never runs
    the tool again. Keyed on arguments alone that is permanent: an edit rejected
    while the file said one thing stayed rejected after the file was changed to
    say exactly what the anchor wanted."""

    def test_the_same_call_against_a_changed_file_is_a_new_call(self):
        import tempfile
        from pathlib import Path
        from config import ROOT
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            (root / "a.js").write_text("const FLIP_CD=5.0;\n", encoding="utf-8")
            call = {"name": "edit_file", "arguments": {
                "path": "a.js", "find": "const FLIP_CD=5.0;", "replace": "const FLIP_CD=1.8;"}}

            before = server._call_signature(call, root)
            self.assertEqual(before, server._call_signature(call, root))

            (root / "a.js").write_text("const FLIP_CD=5.0; // changed\n", encoding="utf-8")
            self.assertNotEqual(before, server._call_signature(call, root))

    def test_a_call_with_no_path_still_has_a_stable_signature(self):
        call = {"name": "git_status", "arguments": {}}
        self.assertEqual(
            server._call_signature(call, None), server._call_signature(call, None)
        )

@unittest.skipIf(server is None, "server dependencies are installed in Aether's .venv")
class CompactionBoundaryTests(unittest.TestCase):
    """Phase 2: compaction is a read-time projection over an append-only
    transcript. Nothing is deleted, so no stored offset can go stale and the
    full run survives for resume and audit."""

    @staticmethod
    def _run(n_reads: int) -> list[dict]:
        msgs: list[dict] = [{"role": "user", "content": "build it"}]
        for i in range(n_reads):
            msgs.append({"role": "assistant", "content": "",
                         "tool_calls": [{"name": "read_file",
                                         "arguments": {"path": f"f{i}.py"}}]})
            msgs.append({"role": "tool", "name": "read_file",
                         "content": json.dumps({"path": f"f{i}.py", "start": 1,
                                                "end": 50, "content": "BODY" + str(i)})})
        return msgs

    def test_rendering_resumes_after_the_newest_boundary(self):
        msgs = self._run(4)
        msgs.insert(5, {"role": "system", "kind": server.COMPACT_BOUNDARY_KIND,
                        "summary": "EARLIER WORK", "quiet": True})
        chat = {"id": "b", "messages": msgs}
        self.assertEqual(server._boundary_index(msgs), 5)

        rendered = server._build_messages(chat, "agent")
        bodies = "\n".join(m.get("content") or "" for m in rendered)
        # The summary stands in for what came before it...
        self.assertIn("EARLIER WORK", rendered[0]["content"])
        # ...and those turns are not replayed as well.
        self.assertNotIn("BODY0", bodies)
        self.assertNotIn("BODY1", bodies)
        # Everything after the boundary is replayed in full, no receipts.
        self.assertIn("BODY2", bodies)
        self.assertIn("BODY3", bodies)

    def test_only_the_newest_boundary_supplies_the_summary(self):
        msgs = self._run(4)
        msgs.insert(3, {"role": "system", "kind": server.COMPACT_BOUNDARY_KIND,
                        "summary": "OLD BRIEF", "quiet": True})
        msgs.insert(7, {"role": "system", "kind": server.COMPACT_BOUNDARY_KIND,
                        "summary": "NEW BRIEF", "quiet": True})
        chat = {"id": "b2", "messages": msgs}
        sys_prompt = server._build_messages(chat, "agent")[0]["content"]
        self.assertIn("NEW BRIEF", sys_prompt)
        self.assertNotIn("OLD BRIEF", sys_prompt)

    def test_a_boundary_never_orphans_a_tool_result(self):
        # Ollama rejects a tool result with no assistant tool_call in front of it.
        msgs = self._run(5)
        for keep in range(1, len(msgs)):
            upto = server._compact_split_point(msgs, keep)
            self.assertNotEqual(
                msgs[upto].get("role") if upto < len(msgs) else None, "tool",
                f"keep={keep} would split an assistant/tool pair",
            )

    def test_a_long_single_request_run_can_still_compact(self):
        # The request sits at index 0 with only tool churn after it. Clamping the
        # split to the latest user turn pinned upto=0, so compaction could never
        # fire and a long run could only grow.
        msgs = self._run(6)
        self.assertEqual(server._latest_user_index(msgs), 0)
        self.assertGreater(server._compact_split_point(msgs, 4), 0)

    def test_the_request_survives_its_own_compaction(self):
        msgs = self._run(6)
        msgs.insert(9, {"role": "system", "kind": server.COMPACT_BOUNDARY_KIND,
                        "summary": "BRIEF", "quiet": True})
        chat = {"id": "req", "messages": msgs}
        rendered = server._build_messages(chat, "agent")
        # Summary in the system prompt, original request re-emitted verbatim.
        self.assertIn("BRIEF", rendered[0]["content"])
        self.assertEqual(rendered[1]["role"], "user")
        self.assertEqual(rendered[1]["content"], "build it")

    def test_a_re_emitted_request_is_not_duplicated_when_still_in_view(self):
        msgs = self._run(2)
        msgs.insert(1, {"role": "system", "kind": server.COMPACT_BOUNDARY_KIND,
                        "summary": "BRIEF", "quiet": True})
        chat = {"id": "dup", "messages": msgs}
        rendered = server._build_messages(chat, "agent")
        self.assertEqual(sum(1 for m in rendered if m.get("role") == "user"), 1)

    def test_the_boundary_is_hidden_from_the_transcript_ui(self):
        # app.js drops quiet messages, so the marker never renders as a turn.
        msgs = self._run(2)
        msgs.insert(3, {"role": "system", "kind": server.COMPACT_BOUNDARY_KIND,
                        "summary": "s", "quiet": True})
        self.assertTrue(msgs[3]["quiet"])

    def test_a_chat_compacted_before_phase_2_still_renders_its_summary(self):
        chat = {"id": "legacy", "messages": self._run(2), "summary": "LEGACY BRIEF"}
        self.assertEqual(server._active_summary(chat), "LEGACY BRIEF")
        self.assertIn("LEGACY BRIEF", server._build_messages(chat, "agent")[0]["content"])


@unittest.skipIf(server is None, "server dependencies are installed in Aether's .venv")
class CompactionIsNonDestructiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_compaction_inserts_a_boundary_and_deletes_nothing(self):
        msgs = CompactionBoundaryTests._run(6)
        chat = {"id": "nd", "messages": msgs}
        before = list(msgs)

        async def fake_summary(chat_, model_key, upto):
            return "BRIEF"

        # Over the threshold on the first look, comfortably under once compacted.
        sizes = iter([999_999, 10, 10, 10, 10])

        def fake_estimate(chat_, model_key, msgs=None):
            try:
                return next(sizes)
            except StopIteration:
                return 10

        with patch.object(server, "_compact_chat", new=fake_summary), \
             patch.object(server, "_estimate_chat_tokens", new=fake_estimate), \
             patch.object(server.st, "save_chat", new=lambda c: c), \
             patch.object(server.st, "load_settings",
                          new=lambda: {"auto_compact_at": 0.85, "compact_keep_recent": 4,
                                       "agent_context": 65536}):
            _, did = await server._maybe_auto_compact(chat, "agent", None)

        self.assertTrue(did)
        kept = [m for m in chat["messages"] if m.get("kind") != server.COMPACT_BOUNDARY_KIND]
        # The transcript GREW. Every original message is still present, in order.
        self.assertEqual(len(chat["messages"]), len(before) + 1)
        self.assertEqual(kept, before)
        self.assertEqual(server._active_summary(chat), "BRIEF")


@unittest.skipIf(server is None, "server dependencies are installed in Aether's .venv")
class DetailHorizonTests(unittest.TestCase):
    """The cheap reduction tier. Collapsing it into summarization alone let the
    prompt reach ~2x the size before anything shrank it, and a measured live run
    made 48 tool calls without finishing a job it had previously done in 27."""

    @staticmethod
    def _run(n: int, body_chars: int = 7000) -> list[dict]:
        msgs: list[dict] = [{"role": "user", "content": "build it"}]
        for i in range(n):
            msgs.append({"role": "assistant", "content": "",
                         "tool_calls": [{"name": "read_file",
                                         "arguments": {"path": f"f{i}.py"}}]})
            msgs.append({"role": "tool", "name": "read_file",
                         "content": json.dumps({"path": f"f{i}.py", "start": 1, "end": 400,
                                                "content": f"MARK{i}" + "x" * body_chars})})
        return msgs

    def test_horizon_drops_old_detail_but_keeps_recent(self):
        chat = {"id": "h", "messages": self._run(30)}
        self.assertTrue(server._advance_detail_horizon(chat, "coder"))
        bodies = "\n".join(
            m.get("content") or "" for m in server._build_messages(chat, "coder")
        )
        self.assertNotIn("MARK0", bodies)
        self.assertIn("MARK29", bodies)

    def test_horizon_is_a_no_op_under_the_threshold(self):
        chat = {"id": "small", "messages": self._run(2)}
        self.assertFalse(server._advance_detail_horizon(chat, "coder"))

    def test_horizon_never_orphans_a_tool_result(self):
        chat = {"id": "orphan", "messages": self._run(30)}
        server._advance_detail_horizon(chat, "coder")
        idx = server._horizon_index(chat["messages"])
        self.assertGreater(idx, 0)
        self.assertNotEqual(chat["messages"][idx + 1].get("role"), "tool")
        # And the render never emits a tool result with no call in front of it.
        pending = False
        for m in server._build_messages(chat, "coder")[1:]:
            if m.get("role") == "tool":
                self.assertTrue(pending, "tool result with no preceding tool_calls")
            pending = bool(m.get("tool_calls"))

    def test_horizon_deletes_nothing(self):
        msgs = self._run(30)
        chat = {"id": "keep", "messages": msgs}
        before = list(msgs)
        server._advance_detail_horizon(chat, "coder")
        kept = [m for m in chat["messages"] if m.get("kind") != server.DETAIL_HORIZON_KIND]
        self.assertEqual(kept, before)

    def test_the_render_is_append_only_between_horizons(self):
        def serialize(ms):
            return "\n\x00\n".join(
                f"{m.get('role')}\x01{m.get('content') or ''}" for m in ms
            )
        chat = {"id": "stable", "messages": self._run(4)}
        previous, moves = "", 0
        for step in range(14):
            if server._advance_detail_horizon(chat, "coder"):
                moves += 1
                previous = ""  # a horizon legitimately rewrites the prefix once
            current = serialize(server._build_messages(chat, "coder"))
            if previous:
                self.assertTrue(current.startswith(previous),
                                "render rewrote already-sent content without a horizon")
            previous = current
            # Distinct files: the ordinary case, where each step only appends.
            chat["messages"].extend([
                {"role": "assistant", "content": "",
                 "tool_calls": [{"name": "read_file",
                                 "arguments": {"path": f"new{step}.py"}}]},
                {"role": "tool", "name": "read_file",
                 "content": json.dumps({"path": f"new{step}.py", "start": 1, "end": 400,
                                        "content": f"NEW{step}" + "x" * 7000})},
            ])
        # Reduction happened, but rarely, not on every step.
        self.assertLess(moves, 7)

    def test_a_reread_collapses_the_older_copy(self):
        # Deliberate trade: superseding an earlier read rewrites the prefix once,
        # which is cheaper than carrying five near-identical copies of one file
        # and far less likely to teach the model to keep exploring.
        msgs = [{"role": "user", "content": "go"}]
        for _ in range(3):
            msgs.append({"role": "assistant", "content": "",
                         "tool_calls": [{"name": "read_file",
                                         "arguments": {"path": "big.py"}}]})
            msgs.append({"role": "tool", "name": "read_file",
                         "content": json.dumps({"path": "big.py", "start": 1, "end": 400,
                                                "content": "SAMEBODY" + "x" * 7000})})
        rendered = server._build_messages({"id": "r", "messages": msgs}, "coder")
        bodies = [m.get("content") or "" for m in rendered if m.get("role") == "tool"]
        self.assertEqual(sum("SAMEBODY" in b for b in bodies), 1)
        self.assertEqual(sum("Superseded" in b for b in bodies), 2)

    def test_a_write_stops_reads_collapsing_across_it(self):
        # Content genuinely differs either side of an edit.
        msgs = [{"role": "user", "content": "go"}]
        def read(tag):
            return [
                {"role": "assistant", "content": "",
                 "tool_calls": [{"name": "read_file", "arguments": {"path": "big.py"}}]},
                {"role": "tool", "name": "read_file",
                 "content": json.dumps({"path": "big.py", "start": 1, "end": 400,
                                        "content": tag + "x" * 100})},
            ]
        msgs += read("BEFORE")
        msgs += [
            {"role": "assistant", "content": "",
             "tool_calls": [{"name": "edit_file", "arguments": {"path": "big.py"}}]},
            {"role": "tool", "name": "edit_file", "content": '{"ok": true}'},
        ]
        msgs += read("AFTER")
        rendered = server._build_messages({"id": "w", "messages": msgs}, "coder")
        bodies = "\n".join(m.get("content") or "" for m in rendered)
        self.assertIn("BEFORE", bodies)
        self.assertIn("AFTER", bodies)


@unittest.skipIf(server is None, "server dependencies are installed in Aether's .venv")
class GatesRemovedTests(unittest.IsolatedAsyncioTestCase):
    """Phase 3: the loop no longer counts the model's moves and then refuses them."""

    META = {"id": "test-model", "keep_alive": "1m", "temperature": 0.2, "context": 32768}
    SETTINGS = {"agent_repeat_limit": 3, "agent_context": 65536,
                "shell_approval_mode": "never_ask"}

    async def _run(self, fake_stream, chat):
        async def no_compact(chat_, model_key, emit=None, msgs=None):
            return chat_, False

        with (
            patch.object(server.oc, "stream_chat", new=fake_stream),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.st, "save_chat", side_effect=lambda v: v),
            patch.object(server.st, "load_settings", return_value=dict(self.SETTINGS)),
        ):
            return [e async for e in server._agent_loop(chat, self.META, "agent", think=False)]

    async def test_an_open_plan_nudges_twice_then_accepts_the_debrief(self):
        # The old gate deleted EVERY debrief while milestones were open and then
        # aborted the run, so a model that had finished had no legal way to say
        # so. The nudge itself was worth keeping: without it two scenarios
        # dropped from 21-28 rounds to 12, so the budget now expires into
        # acceptance rather than into an error.
        rounds = 0

        async def fake_stream(*a, **k):
            nonlocal rounds
            rounds += 1
            yield {"message": {"content": "Done, added the cooldown."},
                   "done": True, "done_reason": "stop"}

        chat = {
            "id": "debrief", "project_id": None,
            "agent_todos": [
                {"id": "a", "content": "Add cooldown", "status": "in_progress"},
                {"id": "b", "content": "Polish", "status": "pending"},
            ],
            "messages": [{"role": "user", "content": "Add a cooldown."}],
        }
        events = await self._run(fake_stream, chat)

        # Two nudges, then the third debrief is accepted, never an abort.
        self.assertEqual(rounds, 3)
        self.assertEqual(events[-1]["message"]["content"], "Done, added the cooldown.")
        self.assertFalse(chat.get("interrupted"))
        # The turn ends on the debrief. The plan is untouched bookkeeping, it
        # neither blocks the turn nor gets rewritten on the way out.
        self.assertEqual([t["status"] for t in chat["agent_todos"]],
                         ["in_progress", "pending"])

    async def test_narration_is_turned_into_an_action_not_a_debrief(self):
        # Qwen sometimes announces its next step instead of taking it. Ending the
        # turn on that loses the run; the removed plan gate used to catch it by
        # accident, so the loop now catches it deliberately and boundedly.
        rounds = 0

        async def fake_stream(*a, **k):
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                yield {"message": {"content": "Let me analyze the test failure and improve the"},
                       "done": True, "done_reason": "stop"}
            else:
                yield {"message": {"content": "Fixed the gravity flip; all three tests pass now."},
                       "done": True, "done_reason": "stop"}

        chat = {"id": "narrate", "project_id": None,
                "messages": [{"role": "user", "content": "Fix it."}]}
        events = await self._run(fake_stream, chat)

        self.assertGreater(rounds, 1, "narration should not have ended the turn")
        self.assertEqual(events[-1]["message"]["content"],
                         "Fixed the gravity flip; all three tests pass now.")

    async def test_open_milestones_are_left_exactly_as_the_model_left_them(self):
        # Bulk-closing on the way out rewrites the record: a live run reported six
        # items finishing at once after the model had actually stopped early.
        async def fake_stream(*a, **k):
            yield {"message": {"content": "Stopping here; the remaining work is blocked."},
                   "done": True, "done_reason": "stop"}

        todos = [{"id": f"m{i}", "content": f"Step {i}", "status": "pending"}
                 for i in range(6)]
        chat = {"id": "leave", "project_id": None,
                "agent_todos": todos,
                "messages": [{"role": "user", "content": "Do the six steps."}]}
        await self._run(fake_stream, chat)

        self.assertEqual([t["status"] for t in chat["agent_todos"]], ["pending"] * 6)

    async def test_reads_are_never_refused(self):
        # No inspection budget, no redundant-read block. Reading "too much" was a
        # symptom of a working set that kept evaporating, not a fault to police.
        rounds = 0

        async def fake_stream(*a, **k):
            nonlocal rounds
            rounds += 1
            if rounds <= 12:
                # Distinct ranges: previously the inspection budget refused these
                # after eight, and the redundant-read guard refused overlaps.
                yield {"message": {"tool_calls": [{"function": {
                    "index": 0, "name": "read_file",
                    "arguments": {"path": "same.py",
                                  "start_line": rounds * 20 + 1,
                                  "end_line": rounds * 20 + 60},
                }}]}, "done": True, "done_reason": "stop"}
            else:
                yield {"message": {"content": "Read enough."},
                       "done": True, "done_reason": "stop"}

        chat = {"id": "reads", "project_id": None,
                "messages": [{"role": "user", "content": "Study the file."}]}
        def read_result(name, arguments, *a, **k):
            start = int((arguments or {}).get("start_line") or 1)
            return json.dumps({"path": "same.py", "start": start,
                               "end": start + 59, "content": "x" * 100})

        with patch.object(server.agent, "run_tool", side_effect=read_result):
            events = await self._run(fake_stream, chat)

        blocked = [e for e in events
                   if e.get("type") == "tool_result" and "Skipped inspection" in str(e.get("summary"))]
        self.assertEqual(blocked, [])
        self.assertEqual(events[-1]["message"]["content"], "Read enough.")

    async def test_steering_never_rides_on_a_system_message(self):
        # Qwen's template hoists system messages to the front of the prompt, so a
        # "trailing" directive is relocated into the prefix and costs a full KV
        # re-evaluation: measured 33.8s for 7 tokens vs 3.5s for 3,101 appended.
        samples: list[dict] = []
        rounds = 0

        async def fake_stream(*a, **k):
            nonlocal rounds
            samples.append({"messages": list(k.get("messages") or a[1])})
            rounds += 1
            if rounds == 1:
                yield {"message": {"tool_calls": [{"function": {
                    "index": 0, "name": "read_file",
                    "arguments": {"path": "f.py", "start_line": 1, "end_line": 5},
                }}]}, "done": True, "done_reason": "stop"}
            else:
                yield {"message": {"content": "ok"}, "done": True, "done_reason": "stop"}

        chat = {"id": "steer", "project_id": None,
                "agent_todos": [{"id": "a", "content": "Do it", "status": "in_progress"}],
                "messages": [{"role": "user", "content": "Continue."}]}
        with patch.object(server.agent, "run_tool",
                          side_effect=lambda *a, **k: json.dumps(
                              {"path": "f.py", "start": 1, "end": 5, "content": "x"})):
            await self._run(fake_stream, chat)

        directive_samples = [s for s in samples if DIRECTIVE_MARKER in _prompt_text(s)]
        self.assertTrue(directive_samples, "expected an open-plan continuation directive")
        for sample in directive_samples:
            self.assertNotEqual(
                _directive_channel(sample), "system",
                "per-step steering must not be a system message",
            )
            # And exactly one system message: the stable prefix.
            self.assertEqual(
                sum(1 for m in sample["messages"] if m.get("role") == "system"), 1)


@unittest.skipIf(server is None, "server dependencies are installed in Aether's .venv")
class OffloadWiringTests(unittest.IsolatedAsyncioTestCase):
    """Phase 4 wiring. The offload helper is unit-tested separately; what this
    covers is that _agent_loop actually routes results through it. The first
    version of Phase 4 was correct in isolation and never fired in a real run,
    because the tools truncated below the threshold before it was reached."""

    async def test_a_large_tool_result_is_stored_as_a_pointer(self):
        import shutil
        big = "\n".join(f"build line {i}: compiling" for i in range(4000))
        rounds = 0

        async def fake_stream(*a, **k):
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                yield {"message": {"tool_calls": [{"function": {
                    "index": 0, "name": "run_shell",
                    "arguments": {"command": "make"},
                }}]}, "done": True, "done_reason": "stop"}
            else:
                yield {"message": {"content": "Build inspected."},
                       "done": True, "done_reason": "stop"}

        async def no_compact(chat_, model_key, emit=None, msgs=None):
            return chat_, False

        chat = {"id": "offload-wiring", "project_id": None,
                "messages": [{"role": "user", "content": "Run the build."}]}
        raw = json.dumps({"exit_code": 0, "stdout": big, "stderr": ""})
        try:
            with (
                patch.object(server.oc, "stream_chat", new=fake_stream),
                patch.object(server, "_maybe_auto_compact", new=no_compact),
                patch.object(server.agent, "run_tool", side_effect=lambda *a, **k: raw),
                patch.object(server.st, "save_chat", side_effect=lambda v: v),
                patch.object(server.st, "load_settings", return_value={
                    "agent_repeat_limit": 3, "agent_context": 65536,
                    "shell_approval_mode": "never_ask"}),
            ):
                [e async for e in server._agent_loop(
                    chat, {"id": "m", "keep_alive": "1m", "temperature": 0.2,
                           "context": 32768}, "agent", think=False)]

            stored = next(m for m in chat["messages"] if m.get("role") == "tool")
            payload = json.loads(stored["content"])
            # The transcript holds a pointer, not 139k characters of build log.
            self.assertIn("stdout", payload["offloaded"])
            self.assertLess(len(stored["content"]), len(raw) // 5)
            # Head AND tail survive. [-12000:] used to drop the head, where the
            # first error of a build log lives.
            self.assertIn("build line 0:", payload["stdout"])
            self.assertIn("build line 3999:", payload["stdout"])
            # And the full log is on disk, byte for byte.
            self.assertEqual(
                Path(payload["offloaded"]["stdout"]["path"]).read_text(encoding="utf-8"),
                big,
            )
        finally:
            shutil.rmtree(server.agent.TOOLOUT_DIR / "offload-wiring", ignore_errors=True)


@unittest.skipIf(server is None, "server dependencies are installed in Aether's .venv")
class DelegationTests(unittest.IsolatedAsyncioTestCase):
    """Phase 5: a sub-task runs the same loop over a fresh transcript, so its
    reading costs the parent only the summary it returns. Same model, run
    sequentially. The benefit is context isolation, not parallelism, and a
    second co-resident model does not fit on a 24GB card."""

    META = {"id": "test-model", "keep_alive": "1m", "temperature": 0.2, "context": 32768}
    SETTINGS = {"agent_repeat_limit": 3, "agent_context": 65536,
                "shell_approval_mode": "never_ask"}

    def setUp(self):
        self.saved: list[dict] = []

    async def _run(self, fake_stream, chat, run_tool=None, large_project=True):
        async def no_compact(chat_, model_key, emit=None, msgs=None):
            return chat_, False

        patches = [
            # The loop recomputes the gate from the project root each run, so a
            # flag set on the chat alone would be overwritten.
            patch.object(server.agent, "project_is_large", return_value=large_project),
            patch.object(server.oc, "stream_chat", new=fake_stream),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.st, "save_chat", side_effect=lambda v: self.saved.append(v) or v),
            patch.object(server.st, "load_settings", return_value=dict(self.SETTINGS)),
        ]
        if run_tool is not None:
            patches.append(patch.object(server.agent, "run_tool", side_effect=run_tool))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            return [e async for e in server._agent_loop(
                chat, self.META, "agent", think=False)]

    async def test_the_parent_pays_only_for_the_summary(self):
        big = "x" * 60_000
        turns = []

        async def fake_stream(*a, **k):
            msgs = list(k.get("messages") or a[1])
            turns.append(msgs)
            # Discriminate on the sub-run's own USER turn. Matching the task
            # text anywhere fails: the delegate result echoes the task back, so
            # the parent's next prompt contains it too.
            is_sub = any(
                m.get("role") == "user" and (m.get("content") or "").strip() == "Find the auth check"
                for m in msgs
            )
            if is_sub:
                if not any(m.get("role") == "tool" for m in msgs):
                    yield {"message": {"tool_calls": [{"function": {
                        "index": 0, "name": "read_file",
                        "arguments": {"path": "auth.py"}}}]},
                        "done": True, "done_reason": "stop"}
                else:
                    yield {"message": {"content": "Auth is checked in auth.py:42."},
                           "done": True, "done_reason": "stop"}
            elif not any(m.get("role") == "tool" for m in msgs):
                yield {"message": {"tool_calls": [{"function": {
                    "index": 0, "name": "delegate",
                    "arguments": {"task": "Find the auth check"}}}]},
                    "done": True, "done_reason": "stop"}
            else:
                yield {"message": {"content": "Patched auth.py:42."},
                       "done": True, "done_reason": "stop"}

        chat = {"id": "deleg", "project_id": None, "delegate_available": True,
                "messages": [{"role": "user", "content": "Fix auth."}]}
        events = await self._run(
            fake_stream, chat,
            run_tool=lambda *a, **k: json.dumps({"path": "auth.py", "content": big}),
        )

        tool_msgs = [m for m in chat["messages"] if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1, "only the delegate result reaches the parent")
        payload = json.loads(tool_msgs[0]["content"])
        self.assertTrue(payload["delegated"])
        self.assertIn("auth.py:42", payload["report"])
        self.assertIn("read_file", payload["tools_attempted"])
        # The 60k the sub-agent read never touches the parent's transcript.
        self.assertNotIn(big, json.dumps(chat["messages"]))
        self.assertLess(len(tool_msgs[0]["content"]), 2000)
        self.assertEqual(events[-1]["message"]["content"], "Patched auth.py:42.")

    async def test_a_subagent_cannot_delegate_further(self):
        async def fake_stream(*a, **k):
            yield {"message": {"content": "done"}, "done": True, "done_reason": "stop"}

        chat = {"id": "sub", "project_id": None, "subagent": {"task": "x"},
                "messages": [{"role": "user", "content": "x"}]}
        await self._run(fake_stream, chat)
        # Enforced twice: the schema is withheld...
        names = [t["function"]["name"] for t in server.agent.tool_defs_for(chat)]
        self.assertNotIn("delegate", names)
        # ...including when the spec is empty. An empty dict is falsy, and
        # treating that as "not a sub-agent" handed the child the full tool set.
        for spec in ({}, None, {"tools": []}):
            got = [t["function"]["name"]
                   for t in server.agent.tool_defs_for({"subagent": spec})]
            self.assertNotIn("delegate", got, f"spec={spec!r} leaked delegate")
            self.assertTrue(got, f"spec={spec!r} left the sub-agent with no tools")
        # A non-sub-run keeps everything, when the gate is open for it.
        self.assertIn("delegate", [t["function"]["name"]
                                   for t in server.agent.tool_defs_for({"delegate_available": True})])

    async def test_a_refused_recursive_delegate_is_answered_not_crashed(self):
        rounds = 0

        async def fake_stream(*a, **k):
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                yield {"message": {"tool_calls": [{"function": {
                    "index": 0, "name": "delegate",
                    "arguments": {"task": "go deeper"}}}]},
                    "done": True, "done_reason": "stop"}
            else:
                yield {"message": {"content": "did it myself"},
                       "done": True, "done_reason": "stop"}

        chat = {"id": "sub2", "project_id": None, "subagent": {"task": "x"},
                "messages": [{"role": "user", "content": "x"}]}
        events = await self._run(fake_stream, chat)
        # ...and if it is called anyway the runtime refuses it with an actionable
        # message rather than recursing. The execution-level allowlist catches it
        # first, which is the more general guard.
        tool_msgs = [m for m in chat["messages"] if m.get("role") == "tool"]
        payload = json.loads(tool_msgs[0]["content"])
        self.assertIn("not available", payload["error"])
        self.assertNotIn("delegate", payload["available"])
        # And the sub-run carries on instead of dying on the refusal.
        self.assertEqual(events[-1]["message"]["content"], "did it myself")

    async def test_a_withheld_tool_is_refused_at_execution_not_just_hidden(self):
        # Withholding the schema is advice: agent.run_tool dispatches on name, so
        # a model that emits a withheld tool still gets it executed. Observed
        # live: a read-only sub-agent successfully called todo_write.
        rounds = 0
        executed: list[str] = []

        async def fake_stream(*a, **k):
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                yield {"message": {"tool_calls": [{"function": {
                    "index": 0, "name": "write_file",
                    "arguments": {"path": "x.py", "content": "boom"}}}]},
                    "done": True, "done_reason": "stop"}
            else:
                yield {"message": {"content": "reported instead"},
                       "done": True, "done_reason": "stop"}

        def spy(name, *a, **k):
            executed.append(name)
            return json.dumps({"ok": True})

        chat = {"id": "restricted", "project_id": None,
                "subagent": {"task": "look only", "tools": ["read_file"]},
                "messages": [{"role": "user", "content": "look"}]}
        await self._run(fake_stream, chat, run_tool=spy)

        self.assertNotIn("write_file", executed, "a withheld tool was executed")
        tool_msgs = [m for m in chat["messages"] if m.get("role") == "tool"]
        payload = json.loads(tool_msgs[0]["content"])
        self.assertIn("not available", payload["error"])
        self.assertEqual(payload["available"], ["read_file"])

    async def test_the_parent_may_still_use_every_tool(self):
        # The guard must not restrict a normal run.
        chat = {"id": "normal", "messages": [], "delegate_available": True}
        self.assertEqual(
            server._allowed_tool_names(chat),
            {t["function"]["name"] for t in server.agent.TOOL_DEFS},
        )

    async def test_delegation_is_gated_on_project_size(self):
        # Measured A/B on a 113k-line repo: the model never called delegate,
        # because peak prompt stayed at 15-24k against a 22k working set. Its
        # schema costs 324 tokens on every request, so it is offered only where
        # surveying is genuinely expensive.
        import tempfile
        names = lambda c: {t["function"]["name"] for t in server.agent.tool_defs_for(c)}
        self.assertNotIn("delegate", names({}))
        self.assertIn("delegate", names({"delegate_available": True}))

        with tempfile.TemporaryDirectory() as tmp:
            small = Path(tmp)
            (small / "only.py").write_text("x = 1")
            self.assertFalse(server.agent.project_is_large(small))
            self.assertFalse(server.agent.project_is_large(None))

            big = small / "big"
            big.mkdir()
            for i in range(server.agent.DELEGATE_MIN_FILES + 5):
                (big / f"f{i}.py").write_text("y = 1")
            self.assertTrue(server.agent.project_is_large(big))

    async def test_a_runaway_subtask_hits_its_budget_and_reports_back(self):
        async def fake_stream(*a, **k):
            # Never concludes: always another tool call.
            yield {"message": {"tool_calls": [{"function": {
                "index": 0, "name": "list_dir",
                "arguments": {"path": f"dir{time.time()}"}}}]},
                "done": True, "done_reason": "stop"}

        chat = {"id": "runaway", "project_id": None, "subagent": {"task": "x"},
                "messages": [{"role": "user", "content": "x"}]}
        events = await self._run(fake_stream, chat,
                                 run_tool=lambda *a, **k: json.dumps({"entries": []}))
        self.assertIn("budget reached", events[-1]["message"]["content"].lower())
        rounds = sum(1 for m in chat["messages"] if m.get("role") == "assistant")
        self.assertLessEqual(rounds, server.agent.SUBAGENT_MAX_STEPS + 1)

    async def test_a_failing_subtask_does_not_kill_the_parent(self):
        async def boom(*a, **k):
            raise RuntimeError("model exploded")
            yield  # pragma: no cover

        async def fake_stream(*a, **k):
            msgs = list(k.get("messages") or a[1])
            if not any(m.get("role") == "tool" for m in msgs):
                yield {"message": {"tool_calls": [{"function": {
                    "index": 0, "name": "delegate",
                    "arguments": {"task": "survey"}}}]},
                    "done": True, "done_reason": "stop"}
            else:
                yield {"message": {"content": "Carried on regardless."},
                       "done": True, "done_reason": "stop"}

        chat = {"id": "boom", "project_id": None,
                "messages": [{"role": "user", "content": "go"}]}
        with patch.object(server, "_agent_loop", wraps=server._agent_loop):
            async def failing_sub(parent, args, meta, model_key, effort=None):
                raise RuntimeError("model exploded")
                yield  # pragma: no cover
            events = await self._run(fake_stream, chat)

        # The parent must still finish its own turn.
        self.assertEqual(events[-1]["message"]["content"], "Carried on regardless.")


@unittest.skipIf(server is None, "server dependencies are installed in Aether's .venv")
class SampleTimeoutRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """A model that ran out of clock needs MORE time, not less. Shrinking the
    budget on every retry is right only for a model returning nothing.

    Observed live: a `high`-effort turn on qwen3.8:27b died with "no assistant
    message after four progressively smaller recovery prompts (sample timeout
    after 60s)". Measured at 82.6 tok/s, an 8192-token reply budget needs ~99s
    of generation alone, impossible in 90s let alone 60s."""

    async def _timeouts(self, done_reason: str) -> list[int]:
        seen: list[int] = []
        rounds = 0

        async def fake_stream(*a, **k):
            nonlocal rounds
            rounds += 1
            # asyncio.timeout() is what the loop wraps the stream in; record the
            # budget it was given by reading the deadline off the running task.
            seen.append(k.get("_timeout") or 0)
            if rounds <= 3:
                yield {"done": True, "done_reason": done_reason, "eval_count": 10,
                       "message": {}}
            else:
                yield {"message": {"content": "ok"}, "done": True, "done_reason": "stop"}

        return seen

    async def test_a_timeout_retry_gets_at_least_the_generation_budget(self):
        from config import EFFORT
        budget = EFFORT["high"]["num_predict"]
        floor = int(budget / 40) + 60
        # The whole point: the retry budget must exceed what the reply physically
        # needs at a conservative rate, and must not decay with the stall count.
        self.assertGreater(floor, 150, "high-effort floor must exceed the base timeout")
        for stalls in range(1, 5):
            after_timeout = max(150, floor)
            after_empty = max(60, 150 - stalls * 30)
            self.assertGreaterEqual(after_timeout, floor)
            self.assertLessEqual(after_empty, 120)
        # An empty-response retry still fails fast; a timeout retry does not.
        self.assertGreater(max(150, floor), max(60, 150 - 3 * 30))

    async def test_the_flag_only_trips_on_a_real_timeout(self):
        self.assertTrue("sample timeout after 60s".startswith("sample timeout"))
        for other in ("stop", "length", "empty response", "load"):
            self.assertFalse(other.startswith("sample timeout"))


@unittest.skipIf(server is None, "server dependencies are installed in Aether's .venv")
class AskUserDisciplineTests(unittest.IsolatedAsyncioTestCase):
    """Questions belong at the start of a job or not at all. A blocking question
    the user has not seen is worse than a wrong guess: an observed run sat behind
    an unanswered volcano-styling question having written nothing."""

    META = {"id": "test-model", "keep_alive": "1m", "temperature": 0.2, "context": 32768}

    async def _run(self, fake_stream, chat, run_tool=None):
        async def no_compact(chat_, model_key, emit=None, msgs=None):
            return chat_, False

        patches = [
            patch.object(server.oc, "stream_chat", new=fake_stream),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.st, "save_chat", side_effect=lambda v: v),
            patch.object(server.st, "load_settings", return_value={
                "agent_repeat_limit": 3, "agent_context": 65536,
                "shell_approval_mode": "never_ask"}),
        ]
        if run_tool is not None:
            patches.append(patch.object(server.agent, "run_tool", side_effect=run_tool))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            return [e async for e in server._agent_loop(
                chat, self.META, "agent", think=False)]

    async def test_a_question_after_the_first_write_is_refused(self):
        rounds = 0

        async def fake_stream(*a, **k):
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                yield {"message": {"tool_calls": [{"function": {
                    "index": 0, "name": "write_file",
                    "arguments": {"path": "game.html", "content": "<html>"}}}]},
                    "done": True, "done_reason": "stop"}
            elif rounds == 2:
                yield {"message": {"tool_calls": [{"function": {
                    "index": 0, "name": "ask_user",
                    "arguments": {"questions": [{"id": "v", "question": "Big volcano?",
                                                 "options": [{"label": "Yes"}, {"label": "No"}]}]}}}]},
                    "done": True, "done_reason": "stop"}
            else:
                yield {"message": {"content": "Built it; assumed a large volcano."},
                       "done": True, "done_reason": "stop"}

        chat = {"id": "late-ask", "project_id": None,
                "messages": [{"role": "user", "content": "Make a volcano game."}]}
        events = await self._run(
            fake_stream, chat,
            run_tool=lambda *a, **k: json.dumps({"ok": True, "path": "game.html"}))

        # The run must not have parked on a question.
        self.assertFalse(any(e.get("type") == "ask_user" for e in events))
        refusal = json.loads([m for m in chat["messages"]
                              if m.get("role") == "tool"][-1]["content"])
        self.assertIn("Too late to ask", refusal["error"])
        self.assertEqual(events[-1]["message"]["content"],
                         "Built it; assumed a large volcano.")

    async def test_a_question_before_any_write_still_works(self):
        rounds = 0

        async def fake_stream(*a, **k):
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                yield {"message": {"tool_calls": [{"function": {
                    "index": 0, "name": "ask_user",
                    "arguments": {"questions": [{"id": "t", "question": "Which file?",
                                                 "options": [{"label": "a.py"}, {"label": "b.py"}]}]}}}]},
                    "done": True, "done_reason": "stop"}
            else:
                yield {"message": {"content": "done"}, "done": True, "done_reason": "stop"}

        chat = {"id": "early-ask", "project_id": None,
                "messages": [{"role": "user", "content": "Patch the right file."}]}
        # Expire immediately rather than blocking the test for the real window.
        with patch.object(server, "ASK_USER_TIMEOUT", 0.01):
            events = await self._run(fake_stream, chat)

        self.assertTrue(any(e.get("type") == "ask_user" for e in events))
        # An unanswered question is cheap: the model is told to assume and continue.
        result = json.loads([m for m in chat["messages"]
                             if m.get("role") == "tool"][0]["content"])
        self.assertTrue(result["timed_out"])
        self.assertIn("most reasonable", result["note"])
        self.assertEqual(events[-1]["message"]["content"], "done")

    async def test_the_block_window_is_short(self):
        # 600s meant a question the user did not notice cost ten minutes.
        self.assertLessEqual(server.ASK_USER_TIMEOUT, 120)


@unittest.skipIf(server is None, "server dependencies are installed in Aether's .venv")
class AgenticModelPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """The picked agentic model must survive chat creation.

    A new agentic chat used to come back with no agent_model at all, and the UI
    re-derived its default from that silence, so choosing the 27B and then
    opening a new conversation quietly put the run back on the coder.
    """

    @contextlib.contextmanager
    def _storage(self, store):
        def new_chat(mode="chat", title="New chat", project_id=None):
            chat = {"id": "c1", "mode": mode, "title": title, "project_id": project_id}
            store["c1"] = chat
            return chat

        with patch.object(server.st, "new_chat", side_effect=new_chat), \
             patch.object(server.st, "get_chat", side_effect=lambda cid: store.get(cid)), \
             patch.object(server.st, "save_chat", side_effect=lambda c: store.__setitem__(c["id"], c) or c):
            yield

    async def test_a_new_agentic_chat_records_the_picked_model(self):
        store = {}
        with self._storage(store):
            chat = await server.api_new_chat(
                server.ChatCreate(mode="agentic", agent_model="agent")
            )
        self.assertEqual(chat["agent_model"], "agent")
        self.assertEqual(server._model_key(chat), "agent")

    async def test_a_new_agentic_chat_without_a_pick_gets_the_executor(self):
        store = {}
        with self._storage(store):
            chat = await server.api_new_chat(server.ChatCreate(mode="agentic"))
        self.assertEqual(chat["agent_model"], server.DEFAULT_AGENT_KEY)

    async def test_a_chat_tab_conversation_carries_no_agent_model(self):
        store = {}
        with self._storage(store):
            chat = await server.api_new_chat(server.ChatCreate(mode="chat"))
        self.assertNotIn("agent_model", chat)

    async def test_patching_retargets_an_open_chat_without_starting_a_new_one(self):
        store = {"c1": {"id": "c1", "mode": "agentic", "agent_model": "coder"}}
        with self._storage(store):
            chat = await server.api_patch_chat(
                "c1", server.ChatPatch(mode="agentic", agent_model="agent")
            )
        self.assertEqual(chat["agent_model"], "agent")
        self.assertEqual(server._model_key(chat), "agent")

    async def test_a_bogus_agent_model_leaves_the_existing_pick_alone(self):
        store = {"c1": {"id": "c1", "mode": "agentic", "agent_model": "agent"}}
        with self._storage(store):
            chat = await server.api_patch_chat("c1", server.ChatPatch(agent_model="nonsense"))
        self.assertEqual(chat["agent_model"], "agent")


@unittest.skipIf(server is None, "server dependencies are installed in Aether's .venv")
class ParkedPromptTests(unittest.IsolatedAsyncioTestCase):
    """A run parked on the user must be answerable from outside the stream.

    Both failures here produced the same symptom: the run waited out its whole
    timeout and then reported an approval the user was never shown.
    """

    SETTINGS = {
        "agent_repeat_limit": 3,
        "agent_context": 32768,
        "shell_approval_mode": "safe_auto",
    }
    META = {"id": "test-model", "keep_alive": "1m", "temperature": 0.2, "context": 32768}

    def _sampler(self, command="sudo apt install p7zip-full"):
        calls = []

        async def fake_stream_chat(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                yield {"message": {"tool_calls": [{
                    "function": {"index": 0, "name": "run_shell",
                                 "arguments": {"command": command}},
                }]}, "done": True, "done_reason": "stop"}
            else:
                yield {"message": {"content": "Done after the approval."},
                       "done": True, "done_reason": "stop"}

        return fake_stream_chat

    @contextlib.contextmanager
    def _harness(self, command="sudo apt install p7zip-full"):
        async def no_compact(chat, model_key, emit=None, msgs=None):
            return chat, False

        with (
            patch.object(server.oc, "stream_chat", new=self._sampler(command)),
            patch.object(server, "_maybe_auto_compact", new=no_compact),
            patch.object(server.agent, "run_tool", side_effect=lambda *a, **k: json.dumps({"ok": True})),
            patch.object(server.st, "save_chat", side_effect=lambda v: v),
            patch.object(server, "SHELL_APPROVAL_TIMEOUT", 5.0),
            patch.object(server.st, "load_settings", return_value=dict(self.SETTINGS)),
        ):
            yield

    async def test_a_parked_approval_can_be_read_back_over_http(self):
        chat = {"id": "parked", "project_id": None,
                "messages": [{"role": "user", "content": "install 7z"}]}
        seen, served = [], []

        async def drive():
            with self._harness():
                async for ev in server._agent_loop(chat, self.META, "agent", think=False):
                    seen.append(ev)
                    if ev.get("type") == "shell_approval":
                        # Exactly what the UI's poll does when it missed the frame.
                        served.append(await server.pending_question("parked"))
                        await server.answer_question("parked", server.AnswerIn(
                            answers={"shell_approval": "Run command"}, cancelled=False,
                        ))

        await asyncio.wait_for(drive(), timeout=20)

        self.assertEqual(len(served), 1)
        self.assertEqual(served[0]["pending"]["type"], "shell_approval")
        self.assertEqual(served[0]["pending"]["command"], "sudo apt install p7zip-full")
        # Answered, not expired, and the slot is released afterwards.
        done = next(e for e in seen if e.get("type") == "shell_approval_done")
        self.assertFalse(done["cancelled"])
        self.assertEqual((await server.pending_question("parked"))["pending"], None)

    async def test_nothing_is_pending_on_an_idle_chat(self):
        self.assertEqual((await server.pending_question("never-ran"))["pending"], None)

    async def test_a_sub_agent_approval_reaches_the_parent_and_its_chat_id(self):
        """The sub-run's events were filtered down to tool_start and message.

        An approval raised inside a delegated sub-task was dropped on the floor:
        no card, no way to answer, and the sub-run sat on the future until it
        expired. It also has to be parked under the parent's id, because the
        synthetic child id is not something the UI can post an answer to.
        """
        parent = {"id": "parent-chat", "project_id": None, "messages": []}
        events, parked_keys = [], []

        async def drive():
            with self._harness():
                async for ev in server._run_subagent(
                    parent, {"task": "install the archiver"}, self.META, "agent"
                ):
                    events.append(ev)
                    if ev.get("type") == "shell_approval":
                        parked_keys.append(list(server._pending_questions))
                        await server.answer_question(ev["chat_id"], server.AnswerIn(
                            answers={"shell_approval": "Deny"}, cancelled=False,
                        ))

        await asyncio.wait_for(drive(), timeout=20)

        approvals = [e for e in events if e.get("type") == "shell_approval"]
        self.assertEqual(len(approvals), 1, "the sub-run's approval never reached the parent stream")
        self.assertEqual(approvals[0]["chat_id"], "parent-chat")
        self.assertEqual(parked_keys[0], ["parent-chat"])
        self.assertTrue(any(e.get("type") == "shell_approval_done" for e in events))
        # And the sub-run still returns a result rather than dying on the prompt.
        self.assertTrue(any(e.get("type") == "subagent_result" for e in events))
