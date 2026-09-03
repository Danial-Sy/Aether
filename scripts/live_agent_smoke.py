#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Run one real, read-only native tool turn against the configured Ollama model."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server
from config import MODELS, ROOT


async def main() -> int:
    chat = {
        "id": "live-native-tool-smoke",
        "project_id": "live-project",
        "messages": [{
            "role": "user",
            "content": (
                "Use read_file to read README.md lines 1 through 8. Then reply with "
                "the exact first Markdown heading and nothing else."
            ),
        }],
    }
    project = {"id": "live-project", "name": "Aether", "root": str(ROOT), "description": ""}
    settings = {
        "agent_repeat_limit": 3,
        "agent_context": 65536,
        "shell_approval_mode": "never_ask",
        "auto_compact_at": 0.85,
        "compact_keep_recent": 10,
    }

    with (
        patch.object(server.st, "get_project", return_value=project),
        patch.object(server.st, "save_chat", side_effect=lambda value: value),
        patch.object(server.st, "load_settings", return_value=settings),
        patch.object(server.st, "history_index", return_value=""),
        patch.object(server.st, "memory_as_prompt", return_value=""),
        patch.object(server.st, "canon_as_prompt", return_value=""),
    ):
        events = [
            event
            async for event in server._agent_loop(
                chat,
                MODELS["agent"],
                "agent",
                effort="low",
                think=False,
            )
        ]

    tool_names = [message.get("name") for message in chat["messages"] if message.get("role") == "tool"]
    final = next(
        (
            event.get("message", {}).get("content", "")
            for event in reversed(events)
            if event.get("type") == "message" and event.get("message", {}).get("content")
        ),
        "",
    ).strip()
    ok = "read_file" in tool_names and "Aether" in final
    print({"ok": ok, "tools": tool_names, "final": final})
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
