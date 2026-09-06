# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import storage


class SettingsMigrationTests(unittest.TestCase):
    def test_retired_agent_step_limit_is_not_loaded_or_saved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings_path = root / "settings" / "settings.json"
            settings_path.parent.mkdir(parents=True)
            settings_path.write_text(
                json.dumps({"agent_max_steps": 40, "theme": "legacy"}),
                encoding="utf-8",
            )
            with (
                patch.object(storage, "SETTINGS_PATH", settings_path),
                patch.object(storage, "CHATS", root / "chats"),
                patch.object(storage, "MEMORY", root / "memory"),
                patch.object(storage, "PROJECTS", root / "projects"),
                patch.object(storage, "UPLOADS", root / "uploads"),
            ):
                loaded = storage.load_settings()
                self.assertNotIn("agent_max_steps", loaded)
                self.assertEqual(loaded["agent_repeat_limit"], 3)

                storage.save_settings({"theme": "updated"})
                persisted = json.loads(settings_path.read_text(encoding="utf-8"))
                self.assertNotIn("agent_max_steps", persisted)
                self.assertEqual(persisted["agent_repeat_limit"], 3)
                self.assertEqual(persisted["theme"], "updated")


if __name__ == "__main__":
    unittest.main()


class SettingsMigrationTests(unittest.TestCase):
    """The four named context settings became per-role in settings_version 3."""

    def test_the_default_windows_map_onto_roles(self):
        out, changed = storage._migrate_settings({
            "settings_version": 2, "chat_context": 32768,
            "reasoning_context": 32768, "agent_context": 65536, "coder_context": 65536,
        })

        self.assertTrue(changed)
        self.assertEqual(out["role_context"], {"chat": 32768, "reasoning": 32768, "agentic": 65536})
        self.assertEqual(out.get("model_context", {}), {})

    def test_a_coder_window_that_differed_is_preserved_exactly(self):
        # Two agentic models on different windows is the one case a per-role
        # setting cannot express, so it becomes a per-model override.
        out, _ = storage._migrate_settings({
            "settings_version": 2, "agent_context": 98304, "coder_context": 49152,
        })

        self.assertEqual(out["role_context"]["agentic"], 98304)
        self.assertEqual(out["model_context"][config.DEFAULT_CODER_MODEL], 49152)

    def test_a_hand_lowered_window_is_never_raised(self):
        out, _ = storage._migrate_settings({"settings_version": 2, "chat_context": 16384})

        self.assertEqual(out["role_context"]["chat"], 16384)

    def test_the_old_keys_do_not_survive_the_migration(self):
        out, _ = storage._migrate_settings({
            "settings_version": 2, "chat_context": 32768, "agent_context": 65536,
            "reasoning_context": 32768, "coder_context": 65536,
        })

        for legacy in ("chat_context", "reasoning_context", "agent_context", "coder_context"):
            self.assertNotIn(legacy, out)
        self.assertEqual(out["settings_version"], storage.SETTINGS_VERSION)

    def test_migrating_twice_changes_nothing_the_second_time(self):
        once, _ = storage._migrate_settings({"settings_version": 2, "agent_context": 65536})
        twice, changed = storage._migrate_settings(dict(once))

        self.assertFalse(changed)
        self.assertEqual(twice["role_context"], once["role_context"])
