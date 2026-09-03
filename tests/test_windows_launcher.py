# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WindowsLauncherContractTests(unittest.TestCase):
    def test_shortcut_launcher_uses_repo_venv_and_desktop_entrypoint(self):
        source = (ROOT / "Aether.vbs").read_text(encoding="utf-8")
        self.assertIn(r"\.venv\Scripts\pythonw.exe", source)
        self.assertIn(r"\desktop.py", source)
        self.assertIn("fso.FileExists(pyw)", source)
        self.assertIn("fso.FileExists(desk)", source)

    def test_shortcut_preserves_existing_ollama_host(self):
        source = (ROOT / "Aether.vbs").read_text(encoding="utf-8")
        self.assertIn(
            'If Len(env("OLLAMA_HOST")) = 0 Then env("OLLAMA_HOST") = '
            '"127.0.0.1:11434"',
            source,
        )

    def test_installer_shortcut_targets_silent_vbs_launcher(self):
        source = (ROOT / "scripts" / "install-windows.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn(r'System32\wscript.exe"', source)
        self.assertIn("Aether.vbs", source)
        self.assertIn("$shortcut.WorkingDirectory = $Root", source)


if __name__ == "__main__":
    unittest.main()
