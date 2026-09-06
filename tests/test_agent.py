# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
import json
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import agent
from config import ROOT


class AgentToolTests(unittest.TestCase):
    def test_shell_inspection_classifier_separates_searches_from_validation(self):
        self.assertTrue(agent.shell_is_inspection('grep -n "cooldown" jungle.html'))
        self.assertTrue(agent.shell_is_inspection("rg gravity . | head -20"))
        self.assertTrue(agent.shell_is_inspection("git diff -- jungle.html"))
        self.assertFalse(agent.shell_is_inspection("node --check game.js"))
        self.assertFalse(agent.shell_is_inspection("python3 -m unittest"))

    def test_todo_plan_auto_promotes_first_pending_item(self):
        chat = {}
        result = json.loads(agent.run_tool(
            "todo_write",
            {
                "merge": False,
                "todos": [
                    {"id": "inspect", "content": "Inspect files", "status": "pending"},
                    {"id": "build", "content": "Build feature", "status": "pending"},
                ],
            },
            None,
            chat=chat,
        ))
        self.assertTrue(result["ok"])
        self.assertEqual(chat["agent_todos"][0]["status"], "in_progress")
        self.assertEqual(chat["agent_todos"][1]["status"], "pending")

    def test_todo_completion_auto_promotes_next_milestone(self):
        chat = {
            "agent_todos": [
                {"id": "inspect", "content": "Inspect files", "status": "in_progress"},
                {"id": "build", "content": "Build feature", "status": "pending"},
            ]
        }
        result = json.loads(agent.run_tool(
            "todo_write",
            {
                "merge": True,
                "todos": [
                    {"id": "inspect", "content": "Inspect files", "status": "completed"},
                ],
            },
            None,
            chat=chat,
        ))
        self.assertTrue(result["ok"])
        self.assertEqual(chat["agent_todos"][0]["status"], "completed")
        self.assertEqual(chat["agent_todos"][1]["status"], "in_progress")

    def test_batch_completion_applies_one_milestone_and_defers_the_rest(self):
        """Rejecting the whole call advanced nothing, which livelocked the run.

        With open todos the runtime refuses every debrief, so a model trying to
        close out its plan could neither finish the plan nor end the turn.
        """
        chat = {"agent_todos": [
            {"id": "inspect", "content": "Inspect files", "status": "in_progress"},
            {"id": "build", "content": "Build feature", "status": "pending"},
            {"id": "verify", "content": "Verify feature", "status": "pending"},
        ]}
        batch = {"merge": True, "todos": [
            {"id": "inspect", "status": "completed"},
            {"id": "build", "status": "completed"},
            {"id": "verify", "status": "completed"},
        ]}
        result = json.loads(agent.run_tool("todo_write", batch, None, chat=chat))

        self.assertTrue(result["ok"])
        self.assertEqual(result["partial"]["completed_now"], "inspect")
        self.assertEqual(result["partial"]["deferred"], ["build", "verify"])
        # Exactly one milestone moved; the others kept their previous status.
        self.assertEqual(chat["agent_todos"][0]["status"], "completed")
        self.assertEqual(chat["agent_todos"][1]["status"], "in_progress")  # auto-promoted
        self.assertEqual(chat["agent_todos"][2]["status"], "pending")

    def test_repeating_the_same_batch_drives_the_plan_to_zero(self):
        # Each call must advance, so a model that keeps resending its whole list
        # converges instead of looping forever.
        chat = {"agent_todos": [
            {"id": "a", "content": "A", "status": "in_progress"},
            {"id": "b", "content": "B", "status": "pending"},
            {"id": "c", "content": "C", "status": "pending"},
        ]}
        batch = {"merge": True, "todos": [
            {"id": "a", "status": "completed"},
            {"id": "b", "status": "completed"},
            {"id": "c", "status": "completed"},
        ]}
        opens = []
        for _ in range(3):
            opens.append(json.loads(agent.run_tool("todo_write", batch, None, chat=chat))["open"])
        self.assertEqual(opens, [2, 1, 0])

    def test_large_file_tool_schemas_publish_native_chunk_limits(self):
        definitions = {
            item["function"]["name"]: item["function"] for item in agent.TOOL_DEFS
        }
        self.assertEqual(
            definitions["write_file"]["parameters"]["properties"]["content"]["maxLength"],
            12000,
        )
        self.assertEqual(
            definitions["append_file"]["parameters"]["properties"]["content"]["maxLength"],
            12000,
        )

    def test_write_file_validates_python(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            result = json.loads(agent.run_tool(
                "write_file",
                {"path": "example.py", "content": "answer = 42\n"},
                root,
            ))
            self.assertTrue(result["ok"])
            self.assertTrue(result["validation"]["ok"])

    def test_write_file_reports_syntax_error(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "broken.py"
            result = json.loads(agent.run_tool(
                "write_file",
                {"path": "broken.py", "content": "def broken(:\n"},
                root,
            ))
            self.assertFalse(result["ok"])
            self.assertFalse(result["changed"])
            self.assertFalse(result["validation"]["ok"])
            self.assertFalse(target.exists())

    @unittest.skipUnless(shutil.which("eslint"), "ESLint is optional")
    def test_edit_file_transaction_rejects_undefined_javascript_identifiers(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "game.html"
            original = "<script>\nfunction burst(){\n  const a=1,v=2;\n  return a+v;\n}\n</script>\n"
            target.write_text(original, encoding="utf-8")
            result = json.loads(agent.run_tool(
                "edit_file",
                {
                    "path": "game.html",
                    "start_line": 3,
                    "end_line": 3,
                    "content": "  const cooldown=1;\n  return a+v;",
                },
                root,
            ))

            self.assertFalse(result["ok"])
            self.assertFalse(result["changed"])
            self.assertIn("no-undef", result["validation"]["validator"])
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_append_file_builds_large_artifact_without_resending_prefix(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "game.html"
            target.write_text("<main>\n", encoding="utf-8")
            result = json.loads(agent.run_tool(
                "append_file",
                {"path": "game.html", "content": "  <canvas></canvas>\n</main>\n"},
                root,
            ))
            self.assertTrue(result["ok"])
            self.assertEqual(result["appended_bytes"], 28)
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "<main>\n  <canvas></canvas>\n</main>\n",
            )

    def test_append_file_requires_an_existing_foundation(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            result = json.loads(agent.run_tool(
                "append_file",
                {"path": "missing.html", "content": "chunk"},
                Path(folder),
            ))
            self.assertIn("error", result)

    def test_append_file_can_insert_before_closing_marker(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "game.html"
            target.write_text("<script>\n</script>\n</html>\n", encoding="utf-8")
            result = json.loads(agent.run_tool(
                "append_file",
                {
                    "path": "game.html",
                    "content": "function draw() {}\n",
                    "before": "</script>",
                },
                root,
            ))
            self.assertTrue(result["ok"])
            self.assertEqual(result["mode"], "insert")
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "<script>\nfunction draw() {}\n</script>\n</html>\n",
            )

    def test_append_file_missing_marker_preserves_file(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "game.html"
            original = "<html></html>\n"
            target.write_text(original, encoding="utf-8")
            result = json.loads(agent.run_tool(
                "append_file",
                {"path": "game.html", "content": "chunk", "before": "</script>"},
                root,
            ))
            self.assertFalse(result["changed"])
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_append_file_refuses_content_after_closed_html(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "game.html"
            original = "<html><script></script></html>\n"
            target.write_text(original, encoding="utf-8")
            result = json.loads(agent.run_tool(
                "append_file",
                {"path": "game.html", "content": "function draw() {}\n"},
                root,
            ))
            self.assertFalse(result["changed"])
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_file_tools_reject_replay_elision_markers(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "game.js"
            target.write_text("const ready = true;\n", encoding="utf-8")
            marker = "[Aether replay elision: 5000 characters]"
            append_result = json.loads(agent.run_tool(
                "append_file", {"path": "game.js", "content": marker}, root
            ))
            write_result = json.loads(agent.run_tool(
                "write_file", {"path": "other.js", "content": marker}, root
            ))
            self.assertFalse(append_result["changed"])
            self.assertFalse(write_result["changed"])
            self.assertEqual(target.read_text(encoding="utf-8"), "const ready = true;\n")
            self.assertFalse((root / "other.js").exists())

    def test_a_missed_anchor_reports_the_closest_real_lines(self):
        # Refusing without showing what IS there costs a reread per attempt, and
        # that reread is what the redundant-read guard blocks. The observed loop
        # was ten identical retries of one edit.
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            (root / "a.js").write_text(
                "const TILE=40;\nconst FLIP_CD=1.8; // seconds\nconst X=1;\n",
                encoding="utf-8",
            )
            result = json.loads(agent.run_tool(
                "edit_file",
                {"path": "a.js", "find": "const FLIP_CD=5.0; // seconds",
                 "replace": "const FLIP_CD=2.0; // seconds"},
                root,
            ))
            self.assertEqual(result["error"], "Anchor text not found")
            self.assertEqual(result["nearest"][0]["line"], 2)
            self.assertEqual(result["nearest"][0]["text"], "const FLIP_CD=1.8; // seconds")

    def test_anchored_edit_without_a_replacement_is_refused(self):
        """`replace` absent is not `replace: ""`.

        Coercing the missing key to empty turned the call into a silent
        deletion that still reported ok. Measured on one run: 33 such calls
        removed ~10k characters of the user's file, after which the anchors for
        every later edit no longer existed.
        """
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "example.js"
            original = "const one = 1;\nconst two = 2;\n"
            target.write_text(original, encoding="utf-8")
            result = json.loads(agent.run_tool(
                "edit_file",
                {"path": "example.js", "find": "const two = 2;"},
                root,
            ))
            self.assertIn("no replacement", result["error"].lower())
            self.assertFalse(result["changed"])
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_anchored_edit_accepts_content_as_the_replacement(self):
        # `content` is the line-range form's field; find + content plainly means
        # it as the replacement, and that is the shape the model actually sends.
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "example.js"
            target.write_text("const one = 1;\nconst two = 2;\n", encoding="utf-8")
            result = json.loads(agent.run_tool(
                "edit_file",
                {"path": "example.js", "find": "const two = 2;",
                 "content": "const two = 22;"},
                root,
            ))
            self.assertTrue(result["ok"], result)
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "const one = 1;\nconst two = 22;\n",
            )

    def test_an_explicit_empty_replace_still_deletes(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "example.js"
            target.write_text("const one = 1;\nconst two = 2;\n", encoding="utf-8")
            result = json.loads(agent.run_tool(
                "edit_file",
                {"path": "example.js", "find": "const two = 2;\n", "replace": ""},
                root,
            ))
            self.assertTrue(result["ok"], result)
            self.assertEqual(target.read_text(encoding="utf-8"), "const one = 1;\n")

    def test_validate_file_fails_on_an_unclosed_script_block(self):
        """Failing open here is how a shredded file passed its own gate.

        Once </script> was destroyed the block regex matched nothing, the scan
        reported "No inline scripts" with ok=true, and every later edit
        committed with no JavaScript check at all.
        """
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "game.html"
            target.write_text("<html><body><script>\nlet a = 1;\n", encoding="utf-8")
            result = json.loads(agent.run_tool("validate_file", {"path": "game.html"}, root))
            self.assertFalse(result["ok"])
            self.assertIn("Unclosed <script>", result["error"])

    def test_validate_file_still_passes_html_with_no_scripts(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "page.html"
            target.write_text("<html><body><p>hi</p></body></html>\n", encoding="utf-8")
            result = json.loads(agent.run_tool("validate_file", {"path": "page.html"}, root))
            self.assertTrue(result["ok"], result)

    def test_an_edit_that_changes_nothing_is_not_reported_as_success(self):
        # Progress theatre: it reset the stall counters and satisfied the guard
        # that stops a milestone completing with no successful change behind it.
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "example.js"
            target.write_text("const FLIP_CD = 3500;\n", encoding="utf-8")
            result = json.loads(agent.run_tool(
                "edit_file",
                {"path": "example.js", "find": "const FLIP_CD = 3500;",
                 "replace": "const FLIP_CD = 3500;"},
                root,
            ))
            self.assertIn("changed nothing", result["error"].lower())
            self.assertFalse(result["changed"])
            self.assertEqual(result["edits_applied"], 0)

    def test_edit_file_replaces_only_requested_lines(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "example.js"
            target.write_text("const one = 1;\nconst two = 2;\nconst three = 3;\n", encoding="utf-8")
            result = json.loads(agent.run_tool(
                "edit_file",
                {
                    "path": "example.js",
                    "start_line": 2,
                    "end_line": 2,
                    "content": "const two = 22;",
                },
                root,
            ))
            self.assertTrue(result["ok"])
            self.assertTrue(result["validation"]["ok"])
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "const one = 1;\nconst two = 22;\nconst three = 3;\n",
            )

    def test_edit_file_batches_ranges_against_original_lines(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "batch.txt"
            target.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
            result = json.loads(agent.run_tool(
                "edit_file",
                {
                    "path": "batch.txt",
                    "edits": [
                        {"start_line": 1, "end_line": 1, "content": "ONE"},
                        {"start_line": 4, "end_line": 4, "content": "FOUR"},
                    ],
                },
                root,
            ))
            self.assertTrue(result["ok"])
            self.assertEqual(result["edits_applied"], 2)
            self.assertEqual(target.read_text(encoding="utf-8"), "ONE\ntwo\nthree\nFOUR\n")

    def test_invalid_edit_is_transactional(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "transaction.js"
            original = "const answer = 42;\n"
            target.write_text(original, encoding="utf-8")
            result = json.loads(agent.run_tool(
                "edit_file",
                {"path": "transaction.js", "start_line": 1, "end_line": 1, "content": "const = ;"},
                root,
            ))
            self.assertFalse(result["ok"])
            self.assertFalse(result["changed"])
            self.assertFalse(result["validation"]["ok"])
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_edit_file_accepts_fifteen_transactional_ranges(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "many.txt"
            original = "".join(f"line {i}\n" for i in range(20))
            target.write_text(original, encoding="utf-8")
            edits = [
                {"start_line": i, "end_line": i, "content": f"changed {i}"}
                for i in range(1, 16)
            ]
            result = json.loads(agent.run_tool(
                "edit_file", {"path": "many.txt", "edits": edits}, root
            ))
            self.assertTrue(result["ok"])
            self.assertEqual(result["edits_applied"], 15)
            self.assertIn("changed 15", target.read_text(encoding="utf-8"))

    def test_edit_file_rejects_excessive_total_payload_before_writing(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "large.txt"
            original = "line one\nline two\n"
            target.write_text(original, encoding="utf-8")
            result = json.loads(agent.run_tool(
                "edit_file",
                {
                    "path": "large.txt",
                    "edits": [{
                        "start_line": 1,
                        "end_line": 1,
                        "content": "x" * (agent.MAX_EDIT_TOTAL_CHARS + 1),
                    }],
                },
                root,
            ))
            self.assertFalse(result["changed"])
            self.assertEqual(result["maximum_chars"], agent.MAX_EDIT_TOTAL_CHARS)
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_shell_classifier_keeps_checks_fast(self):
        self.assertEqual(agent.classify_shell("node --check app.js")[0], "safe")
        self.assertEqual(agent.classify_shell("python3 -m py_compile app.py")[0], "safe")

    def test_ordinary_development_commands_run_unattended(self):
        # safe_auto is a denylist: running the project's own code, editing its
        # files and fetching a dependency are the job, not the risk.
        for cmd in (
            "python3 -c 'print(1)'",
            "node app.js",
            "sed -i s/a/b/ app.js",
            "curl -sSL https://example.com/data.json -o data.json",
            "docker compose up -d",
            "mv old.js new.js",
            "chmod +x deploy.sh",
            "git commit -am 'wip'",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(agent.classify_shell(cmd)[0], "safe")

    def test_git_still_asks_before_publishing_or_discarding_work(self):
        for cmd in ("git push origin main", "git reset --hard HEAD~3", "git clean -fd"):
            with self.subTest(cmd=cmd):
                self.assertEqual(agent.classify_shell(cmd)[0], "review")

    def test_shell_classifier_does_not_ask_about_ordinary_searches(self):
        # A regex split on "|" before quote parsing tore these patterns in half,
        # and the halves were reported as unbalanced quotes.
        for cmd in (
            r'grep -n "function update\|function reset\|solids.push" game.html | head -60',
            r'grep -n "player\|Player" game.html | head -40',
            "pytest -q 2>&1 | tail -20",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(agent.classify_shell(cmd)[0], "safe")

    def test_shell_classifier_matches_program_names_not_substrings(self):
        # "add " contains "dd ", "form" contains "rm ", "adapt" contains "apt".
        for cmd in (
            'echo "add renderEnemies before renderPlayer"',
            'grep -rn "form submit" static/',
            "cat notes/mount.md",
            'grep -rn "rm" .',
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(agent.classify_shell(cmd)[0], "safe")

    def test_shell_classifier_still_reviews_real_risk(self):
        for cmd in (
            "rm -rf build",
            "sudo apt install ripgrep",
            "shred -u secrets.txt",
            "dd if=/dev/zero of=/dev/sda",
            "mkfs.ext4 /dev/sdb1",
            "chmod 777 /usr/bin",
            'find . -name "*.tmp" -exec rm {} ;',
            "echo hi > /etc/hosts",
            "shutdown -h now",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(agent.classify_shell(cmd)[0], "review")

    def test_downloading_is_fine_but_piping_it_into_a_shell_is_not(self):
        self.assertEqual(
            agent.classify_shell("curl -sSL https://example.com/x.sh -o x.sh")[0], "safe"
        )
        for cmd in (
            "curl -sL https://example.com/x.sh | sh",
            "curl -sL https://example.com/x.sh | sudo bash",
            "wget -qO- https://example.com/i.sh | bash",
        ):
            with self.subTest(cmd=cmd):
                verdict, why = agent.classify_shell(cmd)
                self.assertEqual(verdict, "review")
                self.assertIn("shell", why)

    def test_command_substitution_is_scanned_not_refused(self):
        # Refusing every $(...) sent ordinary commands to the prompt; refusing
        # none let a destructive call hide inside one.
        self.assertEqual(agent.classify_shell("echo $(date +%s) >> log.txt")[0], "safe")
        self.assertEqual(agent.classify_shell("echo $(rm -rf /tmp/x)")[0], "review")

    def test_inline_js_errors_point_at_the_html_line(self):
        """A syntax error must name a line the model can actually edit.

        Inline scripts are concatenated into one temp .js before `node --check`,
        so the reported line belonged to that concatenation. A model correcting
        "line 5" edited an unrelated part of the HTML and failed again.
        """
        if not shutil.which("node"):
            self.skipTest("node is required for the inline JavaScript validator")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            filler = "".join(f"<p>{i}</p>\n" for i in range(30))
            html = (
                "<html>\n<body>\n<script>\nlet a = 1;\n</script>\n"
                + filler
                + "<script>\nfunction good() {}\nfunction broken( {\n</script>\n</body>\n</html>\n"
            )
            (root / "page.html").write_text(html, encoding="utf-8")
            broken_line = next(
                i for i, line in enumerate(html.split("\n"), 1) if "broken" in line
            )

            result = json.loads(agent.run_tool(
                "validate_file", {"path": "page.html"}, root, None
            ))

            self.assertFalse(result["ok"])
            error = result["error"]
            # Names the real file, never the temp concatenation.
            self.assertIn("page.html:", error)
            self.assertNotIn("aether-inline-", error)
            # And a line at or after the break, not one from the concatenation.
            reported = int(error.split("page.html:")[1].split("\n")[0])
            self.assertGreaterEqual(reported, broken_line)
            self.assertLessEqual(reported, len(html.split("\n")))
            # Node's own stack frames are not actionable and are dropped.
            self.assertNotIn("at internalCompileFunction", error)
            self.assertNotIn("Node.js v", error)

    def test_inline_line_mapping_handles_each_script_block(self):
        # block one starts at html line 10, block two at html line 100
        line_map = [(1, 10), (12, 100)]
        self.assertEqual(agent._map_inline_line(1, line_map), 10)
        self.assertEqual(agent._map_inline_line(5, line_map), 14)
        self.assertEqual(agent._map_inline_line(12, line_map), 100)
        self.assertEqual(agent._map_inline_line(15, line_map), 103)

    def test_copying_an_upload_into_the_project_does_not_prompt(self):
        # mkdir and touch already auto-run; copying an uploaded asset into the
        # project is the same class of operation and interrupted a run.
        self.assertEqual(agent.classify_shell(
            'cp "/home/u/uploads/track name - artist.mp3" /home/u/game/music.mp3'
        )[0], "safe")
        self.assertEqual(agent.classify_shell("cp -r assets/ build/")[0], "safe")

    def test_copying_into_a_system_path_still_asks(self):
        for cmd in ("cp payload /etc/cron.d/job", "cp x /usr/bin/ls", "cp a /boot/b"):
            with self.subTest(cmd=cmd):
                verdict, why = agent.classify_shell(cmd)
                self.assertEqual(verdict, "review")
                self.assertIn("system path", why)

    def test_anchored_edits_survive_each_other_in_one_batch(self):
        """Line numbers go stale after every applied edit; anchors do not.

        The observed loop was read x5, edit, read x7, edit. The model
        recalibrating line numbers it had just invalidated.
        """
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "game.html"
            target.write_text(
                "<script>\nlet flipCd = 0;\nconst FLIP_CD = 1.4;\n"
                "function update(dt){\n  flipCd -= dt;\n}\nfunction draw(){}\n</script>\n",
                encoding="utf-8",
            )
            result = json.loads(agent.run_tool("edit_file", {
                "path": "game.html",
                "edits": [
                    {"find": "const FLIP_CD = 1.4;", "replace": "const FLIP_CD = 0.8;"},
                    {"find": "function draw(){}",
                     "replace": "function draw(){ drawEnemies(); }\nfunction drawEnemies(){}"},
                ],
            }, root))

            self.assertTrue(result["ok"], result)
            self.assertTrue(result["changed"])
            self.assertEqual(result["mode"], "anchored")
            body = target.read_text(encoding="utf-8")
            self.assertIn("const FLIP_CD = 0.8;", body)
            self.assertIn("function drawEnemies(){}", body)

    def test_an_ambiguous_anchor_is_rejected_with_the_count(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "game.html"
            original = "<script>\nfunction a(){}\nfunction b(){}\n</script>\n"
            target.write_text(original, encoding="utf-8")
            result = json.loads(agent.run_tool(
                "edit_file", {"path": "game.html", "find": "function", "replace": "fn"}, root
            ))
            self.assertEqual(result["occurrences"], 2)
            self.assertFalse(result["changed"])
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_a_missing_anchor_says_so_and_changes_nothing(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "game.html"
            original = "<script>\nlet a = 1;\n</script>\n"
            target.write_text(original, encoding="utf-8")
            result = json.loads(agent.run_tool(
                "edit_file", {"path": "game.html", "find": "nope()", "replace": "x"}, root
            ))
            self.assertIn("not found", result["error"])
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_an_anchored_batch_rolls_back_as_one_transaction(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "game.html"
            original = "<script>\nlet a = 1;\nfunction draw(){}\n</script>\n"
            target.write_text(original, encoding="utf-8")
            result = json.loads(agent.run_tool("edit_file", {
                "path": "game.html",
                "edits": [
                    {"find": "let a = 1;", "replace": "let a = 2;"},
                    {"find": "function draw(){}", "replace": "function draw( {"},
                ],
            }, root))
            self.assertFalse(result["ok"])
            self.assertFalse(result["changed"])
            # The good edit is rolled back with the bad one.
            self.assertEqual(target.read_text(encoding="utf-8"), original)
            # And the error names the real file, not the temp copy it checked.
            self.assertIn("game.html:", result["validation"]["error"])
            self.assertNotIn("aether-", result["validation"]["error"])

    def test_mixing_anchored_and_line_edits_is_refused(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            (root / "game.html").write_text("<script>\nlet a = 1;\n</script>\n", encoding="utf-8")
            result = json.loads(agent.run_tool("edit_file", {
                "path": "game.html",
                "edits": [
                    {"find": "let a = 1;", "replace": "let a = 2;"},
                    {"start_line": 1, "end_line": 1, "content": "<script>"},
                ],
            }, root))
            self.assertIn("Mixed", result["error"])

    def test_a_merge_update_can_close_a_todo_by_id_alone(self):
        # Requiring content on every update rejected the obvious call and the
        # model burned steps guessing at the shape.
        chat = {"agent_todos": [
            {"id": "copy-mp3", "content": "Copy the mp3", "status": "in_progress"},
            {"id": "music", "content": "Wire the music", "status": "pending"},
        ]}
        result = json.loads(agent.run_tool(
            "todo_write",
            {"merge": True, "todos": [{"id": "copy-mp3", "status": "completed"}]},
            None, chat=chat,
        ))
        self.assertTrue(result["ok"])
        self.assertEqual(chat["agent_todos"][0]["status"], "completed")
        # Content is inherited, not blanked.
        self.assertEqual(chat["agent_todos"][0]["content"], "Copy the mp3")
        self.assertEqual(chat["agent_todos"][1]["status"], "in_progress")

    def test_a_brand_new_todo_still_needs_content(self):
        chat = {"agent_todos": [{"id": "a", "content": "Existing", "status": "in_progress"}]}
        result = json.loads(agent.run_tool(
            "todo_write",
            {"merge": True, "todos": [{"id": "brand-new", "status": "pending"}]},
            None, chat=chat,
        ))
        self.assertIn("error", result)
        self.assertIn("id, content and status", result["hint"])

    def test_completing_only_items_that_are_not_active_is_still_refused(self):
        # Silently completing the active milestone the model did not mention
        # would assert work it never claimed to have done.
        chat = {"agent_todos": [
            {"id": "music", "content": "Wire music", "status": "in_progress"},
            {"id": "cooldown", "content": "Shorten cooldown", "status": "pending"},
            {"id": "enemies", "content": "Add enemies", "status": "pending"},
        ]}
        result = json.loads(agent.run_tool("todo_write", {"merge": True, "todos": [
            {"id": "cooldown", "status": "completed"},
            {"id": "enemies", "status": "completed"},
        ]}, None, chat=chat))

        self.assertFalse(result["changed"])
        self.assertEqual(
            result["send_exactly"],
            {"merge": True, "todos": [{"id": "music", "status": "completed"}]},
        )
        follow_up = json.loads(agent.run_tool(
            "todo_write", result["send_exactly"], None, chat=chat
        ))
        self.assertTrue(follow_up["ok"])
        self.assertEqual(chat["agent_todos"][0]["status"], "completed")

    def test_out_of_order_completion_names_the_active_milestone(self):
        chat = {"agent_todos": [
            {"id": "music", "content": "Wire music", "status": "in_progress"},
            {"id": "enemies", "content": "Add enemies", "status": "pending"},
        ]}
        result = json.loads(agent.run_tool("todo_write", {"merge": True, "todos": [
            {"id": "enemies", "status": "completed"},
        ]}, None, chat=chat))
        self.assertEqual(result["active_id"], "music")
        self.assertEqual(result["send_exactly"]["todos"][0]["id"], "music")

    def test_write_file_will_not_touch_an_existing_file(self):
        """A file is written once and edited from then on.

        Letting write_file replace an existing file makes every call a chance to
        drop whatever the model did not retype.
        """
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "game.html"
            original = "<script>\nlet a = 1;\nlet b = 2;\n</script>\n"
            target.write_text(original, encoding="utf-8")

            # Even growing content is refused: this is edit_file's job.
            result = json.loads(agent.run_tool("write_file", {
                "path": "game.html",
                "content": original.replace("</script>", "let c = 3;\n</script>"),
            }, root))

            self.assertFalse(result["changed"])
            self.assertIn("use edit_file", result["error"])
            self.assertEqual(result["existing_lines"], 5)
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_write_file_creates_a_file_that_does_not_exist(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            result = json.loads(agent.run_tool("write_file", {
                "path": "fresh.html", "content": "<script>\nlet a = 1;\n</script>\n",
            }, root))
            self.assertTrue(result["changed"])
            self.assertTrue((root / "fresh.html").is_file())

    def test_explicit_overwrite_is_allowed_but_still_cannot_gut_the_file(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "game.html"
            big = "<script>\n" + "".join(
                f"function helper{i}(){{ return {i}; }}\n" for i in range(300)
            ) + "</script>\n"
            target.write_text(big, encoding="utf-8")

            # A genuine same-size rewrite goes through.
            rewritten = big.replace("return 0;", "return 42;")
            self.assertTrue(json.loads(agent.run_tool("write_file", {
                "path": "game.html", "content": rewritten, "overwrite": True,
            }, root))["changed"])

            # Gutting it does not, even with the flag set.
            result = json.loads(agent.run_tool("write_file", {
                "path": "game.html",
                "content": "<script>\nfunction helper0(){ return 0; }\n</script>\n",
                "overwrite": True,
            }, root))
            self.assertFalse(result["changed"])
            self.assertIn("ask_user", result["hint"])
            self.assertEqual(target.read_text(encoding="utf-8"), rewritten)

    def test_write_file_refuses_to_shrink_a_substantial_file(self):
        """One write_file regenerated a 687-line game as 16k chars.

        Everything the model did not reproduce was destroyed, and recovering it
        meant stitching the file back out of chat transcripts.
        """
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "game.html"
            original = "<script>\n" + "".join(
                f"function helper{i}(){{ return {i}; }}\n" for i in range(300)
            ) + "</script>\n"
            target.write_text(original, encoding="utf-8")

            result = json.loads(agent.run_tool("write_file", {
                "path": "game.html",
                "content": "<script>\nfunction helper0(){ return 0; }\n</script>\n",
            }, root))

            self.assertFalse(result["changed"])
            self.assertIn("edit_file", result["error"])
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_write_file_still_allows_growth_and_small_files(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            big = "<script>\n" + "".join(
                f"function helper{i}(){{ return {i}; }}\n" for i in range(300)
            ) + "</script>\n"
            target = root / "game.html"
            target.write_text(big, encoding="utf-8")
            grown = big.replace("</script>", "function extra(){ return 1; }\n</script>")
            self.assertTrue(json.loads(agent.run_tool(
                "write_file",
                {"path": "game.html", "content": grown, "overwrite": True},
                root,
            ))["changed"])

    def test_every_overwrite_leaves_a_recoverable_snapshot(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "snap.html"
            target.write_text("<script>\nlet a = 1;\n</script>\n", encoding="utf-8")
            agent.run_tool(
                "edit_file",
                {"path": "snap.html", "find": "let a = 1;", "replace": "let a = 2;"},
                root,
            )
            backups = agent.BACKUP_DIR / "snap.html"
            try:
                saved = [f.read_text(encoding="utf-8") for f in backups.iterdir()]
                self.assertTrue(any("let a = 1;" in body for body in saved), saved)
            finally:
                shutil.rmtree(backups, ignore_errors=True)

    def test_internal_loop_placeholder_is_never_a_real_answer(self):
        self.assertTrue(agent.is_internal_continuation_placeholder(
            "(continuing tools for the same job)"
        ))
        self.assertTrue(agent.is_internal_continuation_placeholder(
            "(working notes, continue the same job; not a final answer yet)"
        ))
        self.assertFalse(agent.is_internal_continuation_placeholder(
            "I updated the file and verified its JavaScript syntax."
        ))

    def test_large_write_body_is_elided_only_for_replay(self):
        body = "const value = 1;\n" * 500
        stored = [{"name": "write_file", "arguments": {"path": "app.js", "content": body}}]
        normal = agent.to_ollama_tool_calls(stored)
        replay = agent.to_ollama_tool_calls(stored, elide_large_inputs=True)
        self.assertEqual(normal[0]["function"]["arguments"]["content"], body)
        replay_body = replay[0]["function"]["arguments"]["content"]
        self.assertIn("Aether replay elision", replay_body)
        self.assertLess(len(replay_body), 300)

    def test_large_append_body_is_elided_for_replay(self):
        body = "function draw() {}\n" * 500
        replay = agent.to_ollama_tool_calls(
            [{"name": "append_file", "arguments": {"path": "game.js", "content": body}}],
            elide_large_inputs=True,
        )
        self.assertIn("Aether replay elision", replay[0]["function"]["arguments"]["content"])

    def test_read_results_include_revision_for_cross_turn_reuse(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "revision.txt"
            target.write_text("one\ntwo\nthree\n", encoding="utf-8")
            result = json.loads(agent.run_tool(
                "read_file", {"path": "revision.txt", "start_line": 1, "end_line": 2}, root
            ))
            self.assertTrue(result["revision"])
            normalized = agent.normalize_read_request(
                {"path": "revision.txt", "start_line": 1, "end_line": 2}, root
            )
            self.assertEqual(normalized, (str(target), 1, 2, result["revision"]))

    def test_large_read_is_bounded_at_a_line_boundary(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            root = Path(folder)
            target = root / "large.txt"
            target.write_text("".join(f"line {i} with some content\n" for i in range(1000)), encoding="utf-8")
            result = json.loads(agent.run_tool(
                "read_file", {"path": "large.txt", "start_line": 1, "end_line": 1000}, root
            ))
            self.assertTrue(result["truncated"])
            self.assertLess(result["end"], 1000)
            self.assertEqual(result["requested_end"], 1000)
            self.assertLessEqual(len(result["content"]), agent.READ_FILE_CONTENT_MAX_CHARS)
            self.assertIn(f"continue at line {result['end'] + 1}", result["hint"])


if __name__ == "__main__":
    unittest.main()


class SearchRoutingTests(unittest.TestCase):
    """The web router must not hijack agentic build requests."""

    BUILD_REQUEST = (
        "so enemies weren't actually added into the game, there are none. I also want to "
        "make the cool down a little shorter, and make the environment more consistent. "
        "also isntead of your 8 bit musive ive added a mp3 file a jungle music one, use "
        "the provided in chat mp3 instead ok?"
    )

    def setUp(self):
        import search_router
        self.sr = search_router

    def test_a_build_request_does_not_trigger_a_web_search(self):
        # "your 8" matched the word+number entity pattern and the trailing "ok?"
        # satisfied the question check, so an ordinary implementation request was
        # answered with YouTube links.
        decision = self.sr.route_search(self.BUILD_REQUEST, messages=[], agentic=True)
        self.assertFalse(decision.search)

    def test_agentic_still_searches_when_actually_asked(self):
        for msg in (
            "search the web for canvas sprite batching techniques",
            "look up the Web Audio API decodeAudioData signature",
        ):
            with self.subTest(msg=msg):
                self.assertTrue(
                    self.sr.route_search(msg, messages=[], agentic=True).search
                )

    def test_the_ui_toggle_still_forces_a_search_in_agentic_mode(self):
        decision = self.sr.route_search(
            self.BUILD_REQUEST, messages=[], agentic=True, force_on=True
        )
        self.assertTrue(decision.search)

    def test_chat_mode_question_routing_is_unchanged(self):
        # Static facts already routed "no" before this change; these are the
        # current-information questions the router exists for.
        for msg in ("who won the 2026 F1 championship?", "what's the weather in Toronto?"):
            with self.subTest(msg=msg):
                self.assertTrue(self.sr.route_search(msg, messages=[]).search)

    def test_a_trailing_ok_does_not_make_a_long_instruction_a_question(self):
        # Guards the tightened entity-question rule outside agentic mode too.
        self.assertFalse(self.sr._entity_question(self.BUILD_REQUEST))
        self.assertTrue(self.sr._entity_question("what is Godot 4 tick rate?"))


class ToolOutputOffloadTests(unittest.TestCase):
    """Phase 4: oversized tool output goes to the filesystem and rides in the
    prompt as head + tail + path. Truncating cut exactly the tail the model
    needed next; the old one-line receipt cut everything."""

    CHAT = "offload-tests"

    def tearDown(self):
        shutil.rmtree(agent.TOOLOUT_DIR / self.CHAT, ignore_errors=True)

    @staticmethod
    def _big(lines: int = 400) -> str:
        return "\n".join(f"line {i}: " + "x" * 60 for i in range(lines))

    def test_small_results_are_sent_verbatim(self):
        self.assertIsNone(agent.offload_tool_output("read_file", "tiny", self.CHAT, 1))
        just_under = "y" * agent.OFFLOAD_CHARS
        self.assertIsNone(agent.offload_tool_output("read_file", just_under, self.CHAT, 2))

    def test_large_plain_results_shrink_but_lose_nothing(self):
        body = self._big()
        payload = json.loads(agent.offload_tool_output("run_shell", body, self.CHAT, 3))
        ptr = payload["offloaded"]
        self.assertEqual(ptr["chars"], len(body))
        self.assertEqual(ptr["lines"], body.count("\n") + 1)
        # The prompt pays a fraction of the raw cost...
        self.assertLess(len(json.dumps(payload)), len(body) // 3)
        # ...and the complete text is on disk, byte for byte.
        self.assertEqual(Path(ptr["path"]).read_text(encoding="utf-8"), body)

    def test_a_structured_result_keeps_its_envelope(self):
        # Offloading the whole JSON string handed the model the head and tail of
        # truncated JSON, which is unreadable and easy to misparse.
        body = self._big()
        raw = json.dumps({"path": "big.py", "start": 1, "end": 400, "content": body})
        payload = json.loads(agent.offload_tool_output("read_file", raw, self.CHAT, 7))
        # Envelope intact and still machine-readable.
        self.assertEqual(payload["path"], "big.py")
        self.assertEqual(payload["start"], 1)
        self.assertEqual(payload["end"], 400)
        # Only the payload field was offloaded, and both ends of it survive.
        self.assertIn("line 0:", payload["content"])
        self.assertIn("line 399:", payload["content"])
        self.assertLess(len(payload["content"]), len(body))
        self.assertEqual(
            Path(payload["offloaded"]["content"]["path"]).read_text(encoding="utf-8"),
            body,
        )

    def test_every_oversized_field_is_offloaded_not_just_the_largest(self):
        # run_shell returns stdout AND stderr. Offloading only the larger one
        # left the other inline at up to the full capture cap.
        out, err = self._big(400), self._big(300)
        raw = json.dumps({"exit_code": 1, "stdout": out, "stderr": err})
        payload = json.loads(agent.offload_tool_output("run_shell", raw, self.CHAT, 8))
        self.assertEqual(set(payload["offloaded"]), {"stdout", "stderr"})
        self.assertEqual(payload["exit_code"], 1)
        for field, body in (("stdout", out), ("stderr", err)):
            ptr = payload["offloaded"][field]
            self.assertEqual(Path(ptr["path"]).read_text(encoding="utf-8"), body)
            self.assertLess(len(payload[field]), len(body))
        # Neither field is left inline at full size.
        self.assertLess(len(json.dumps(payload)), (len(out) + len(err)) // 3)

    def test_shell_output_keeps_its_head(self):
        # [-12000:] dropped the head of stdout, which for a build log is where
        # the first error is, and the command has already run, so unlike a
        # truncated read that loss is permanent.
        out = self._big(400)
        raw = json.dumps({"exit_code": 1, "stdout": out, "stderr": ""})
        payload = json.loads(agent.offload_tool_output("run_shell", raw, self.CHAT, 9))
        self.assertIn("line 0:", payload["stdout"])
        self.assertIn("line 399:", payload["stdout"])

    def test_both_ends_survive(self):
        # Truncation kept the head and dropped the tail, which is the half the
        # model usually needs next.
        body = self._big()
        payload = json.loads(agent.offload_tool_output("run_shell", body, self.CHAT, 4))
        self.assertTrue(body.startswith(payload["head"]))
        self.assertTrue(body.endswith(payload["tail"]))
        self.assertIn("line 0:", payload["head"])
        self.assertIn("line 399:", payload["tail"])

    def test_the_agent_can_follow_the_pointer(self):
        # A path the model cannot read is worse than a truncated result, so the
        # offload directory has to be inside the allowed roots.
        body = self._big()
        payload = json.loads(agent.offload_tool_output("run_shell", body, self.CHAT, 5))
        result = json.loads(agent.run_tool(
            "read_file",
            {"path": payload["offloaded"]["path"], "start_line": 398, "end_line": 400},
            None,
        ))
        self.assertNotIn("error", result)
        self.assertIn("line 398:", result["content"])

    def test_offload_files_are_pruned(self):
        for seq in range(agent.TOOLOUT_KEEP + 15):
            agent.offload_tool_output("run_shell", self._big(200), self.CHAT, seq)
        kept = list((agent.TOOLOUT_DIR / self.CHAT).iterdir())
        self.assertLessEqual(len(kept), agent.TOOLOUT_KEEP)
        # Pruning drops the oldest, so the newest sequence must survive.
        self.assertTrue(any(f"{agent.TOOLOUT_KEEP + 14:04d}" in k.name for k in kept))

    def test_a_write_failure_falls_back_instead_of_breaking_the_call(self):
        with unittest.mock.patch.object(
            agent.Path, "write_text", side_effect=OSError("disk full")
        ):
            self.assertIsNone(
                agent.offload_tool_output("run_shell", self._big(), self.CHAT, 6)
            )


class NarrationVersusDeliverableTests(unittest.TestCase):
    """A written-out plan is a failed action, not a finished turn.

    looks_like_final_answer treats anything over 400 characters as the
    deliverable. A plan defeats that by being long and well formed while its
    closing line is still only a promise, so the agent loop ended runs having
    called no tools at all and handed the user a plan as if it were the work.
    """

    PLAN = (
        "Here is my plan for the jungle platformer:\n\n"
        "1. Create index.html with a canvas element and the game shell.\n"
        "2. Add a physics loop with gravity, jumping and horizontal acceleration.\n"
        "3. Draw the tile map and the parallax jungle background layers.\n"
        "4. Add collision detection against the tile grid.\n"
        "5. Add collectibles, enemies and a HUD showing score and lives.\n"
        "6. Open it in a browser and iterate on the feel of the jump arc.\n\n"
        "Let me start by creating index.html."
    )

    DELIVERABLE = (
        "Done. I created ~/games/platformer/index.html with the whole game in one "
        "file: gravity and a double jump, five hand-authored tile levels, three "
        "enemy types with simple patrol AI, collectible fruit and a score HUD. "
        "The level data is a plain array at the top of the file, so adding levels "
        "does not mean touching the engine.\n\n"
        "Serve it with `python3 -m http.server 8000` and open "
        "http://localhost:8000/index.html. Let me know if you want the jump arc "
        "tuned: GRAVITY and JUMP_VELOCITY are the two constants that control it."
    )

    def test_a_long_plan_ending_on_a_promise_is_not_a_final_answer(self):
        self.assertGreater(len(self.PLAN), 400)
        self.assertTrue(agent.looks_like_final_answer(self.PLAN))
        self.assertTrue(agent.looks_incomplete(self.PLAN))

    def test_a_long_report_of_finished_work_still_ends_the_turn(self):
        self.assertGreater(len(self.DELIVERABLE), 400)
        self.assertFalse(agent.looks_incomplete(self.DELIVERABLE))

    def test_only_the_closing_lines_count_as_a_promise(self):
        # "I'll create" early in a report that then shows the created file is a
        # narration of work already done, not a promise of work still to come.
        report = (
            "I said I'll create the loader first, so that is where I started. "
            + "The loader now reads the manifest and validates every entry. " * 8
            + "\n\nAll 41 tests pass."
        )
        self.assertGreater(len(report), 400)
        self.assertFalse(agent.ends_with_promise(report))
        self.assertFalse(agent.looks_incomplete(report))

    def test_short_promises_are_still_caught(self):
        for text in ("I'll begin by reading the existing file.", "Writing it now.", "Let me start."):
            with self.subTest(text=text):
                self.assertTrue(agent.looks_incomplete(text))


class PlanRewriteTests(unittest.TestCase):
    """A merge=false rewrite must not resurrect finished work.

    Qwen re-emits the whole plan mid-run to expand the step it is about to start,
    and regenerates every item as "pending" because it does not track its own
    statuses. Replacing the list wholesale reset a run that was four milestones
    deep back to nothing done.
    """

    PLAN = [
        {"id": "scaffold", "content": "Create index.html shell", "status": "completed"},
        {"id": "styles", "content": "Write style.css", "status": "completed"},
        {"id": "script", "content": "Write script.js foundation", "status": "in_progress"},
        {"id": "levels", "content": "Add level data", "status": "pending"},
    ]

    def _write(self, chat, merge, todos):
        return json.loads(agent.run_tool(
            "todo_write", {"merge": merge, "todos": todos}, None, chat=chat
        ))

    def test_a_forgetful_rewrite_keeps_the_finished_milestones(self):
        chat = {"agent_todos": [dict(t) for t in self.PLAN]}
        result = self._write(chat, False, [
            {"id": "scaffold", "content": "Create index.html shell", "status": "pending"},
            {"id": "styles", "content": "Write style.css", "status": "pending"},
            {"id": "script", "content": "Write script.js foundation", "status": "pending"},
            {"id": "input", "content": "Add keyboard input handling", "status": "pending"},
            {"id": "levels", "content": "Add level data", "status": "pending"},
        ])
        by_id = {t["id"]: t["status"] for t in chat["agent_todos"]}
        self.assertEqual(by_id["scaffold"], "completed")
        self.assertEqual(by_id["styles"], "completed")
        self.assertEqual(sorted(result["restored"]), ["scaffold", "styles"])
        self.assertEqual(result["done"], 2)
        # The step that was underway is the one picked back up, not the first item.
        self.assertEqual(by_id["script"], "in_progress")
        self.assertEqual(by_id["input"], "pending")

    def test_a_rewrite_that_renames_ids_is_matched_on_content(self):
        chat = {"agent_todos": [dict(t) for t in self.PLAN]}
        self._write(chat, False, [
            {"id": "step-1", "content": "Create index.html shell", "status": "pending"},
            {"id": "step-2", "content": "Write style.css", "status": "pending"},
            {"id": "step-3", "content": "Write script.js foundation", "status": "pending"},
        ])
        by_content = {t["content"]: t["status"] for t in chat["agent_todos"]}
        self.assertEqual(by_content["Create index.html shell"], "completed")
        self.assertEqual(by_content["Write style.css"], "completed")

    def test_a_deliberate_reopen_is_left_alone(self):
        # The rewrite still marks one earlier milestone completed, so it knows the
        # state and is reopening "styles" on purpose. Nothing is restored.
        chat = {"agent_todos": [dict(t) for t in self.PLAN]}
        result = self._write(chat, False, [
            {"id": "scaffold", "content": "Create index.html shell", "status": "completed"},
            {"id": "styles", "content": "Write style.css", "status": "pending"},
            {"id": "script", "content": "Write script.js foundation", "status": "pending"},
        ])
        by_id = {t["id"]: t["status"] for t in chat["agent_todos"]}
        self.assertEqual(result["restored"], [])
        self.assertEqual(by_id["scaffold"], "completed")
        # Reopened, and auto-promoted because the payload named no active item.
        self.assertEqual(by_id["styles"], "in_progress")

    def test_a_first_plan_has_nothing_to_restore(self):
        chat = {}
        result = self._write(chat, False, [
            {"id": "a", "content": "Read the source", "status": "pending"},
            {"id": "b", "content": "Patch the bug", "status": "pending"},
        ])
        self.assertEqual(result["restored"], [])
        self.assertEqual(result["done"], 0)
        self.assertEqual(chat["agent_todos"][0]["status"], "in_progress")

    def test_merge_updates_are_untouched_by_the_repair(self):
        chat = {"agent_todos": [dict(t) for t in self.PLAN]}
        result = self._write(chat, True, [{"id": "script", "status": "completed"}])
        by_id = {t["id"]: t["status"] for t in chat["agent_todos"]}
        self.assertEqual(result["restored"], [])
        self.assertEqual(by_id["script"], "completed")
        self.assertEqual(by_id["levels"], "in_progress")


class ContentToolCallTests(unittest.TestCase):
    """Weak models write tool calls into the reply instead of the tool_calls field.

    Measured on llama3.2:3b with Aether's real schemas: 2 of 16 replies did it,
    both on the deeply nested ones (todo_write, ask_user). Without a fallback
    the loop reads them as a final answer and the run ends having done nothing.
    """

    def _names(self, content, native=None):
        return [c["name"] for c in
                agent.extract_tool_calls({"content": content, "tool_calls": native or []})]

    def test_the_shape_llama_actually_emitted(self):
        observed = ('{"name":"todo_write","parameters":{"merge":true,"todos":'
                    '[{"content":"Build","status":"pending","id":"1","activeForm":"Building"}]}}')

        self.assertEqual(self._names(observed), ["todo_write"])

    def test_arguments_and_parameters_are_both_accepted(self):
        for field in ("arguments", "parameters", "args"):
            content = '{"name":"list_dir","%s":{"path":"."}}' % field
            self.assertEqual(self._names(content), ["list_dir"], field)

    def test_a_fenced_json_block_is_read(self):
        self.assertEqual(
            self._names('```json\n{"name":"git_status","arguments":{}}\n```'), ["git_status"])

    def test_the_mistral_marker_is_read(self):
        self.assertEqual(
            self._names('[TOOL_CALLS] [{"name":"list_dir","arguments":{"path":"."}}]'), ["list_dir"])

    def test_an_array_yields_every_call(self):
        content = '[{"name":"list_dir","arguments":{}},{"name":"git_status","arguments":{}}]'

        self.assertEqual(self._names(content), ["list_dir", "git_status"])

    def test_arguments_given_as_a_json_string_are_decoded(self):
        self.assertEqual(
            self._names('{"name":"list_dir","arguments":"{\\"path\\": \\".\\"}"}'), ["list_dir"])

    # The safety half. A false positive runs a tool the user never asked for.
    def test_a_call_quoted_inside_prose_is_never_executed(self):
        content = 'You could call {"name":"write_file","arguments":{"path":"x"}} to do that.'

        self.assertEqual(self._names(content), [])

    def test_an_unregistered_name_is_ignored(self):
        self.assertEqual(self._names('{"name":"rm_rf","arguments":{"path":"/"}}'), [])

    def test_ordinary_json_data_is_not_a_tool_call(self):
        self.assertEqual(self._names('{"path":"a.py","size":3}'), [])
        self.assertEqual(self._names('{"name":"a.py","size":3}'), [])

    def test_prose_is_left_alone(self):
        self.assertEqual(self._names("I finished the work and the tests pass."), [])

    def test_non_dict_arguments_are_refused(self):
        self.assertEqual(self._names('{"name":"list_dir","arguments":[1,2]}'), [])

    def test_the_fallback_stands_down_when_the_model_called_natively(self):
        native = [{"function": {"name": "list_dir", "arguments": {"path": "."}}}]
        content = '{"name":"todo_write","parameters":{"merge":true,"todos":[]}}'

        # Native wins; the content copy must not run as a second call.
        self.assertEqual(self._names(content, native), ["list_dir"])

    def test_a_marker_is_trusted_even_with_prose_around_it(self):
        content = 'Sure, doing that now.\n<tool_call>{"name":"git_status","arguments":{}}</tool_call>'

        self.assertEqual(self._names(content), ["git_status"])


class SchemaTrimTests(unittest.TestCase):
    """All 14 schemas cost ~4.1k tokens, which a small window cannot spare."""

    def _names(self, limit):
        return {t["function"]["name"] for t in agent.tool_defs_for({}, limit)}

    def test_a_large_window_keeps_every_tool(self):
        self.assertEqual(self._names(131072), self._names(None))

    def test_the_default_agentic_window_is_not_trimmed(self):
        # 65536 is DEFAULT_SETTINGS' agentic window; trimming it would be a
        # regression for the model Aether was built on.
        self.assertEqual(self._names(65536), self._names(None))

    def test_a_small_window_drops_the_optional_tools(self):
        kept = self._names(16384)

        self.assertNotIn("ask_user", kept)
        self.assertNotIn("remember", kept)
        self.assertLess(agent.schema_tokens(agent.tool_defs_for({}, 16384)),
                        agent.schema_tokens(agent.tool_defs_for({}, None)))

    def test_the_tools_agentic_work_needs_are_never_dropped(self):
        for limit in (16384, 8192, 2048):
            kept = self._names(limit)
            for essential in ("list_dir", "read_file", "write_file", "edit_file",
                              "run_shell", "search_files", "todo_write"):
                self.assertIn(essential, kept, f"{essential} at {limit}")

    def test_trimming_is_ordered_not_arbitrary(self):
        big, small = self._names(20000), self._names(16384)

        self.assertTrue(small <= big)

    def test_the_prompt_never_names_a_tool_that_was_trimmed(self):
        defs = agent.tool_defs_for({}, 16384)
        addon = agent.tools_system_addon(defs)
        listed = addon.splitlines()[3].replace("Available tools: ", "").rstrip(".").split(", ")

        self.assertEqual(set(listed), {t["function"]["name"] for t in defs})
        self.assertNotIn("ask_user is the ONLY", addon)


class BrokenToolCallTests(unittest.TestCase):
    """A tool call written into the reply AND malformed.

    Observed live on llama3.2:3b asked to plan with todo_write: it emitted
    `"todos":[["activeForm":...]]`, square brackets where objects belonged.
    The JSON never parses, so the arguments cannot be recovered. Executing a
    guess would run something the model did not ask for, so the loop retries
    instead.
    """

    def test_the_malformed_call_observed_live_is_recognised(self):
        observed = ('{"name":"todo_write","parameters":{"merge":true,"todos":'
                    '[["activeForm":"Update changelog","content":"Add changelog","status":"pending"]]}}')

        self.assertEqual(agent.looks_like_broken_tool_call(observed), "todo_write")

    def test_a_call_cut_off_mid_write_is_recognised(self):
        truncated = '{"name":"write_file","arguments":{"path":"a.py","conte'

        self.assertEqual(agent.looks_like_broken_tool_call(truncated), "write_file")

    def test_a_call_that_parses_is_not_reported_as_broken(self):
        # Those already ran as real calls; reporting them would retry forever.
        good = '{"name":"list_dir","arguments":{"path":"."}}'

        self.assertIsNone(agent.looks_like_broken_tool_call(good))

    def test_prose_is_not_a_broken_call(self):
        self.assertIsNone(agent.looks_like_broken_tool_call("I finished, everything passes."))
        self.assertIsNone(agent.looks_like_broken_tool_call(""))

    def test_json_that_names_no_tool_is_not_a_broken_call(self):
        self.assertIsNone(agent.looks_like_broken_tool_call('{"name":"whatever","x":1}'))
        self.assertIsNone(agent.looks_like_broken_tool_call('{"path":"a","size":3}'))

    def test_no_arguments_are_ever_invented_from_broken_json(self):
        broken = '{"name":"run_shell","arguments":{"command":"rm -rf /' 
        # It reports only the name. Nothing here can turn into an executed call.
        self.assertEqual(agent.looks_like_broken_tool_call(broken), "run_shell")
        self.assertEqual(agent.extract_tool_calls({"content": broken, "tool_calls": []}), [])


class TrailingCallTests(unittest.TestCase):
    """Models often write a sentence and then the call.

    Observed live: 'Here is the updated plan:\\n{"name": "todo_write", ...}'.
    Requiring the JSON to reach the end of the reply is what separates that
    from a call quoted mid-sentence, which is followed by more prose.
    """

    def _names(self, content):
        return [c["name"] for c in agent.extract_tool_calls({"content": content, "tool_calls": []})]

    def test_a_call_after_a_sentence_is_recovered(self):
        content = 'Here is the updated plan:\n\n{"name":"todo_write","parameters":{"merge":true,"todos":[]}}'

        self.assertEqual(self._names(content), ["todo_write"])

    def test_a_call_with_prose_after_it_is_still_refused(self):
        content = 'You could call {"name":"write_file","arguments":{"path":"x"}} to do that.'

        self.assertEqual(self._names(content), [])

    def test_trailing_data_that_names_no_tool_is_refused(self):
        self.assertEqual(self._names('The result was: {"path":"a","size":3}'), [])

    def test_a_truncated_call_is_reported_broken_not_executed(self):
        cut = '{"name":"write_file","arguments":{"path":"a.py","conte'

        self.assertEqual(self._names(cut), [])
        self.assertEqual(agent.looks_like_broken_tool_call(cut), "write_file")

    def test_a_mangled_call_after_prose_is_reported_broken(self):
        content = 'Here is the plan:\n{"name":"todo_write","parameters":{"todos":[["a":1]]}}'

        self.assertEqual(agent.looks_like_broken_tool_call(content), "todo_write")

    def test_a_mangled_call_quoted_mid_sentence_is_left_alone(self):
        content = 'Maybe {"name":"todo_write","x":1} could work, but I already did it.'

        self.assertIsNone(agent.looks_like_broken_tool_call(content))
