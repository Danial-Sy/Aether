# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
import re
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


class WindowsInstallerWizardTests(unittest.TestCase):
    """The installer offers Default, Recommended, or a tag you type.

    PowerShell is parsed properly by CI on Windows. These are the structural
    checks that can run anywhere, so a broken edit is caught before the push.
    """

    def setUp(self):
        self.text = (ROOT / "scripts" / "install-windows.ps1").read_text(encoding="utf-8")

    def _functions(self):
        return set(re.findall(r"^function\s+([A-Za-z-]+)", self.text, re.M))

    def test_every_function_it_calls_is_defined(self):
        defined = self._functions()
        called = set(re.findall(r"[^-\w](Get-InstallPlan|Choose-Models|Show-Choice|"
                                r"Read-CustomTags|Has-Model|Stop-Setup|Write-Step)\b", self.text))
        self.assertTrue(called <= defined, called - defined)

    def test_the_two_model_profiles_are_gone(self):
        for stale in ("Choose-Profile", "modelProfile", "profileModels"):
            self.assertNotIn(stale, self.text, stale)

    def test_the_three_choices_are_offered(self):
        self.assertIn("Choose 1, 2 or 3", self.text)
        self.assertIn('Show-Choice 1 "Default"', self.text)
        self.assertIn('Show-Choice 2 "Recommended"', self.text)
        self.assertIn("Read-CustomTags", self.text)

    def test_no_model_tag_is_hardcoded_into_the_choices(self):
        # Sizes and tags come from the catalog, not from a table that goes stale.
        chooser = self.text[self.text.index("function Choose-Models"):]
        chooser = chooser[:chooser.index("function Set-OllamaEnvironment")]
        self.assertNotIn("qwen", chooser.lower())

    def test_disk_math_comes_from_the_registry(self):
        self.assertIn("models.manifest_size", self.text)
        # The old hardcoded GB table is gone.
        self.assertNotIn("$requiredDisk += 19", self.text)
        self.assertNotIn("$requiredDisk += 20", self.text)

    def test_a_typed_tag_is_verified_before_downloading(self):
        block = self.text[self.text.index("function Read-CustomTags"):]
        block = block[:block.index("function Choose-Models")]
        self.assertIn("manifest_size", block)
        self.assertIn("has no model called", block)

    def test_it_hands_the_chosen_tags_to_setup(self):
        self.assertIn('$setupArgs += @("--tags") + $chosen', self.text)

    def test_braces_and_parens_balance(self):
        stripped = re.sub(r"#.*", "", self.text)
        for opener, closer in (("{", "}"), ("(", ")"), ("[", "]")):
            self.assertEqual(stripped.count(opener), stripped.count(closer),
                             f"unbalanced {opener}{closer}")

    def test_the_oversize_warning_is_about_the_choice_not_one_model(self):
        # It used to name Qwen3.8 27B, which goes stale the moment the default
        # pair changes. It now fires when the user overrides the plan.
        block = self.text[self.text.index("$overrode ="):]
        block = block[:block.index("Write-Step")]
        self.assertNotIn("qwen", block.lower())
        self.assertIn("plan.recommended.tags", block)

    def test_it_falls_back_when_the_catalog_cannot_be_read(self):
        self.assertIn("Could not read the model catalog", self.text)
