# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Live check: does delegation actually work against the real model?"""
import asyncio, json, sys, tempfile, shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unittest.mock import patch
import server, storage as st
from config import MODELS

root = Path(tempfile.mkdtemp(prefix="aether-delegate-"))
(root / "auth.py").write_text(
    "\n".join(["# padding" ] * 400) +
    "\n\ndef check_token(tok):\n    '''THE AUTH CHECK LIVES HERE'''\n    return tok == 'ok'\n"
)
(root / "views.py").write_text("\n".join(["# noise"] * 300))
(root / "utils.py").write_text("\n".join(["# more noise"] * 300))

proj = {"id": "delegate-live", "name": "delegate-live", "root": str(root), "description": ""}
settings = {"agent_repeat_limit": 3, "agent_context": 65536,
            "shell_approval_mode": "never_ask", "auto_compact_at": 0.85,
            "compact_keep_recent": 10}

async def main():
    parent = {"id": "live-parent", "project_id": "delegate-live", "mode": "agentic",
              "agent_model": "coder", "messages": []}
    meta = await server.oc.ensure_model_loaded("coder")
    events = []
    with (patch.object(st, "load_settings", return_value=settings),
          patch.object(st, "get_project", return_value=proj),
          patch.object(st, "save_chat", side_effect=lambda c: c)):
        async for ev in server._run_subagent(
            parent,
            {"task": f"In the project at {root}, find which file and function performs "
                     f"the authentication token check. Report the file and function name."},
            meta, "coder",
        ):
            events.append(ev)
    res = next((e["result"] for e in events if e.get("type") == "subagent_result"), None)
    p = json.loads(res)
    print("=== delegation result ===")
    print("  rounds       :", p.get("rounds"))
    print("  tools_tried  :", p.get("tools_attempted"))
    print("  report chars :", len(p.get("report") or ""))
    print("  report       :", (p.get("report") or "")[:400].replace("\n", " "))
    print("  found it     :", "check_token" in (p.get("report") or ""))
    print("  payload size :", len(res), "chars returned to the parent")

asyncio.run(main())
shutil.rmtree(root, ignore_errors=True)
