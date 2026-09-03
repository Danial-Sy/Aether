#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Measure where an agentic step's wall clock actually goes.

Splits each step into time-to-first-token (dominated by prompt processing) and
generation. A slow run caused by reprocessed context and one caused by slow
token generation look identical from outside and need opposite fixes.

Usage: AETHER_TIMING_LOG=/tmp/t.jsonl .venv/bin/python scripts/bench_agent_step.py [model_key]
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server
from config import MODELS

# A file the size of the one that was actually slow.
GAME_HTML = "<!doctype html>\n<html><body><canvas id=c></canvas>\n<script>\n" + "".join(
    f"function helper{i}(a, b) {{\n  const v = a * {i} + b;\n  return v > 0 ? v : -v;\n}}\n"
    for i in range(180)
) + "let flipCd = 0;\nconst FLIP_CD = 1.4;\nfunction update(dt) {\n  flipCd -= dt;\n}\n</script></body></html>\n"


async def main() -> int:
    model_key = sys.argv[1] if len(sys.argv) > 1 else "coder"
    meta = MODELS[model_key]
    root = Path("/tmp/aether-bench")
    root.mkdir(exist_ok=True)
    (root / "game.html").write_text(GAME_HTML, encoding="utf-8")

    chat = {
        "id": "bench",
        "project_id": "bench-project",
        "messages": [{
            "role": "user",
            "content": (
                "In game.html, change FLIP_CD from 1.4 to 0.8 using edit_file with "
                "find/replace. Then reply with the single word DONE."
            ),
        }],
    }
    project = {"id": "bench-project", "name": "bench", "root": str(root), "description": ""}
    settings = {
        "agent_repeat_limit": 3,
        "agent_context": 65536,
        "shell_approval_mode": "never_ask",
        "auto_compact_at": 0.85,
        "compact_keep_recent": 10,
    }

    started = time.monotonic()
    with (
        patch.object(server.st, "get_project", return_value=project),
        patch.object(server.st, "save_chat", side_effect=lambda v: v),
        patch.object(server.st, "load_settings", return_value=settings),
        patch.object(server.st, "history_index", return_value=""),
        patch.object(server.st, "memory_as_prompt", return_value=""),
        patch.object(server.st, "canon_as_prompt", return_value=""),
    ):
        await server.oc.ensure_model_loaded(model_key)
        events = [
            e async for e in server._agent_loop(
                chat, meta, model_key, effort="high", think=False
            )
        ]
    total = time.monotonic() - started

    log = server._TIMING_LOG
    rows = []
    if log and Path(log).exists():
        rows = [json.loads(line) for line in Path(log).read_text().splitlines() if line.strip()]

    print(f"\nmodel        : {meta['id']}")
    print(f"steps        : {len(rows)}")
    print(f"wall clock   : {total:.1f}s")
    if rows:
        sent = [r["prompt_tokens_sent"] for r in rows]
        reproc = [r.get("prompt_eval_count") or 0 for r in rows]
        pe = [r["prompt_eval_s"] for r in rows]
        ev = [r["eval_s"] for r in rows]
        gen_tok = [r.get("eval_count") or 0 for r in rows]
        print(f"prompt sent  : median {statistics.median(sent):.0f} tok")
        print(f"reprocessed  : median {statistics.median(reproc):.0f} tok  "
              f"({100 * statistics.median(reproc) / max(statistics.median(sent), 1):.0f}% of prompt)")
        print(f"prompt eval  : {sum(pe):.1f}s total  "
              f"({sum(reproc) / max(sum(pe), 0.001):.0f} tok/s)")
        print(f"generation   : {sum(ev):.1f}s total, {sum(gen_tok)} tok  "
              f"({sum(gen_tok) / max(sum(ev), 0.001):.0f} tok/s)")
        share = 100 * sum(pe) / max(sum(pe) + sum(ev), 0.001)
        print(f"\n=> {share:.0f}% of model time is prompt processing")
        print("   high % + reprocessed ~= sent  -> KV cache is NOT being reused")
        print("   low  % + slow tok/s          -> the model itself is the cost")
    final = next(
        (e.get("message", {}).get("content", "") for e in reversed(events)
         if e.get("type") == "message" and e.get("message", {}).get("content")),
        "",
    )
    print(f"\nfinal reply  : {final[:80]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
