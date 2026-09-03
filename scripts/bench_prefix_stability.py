#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Measure how much of each step's prompt is reusable from the previous step.

Ollama reuses KV cache only for a byte-identical prefix. Anything that rewrites
already-sent content mid-prompt (a sliding tool-result window, recompression of
older results, dropping old turns) forces everything after that point to be
re-evaluated. This walks a realistic agent transcript and reports, per step, how
much of the previous prompt survived.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server

FILE_BODY = "\n".join(f"{i}|  const value{i} = compute({i});" for i in range(1, 400))


def serialized(msgs: list[dict]) -> str:
    return "\n\x00\n".join(
        f"{m.get('role')}\x01{m.get('name') or ''}\x01{m.get('content') or ''}" for m in msgs
    )


def common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def main() -> int:
    steps = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    messages = [{"role": "user", "content": "Add enemies, thorns and music to jungle.html."}]
    chat = {"id": "prefix", "messages": messages}

    prev = ""
    print(f"{'step':<5} {'est tokens':>11} {'reused':>9} {'rewritten':>10}  note")
    rewrites = 0
    compactions = 0
    horizons = 0
    limit = server._ctx_limit("coder")

    async def fake_summary(chat_, model_key, upto):
        # Stand-in for the real summarizer so the bench needs no GPU. Length is
        # representative of a five-section brief.
        return "COMPACTED BRIEF\n" + ("- carried detail\n" * 40)

    for step in range(steps):
        # Exactly what the loop does before building the prompt.
        built = server._build_messages(chat, "coder")
        if server._advance_detail_horizon(chat, "coder", msgs=built):
            horizons += 1
            built = server._build_messages(chat, "coder")
        with patch.object(server, "_compact_chat", new=fake_summary):
            _, did = asyncio.run(
                server._maybe_auto_compact(chat, "coder", None, msgs=built)
            )
        if did:
            compactions += 1
            built = server._build_messages(chat, "coder")
        cur = serialized(built)
        est = server._estimate_chat_tokens(chat, "coder", msgs=built)
        if prev:
            keep = common_prefix(prev, cur)
            reuse = 100 * keep / max(len(prev), 1)
            lost = len(prev) - keep
            if lost > 200:
                rewrites += 1
            note = "" if lost <= 200 else f"<-- context reduced (est {est})"
            over = "  OVER LIMIT" if est > limit else ""
            print(f"{step:<5} {est:>11,} {reuse:>8.0f}% {lost:>10,}  {note}{over}")
        prev = cur
        # one read + one edit per step, the shape of a real run
        messages.append({
            "role": "assistant", "content": "", "interim": True,
            "tool_calls": [{"name": "read_file", "arguments": {
                "path": "jungle.html", "start_line": step * 20 + 1, "end_line": step * 20 + 120}}],
        })
        messages.append({"role": "tool", "name": "read_file", "content": json.dumps({
            "path": "jungle.html", "start": step * 20 + 1, "end": step * 20 + 120,
            "revision": "r1", "content": FILE_BODY[:7000]})})

    print(f"\n{rewrites} of {steps - 1} steps rewrote already-sent content "
          f"({100 * rewrites / max(steps - 1, 1):.0f}%); {horizons} horizon(s), {compactions} compaction(s).")
    print(f"context limit {limit:,} tokens; final estimate "
          f"{server._estimate_chat_tokens(chat, 'coder'):,}; "
          f"{len(chat['messages'])} messages retained on disk")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
