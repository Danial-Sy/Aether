#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Exercise Aether's real Ollama agent across multi-round, multi-file jobs.

Unlike the unit tests, this suite does not mock model output.  It builds disposable
projects, lets the configured Aether agent inspect and modify them, then checks the
result independently.  Nothing in the Aether checkout is edited by the scenarios.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server
from config import MODELS


@dataclass(frozen=True)
class Scenario:
    name: str
    prompt: str
    files: dict[str, str]
    required_mutations: int
    minimum_rounds: int


SCENARIOS = (
    Scenario(
        name="cross-file-invoice-refactor",
        prompt=(
            "Complete this as one continuous multi-step assignment. Inspect the repository "
            "and maintain a todo plan. Repair the invoice calculations so quantity and a "
            "percentage discount are handled correctly. Add category_quantities(lines), then "
            "update render_receipt so it prints the total followed by alphabetically sorted "
            "'<category>: <quantity>' lines. Update README.md to document the behavior. Run "
            "'python3 -m unittest discover -s tests -v' and keep working until the entire suite "
            "passes. Make the changes; do not stop at an explanation."
        ),
        files={
            "ledger.py": '''"""Small invoice domain module."""\n\n\ndef line_total(unit_price, quantity):\n    return round(unit_price, 2)\n\n\ndef invoice_total(lines, discount_percent=0):\n    subtotal = sum(line_total(line["unit_price"], line["quantity"]) for line in lines)\n    return round(subtotal - discount_percent, 2)\n''',
            "receipt.py": '''from ledger import invoice_total\n\n\ndef render_receipt(lines, discount_percent=0):\n    return f"Total: ${invoice_total(lines, discount_percent):.2f}"\n''',
            "README.md": "# Invoice fixture\n\nA deliberately incomplete invoice example.\n",
            "tests/__init__.py": "",
            "tests/test_invoice.py": '''import unittest\n\nfrom ledger import category_quantities, invoice_total, line_total\nfrom receipt import render_receipt\n\n\nLINES = [\n    {"category": "books", "unit_price": 12.50, "quantity": 2},\n    {"category": "games", "unit_price": 40.00, "quantity": 1},\n    {"category": "books", "unit_price": 5.00, "quantity": 3},\n]\n\n\nclass InvoiceTests(unittest.TestCase):\n    def test_line_total_includes_quantity(self):\n        self.assertEqual(line_total(4.25, 3), 12.75)\n\n    def test_invoice_total_applies_percentage_discount(self):\n        self.assertEqual(invoice_total(LINES, 10), 72.00)\n\n    def test_category_quantities_accumulate(self):\n        self.assertEqual(category_quantities(LINES), {"books": 5, "games": 1})\n\n    def test_receipt_has_sorted_category_lines(self):\n        self.assertEqual(\n            render_receipt(LINES, 10),\n            "Total: $72.00\\nbooks: 5\\ngames: 1",\n        )\n\n\nif __name__ == "__main__":\n    unittest.main()\n''',
        },
        required_mutations=3,
        minimum_rounds=4,
    ),
    Scenario(
        name="dependency-plan-synthesis",
        prompt=(
            "Treat this as a multi-iteration repository assignment, not a one-shot answer. "
            "Inspect every JSON file under components/ and use a todo plan. Create "
            "deployment_plan.json with exactly these top-level keys: startup_order (every "
            "service exactly once, dependencies before dependents), ports_by_service (service "
            "to port), and services_by_owner (owner to alphabetically sorted service names). "
            "Also create RUNBOOK.md with a numbered startup list in that same order and a short "
            "owner section. Do not change component files or tests. Run "
            "'python3 -m unittest discover -s tests -v' and continue until all tests pass."
        ),
        files={
            "components/ingest.json": '{"service":"ingest","owner":"data","port":7101,"dependencies":[]}\n',
            "components/normalize.json": '{"service":"normalize","owner":"data","port":7102,"dependencies":["ingest"]}\n',
            "components/index.json": '{"service":"index","owner":"search","port":7201,"dependencies":["normalize"]}\n',
            "components/api.json": '{"service":"api","owner":"platform","port":7301,"dependencies":["index"]}\n',
            "components/audit.json": '{"service":"audit","owner":"security","port":7401,"dependencies":["ingest"]}\n',
            "components/dashboard.json": '{"service":"dashboard","owner":"platform","port":7302,"dependencies":["api","audit"]}\n',
            "tests/__init__.py": "",
            "tests/test_deployment.py": '''import json\nimport unittest\nfrom pathlib import Path\n\n\nROOT = Path(__file__).resolve().parents[1]\n\n\nclass DeploymentPlanTests(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls):\n        cls.components = {}\n        for path in (ROOT / "components").glob("*.json"):\n            item = json.loads(path.read_text())\n            cls.components[item["service"]] = item\n        cls.plan = json.loads((ROOT / "deployment_plan.json").read_text())\n\n    def test_exact_schema_and_services(self):\n        self.assertEqual(\n            set(self.plan),\n            {"startup_order", "ports_by_service", "services_by_owner"},\n        )\n        self.assertEqual(set(self.plan["startup_order"]), set(self.components))\n        self.assertEqual(len(self.plan["startup_order"]), len(self.components))\n\n    def test_dependencies_precede_dependents(self):\n        positions = {name: i for i, name in enumerate(self.plan["startup_order"])}\n        for name, item in self.components.items():\n            for dependency in item["dependencies"]:\n                self.assertLess(positions[dependency], positions[name])\n\n    def test_ports_and_owner_groups(self):\n        expected_ports = {name: item["port"] for name, item in self.components.items()}\n        self.assertEqual(self.plan["ports_by_service"], expected_ports)\n        expected_owners = {}\n        for name, item in self.components.items():\n            expected_owners.setdefault(item["owner"], []).append(name)\n        expected_owners = {key: sorted(value) for key, value in expected_owners.items()}\n        self.assertEqual(self.plan["services_by_owner"], expected_owners)\n\n    def test_runbook_tracks_generated_plan(self):\n        runbook = (ROOT / "RUNBOOK.md").read_text().lower()\n        cursor = -1\n        for service in self.plan["startup_order"]:\n            next_cursor = runbook.find(service.lower(), cursor + 1)\n            self.assertGreater(next_cursor, cursor)\n            cursor = next_cursor\n        for owner in self.plan["services_by_owner"]:\n            self.assertIn(owner.lower(), runbook)\n\n\nif __name__ == "__main__":\n    unittest.main()\n''',
        },
        required_mutations=2,
        minimum_rounds=4,
    ),
    Scenario(
        name="large-jungle-platformer",
        prompt=(
            "Build a polished self-contained 2D jungle platformer in "
            "jungle_platformer.html. Pressing G must reverse gravity and the level design "
            "must rely heavily on walking and jumping on both floors and ceilings. Support "
            "WASD and arrow keys, with variable-height jumping when W or Up is held. Include "
            "excellent canvas graphics such as layered jungle scenery, animation, particles, "
            "lighting or glow, camera movement, hazards, collectibles, checkpoints, and a clear "
            "goal. Maintain a todo plan, build the actual game, and use chunked file tools when "
            "needed instead of attempting an oversized single tool call. Do not change tests. "
            "Run 'python3 -m unittest discover -s tests -v' and continue until it passes."
        ),
        files={
            "README.md": "# Jungle platformer live fixture\n",
            "tests/__init__.py": "",
            "tests/test_game.py": '''import re\nimport subprocess\nimport unittest\nfrom pathlib import Path\n\n\nROOT = Path(__file__).resolve().parents[1]\nGAME = ROOT / "jungle_platformer.html"\n\n\nclass JunglePlatformerTests(unittest.TestCase):\n    @classmethod\n    def setUpClass(cls):\n        cls.source = GAME.read_text(encoding="utf-8")\n        cls.lower = cls.source.lower()\n\n    def test_substantial_canvas_game_exists(self):\n        self.assertGreater(len(self.source), 12000)\n        self.assertIn("<canvas", self.lower)\n        self.assertIn("requestanimationframe", self.lower)\n\n    def test_requested_mechanics_are_implemented(self):\n        for marker in ("gravity", "keydown", "keyup", "jump", "checkpoint", "particle", "jungle"):\n            self.assertIn(marker, self.lower)\n        self.assertRegex(self.source, re.compile(r"(?:key|code)\\s*(?:===|==|:)\\s*['\\\"](?:g|KeyG)['\\\"]", re.I))\n        self.assertTrue("arrowleft" in self.lower or "arrowright" in self.lower)\n        self.assertTrue("keyw" in self.lower or "['w']" in self.lower or '["w"]' in self.lower)\n\n    def test_embedded_javascript_has_valid_syntax(self):\n        scripts = re.findall(r"<script[^>]*>(.*?)</script>", self.source, re.I | re.S)\n        self.assertTrue(scripts)\n        proc = subprocess.run(\n            ["node", "--check", "-"],\n            input="\\n".join(scripts),\n            text=True,\n            capture_output=True,\n            timeout=30,\n        )\n        self.assertEqual(proc.returncode, 0, proc.stderr)\n\n\nif __name__ == "__main__":\n    unittest.main()\n''',
        },
        required_mutations=1,
        minimum_rounds=5,
    ),
)


def write_fixture(root: Path, scenario: Scenario) -> None:
    for relative, content in scenario.files.items():
        if scenario.name == "large-jungle-platformer" and relative == "tests/test_game.py":
            content = content.replace(
                r"(?:key|code)\s*",
                r"(?:e\.key|key|code|k)\s*",
            )
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def verify_fixture(root: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    details = ((proc.stdout or "") + (proc.stderr or "")).strip()
    game = root / "jungle_platformer.html"
    if game.exists():
        source = game.read_text(encoding="utf-8", errors="replace")
        lower = source.lower()
        closing = lower.rfind("</html>")
        if "[Aether replay elision:" in source:
            return False, (details + "\nInternal replay marker leaked into game source.")[-4000:]
        if closing < 0 or source[closing + len("</html>"):].strip():
            return False, (details + "\nGame source exists outside the closing HTML tag.")[-4000:]
    return proc.returncode == 0, details[-4000:]


async def run_scenario(
    scenario: Scenario,
    model_key: str,
    timeout: int,
    effort: str,
    think: bool,
) -> dict:
    with tempfile.TemporaryDirectory(prefix="aether-agent-complex-") as temp:
        root = Path(temp)
        write_fixture(root, scenario)
        chat = {
            "id": f"live-complex-{scenario.name}",
            "project_id": "fixture-project",
            "messages": [{"role": "user", "content": scenario.prompt}],
        }
        project = {
            "id": "fixture-project",
            "name": scenario.name,
            "root": str(root),
            "description": "Disposable live agent test fixture",
        }
        settings = {
            "agent_repeat_limit": 3,
            "agent_context": 65536,
            "shell_approval_mode": "never_ask",
            "auto_compact_at": 0.85,
            "compact_keep_recent": 10,
        }

        async def collect_events() -> list[dict]:
            events = []
            async for event in server._agent_loop(
                chat,
                MODELS[model_key],
                model_key,
                effort=effort,
                think=think,
            ):
                events.append(event)
                if event.get("type") == "tool_start":
                    print(f"  tool: {event.get('name')}", flush=True)
            return events

        with (
            patch.object(server.st, "get_project", return_value=project),
            patch.object(server.st, "save_chat", side_effect=lambda value: value),
            patch.object(server.st, "load_settings", return_value=settings),
            patch.object(server.st, "history_index", return_value=""),
            patch.object(server.st, "memory_as_prompt", return_value=""),
            patch.object(server.st, "canon_as_prompt", return_value=""),
        ):
            events = await asyncio.wait_for(collect_events(), timeout=timeout)

        tool_names = [
            message.get("name")
            for message in chat["messages"]
            if message.get("role") == "tool"
        ]
        assistant_rounds = sum(
            1 for message in chat["messages"] if message.get("role") == "assistant"
        )
        mutation_count = sum(
            name in {"edit_file", "write_file", "append_file"} for name in tool_names
        )
        final = next(
            (
                event.get("message", {}).get("content", "")
                for event in reversed(events)
                if event.get("type") == "message"
                and event.get("message", {}).get("content")
            ),
            "",
        ).strip()
        verified, verifier_output = verify_fixture(root)
        guard_hits = sum(
            "[loop guard]" in str(event.get("thinking") or "") for event in events
        )
        todo_snapshots = [
            event.get("todos") or []
            for event in events
            if event.get("type") == "todos"
        ]
        todo_done_counts = [
            sum(
                item.get("status") in {"completed", "cancelled"}
                for item in snapshot
            )
            for snapshot in todo_snapshots
        ]
        largest_todo_jump = max(
            (
                current - previous
                for previous, current in zip(todo_done_counts, todo_done_counts[1:])
            ),
            default=0,
        )
        project_refreshes = sum(
            event.get("type") == "project_tree" for event in events
        )
        failures = []
        if not verified:
            failures.append("independent test suite failed")
        if assistant_rounds < scenario.minimum_rounds:
            failures.append(
                f"only {assistant_rounds} model rounds (wanted {scenario.minimum_rounds}+)"
            )
        if mutation_count < scenario.required_mutations:
            failures.append(
                f"only {mutation_count} file mutations (wanted {scenario.required_mutations}+)"
            )
        if "todo_write" not in tool_names:
            failures.append("agent did not maintain a todo plan")
        if "run_shell" not in tool_names:
            failures.append("agent did not run the requested test command")
        if not final:
            failures.append("agent did not produce a final debrief")
        if largest_todo_jump > 1:
            failures.append(
                f"todo progress jumped by {largest_todo_jump} completed items at once"
            )
        if mutation_count and project_refreshes == 0:
            failures.append("file mutations did not emit a project-tree refresh")

        return {
            "scenario": scenario.name,
            "ok": not failures,
            "assistant_rounds": assistant_rounds,
            "tool_calls": len(tool_names),
            "mutations": mutation_count,
            "loop_guard_events": guard_hits,
            "todo_snapshots": len(todo_snapshots),
            "largest_todo_jump": largest_todo_jump,
            "project_refreshes": project_refreshes,
            "tools": tool_names,
            "failures": failures,
            "final": final[:600],
            "verifier_tail": verifier_output[-1200:] if failures else "",
        }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=("agent", "coder"), default="agent")
    parser.add_argument(
        "--effort", choices=("minimal", "low", "medium", "high", "max"), default="high"
    )
    parser.add_argument("--think", action="store_true", help="enable the model's thinking pass")
    parser.add_argument("--timeout", type=int, default=900, help="seconds per scenario")
    parser.add_argument("--scenario", choices=[item.name for item in SCENARIOS])
    args = parser.parse_args()

    selected = [item for item in SCENARIOS if not args.scenario or item.name == args.scenario]
    results = []
    for scenario in selected:
        print(f"\n[{scenario.name}] starting with {MODELS[args.model]['id']}", flush=True)
        try:
            result = await run_scenario(
                scenario, args.model, args.timeout, args.effort, args.think
            )
        except asyncio.TimeoutError:
            result = {
                "scenario": scenario.name,
                "ok": False,
                "failures": [f"timed out after {args.timeout}s"],
            }
        results.append(result)
        print(json.dumps(result, indent=2), flush=True)

    passed = sum(bool(result.get("ok")) for result in results)
    print(f"\nLive complex suite: {passed}/{len(results)} scenarios passed", flush=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
