# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
