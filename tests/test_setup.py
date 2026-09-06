# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import setup

ROOT = Path(__file__).resolve().parents[1]


class ModelDetectionTests(unittest.TestCase):
    def test_normalizes_official_registry_and_implicit_latest_names(self):
        self.assertEqual(
            setup.normalize_model_tag("registry.ollama.ai/library/QWEN3.8:27B"),
            "qwen3.8:27b",
        )
        self.assertEqual(setup.normalize_model_tag("qwen3.8"), "qwen3.8:latest")

    def test_model_match_is_exact_not_a_prefix_collision(self):
        installed = {"qwen3.8:27b-custom", "qwen3-coder:30b"}
        self.assertFalse(setup.model_is_installed(installed, "qwen3.8:27b"))
        self.assertTrue(setup.model_is_installed(installed, "qwen3-coder:30b"))

    @patch.object(setup.urllib.request, "urlopen")
    def test_api_model_inventory_is_preferred(self, urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps({
            "models": [
                {"name": "qwen3.8:27b"},
                {"model": "qwen3-coder:30b"},
            ]
        }).encode("utf-8")
        urlopen.return_value.__enter__.return_value = response

        self.assertEqual(
            setup._installed_models_from_api(),
            {"qwen3.8:27b", "qwen3-coder:30b"},
        )

    @patch.object(setup.subprocess, "call")
    @patch.object(setup, "installed_models")
    @patch.object(setup, "log")
    def test_pull_reuses_existing_models_and_downloads_only_missing_one(
        self, _log, installed_models, call
    ):
        installed_models.side_effect = [
            {"qwen2.5:0.5b", "qwen3.8:27b"},
            {"qwen2.5:0.5b", "qwen3.8:27b", "qwen3-coder:30b"},
        ]
        call.return_value = 0

        setup.pull_models(
            "ollama.exe",
            ["qwen2.5:0.5b", "qwen3.8:27b", "qwen3-coder:30b"],
        )

        call.assert_called_once_with(["ollama.exe", "pull", "qwen3-coder:30b"])

    @patch.object(setup.subprocess, "call", return_value=0)
    @patch.object(setup, "installed_models", side_effect=[set(), set()])
    def test_successful_pull_exit_is_rejected_when_model_is_still_invisible(
        self, _installed_models, _call
    ):
        with (
            patch.object(setup, "log"),
            patch.object(setup.sys, "stderr", io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            setup.pull_models("ollama.exe", ["qwen3.8:27b"])


if __name__ == "__main__":
    unittest.main()


GIB = 1024 ** 3


def _hw(vram_gb, ram_gb, disk_gb=500, gpu="Test GPU"):
    import models as reg
    usable = int(vram_gb * GIB) or int(ram_gb * GIB)
    return {
        "gpu": gpu if vram_gb else "none detected",
        "vram_bytes": int(vram_gb * GIB),
        "ram_bytes": int(ram_gb * GIB),
        "free_disk_bytes": int(disk_gb * GIB),
        "model_store": "/models",
        "cpu_only": not vram_gb,
        "usable_bytes": usable,
        "recommend_bytes": usable if vram_gb else min(int(ram_gb * GIB), reg.CPU_RECOMMEND_CAP),
    }


class WizardTests(unittest.TestCase):
    """Default, Recommended for this machine, or a tag you type."""

    def _choose(self, hw, installed=frozenset(), size=5 * GIB):
        import models as reg
        buf = io.StringIO()
        with patch.object(reg, "hardware", return_value=hw), \
             patch.object(reg, "manifest_size", side_effect=lambda t, **k: size), \
             contextlib.redirect_stdout(buf):
            tags = setup.choose_models(set(installed), non_interactive=True)
        return tags, buf.getvalue()

    def test_a_small_card_is_not_offered_the_tuned_pair(self):
        tags, _ = self._choose(_hw(6, 16))

        self.assertNotIn("qwen3.8:27b", tags)
        self.assertNotIn("qwen3-coder:30b", tags)

    def test_the_recommendation_grows_with_the_card(self):
        import models as reg
        sizes = []
        for vram in (4, 8, 16, 24):
            tier = reg.recommend(vram)
            sizes.append(tier["id"])

        self.assertEqual(sizes, ["tiny", "small", "medium", "default"])

    def test_a_24gb_card_gets_the_pair_aether_was_tuned_on(self):
        tags, _ = self._choose(_hw(24, 32))

        self.assertIn("qwen3.8:27b", tags)
        self.assertIn("qwen3-coder:30b", tags)

    def test_a_much_bigger_card_is_offered_more_than_the_default(self):
        tags, out = self._choose(_hw(80, 128))

        self.assertIn("llama3.3:70b", tags)
        self.assertIn("choosing 2", out)

    def test_a_machine_with_no_gpu_is_sized_for_speed_not_for_what_fits(self):
        # 128 GB of RAM would hold a 70B; it would answer at about a word a
        # second, so the recommendation is capped into the 8B tier.
        tags, out = self._choose(_hw(0, 128))

        self.assertNotIn("qwen3.8:27b", tags)
        self.assertNotIn("llama3.3:70b", tags)
        self.assertIn("CPU only", out)

    def test_the_title_model_is_always_included(self):
        import config
        for hw in (_hw(4, 8), _hw(24, 32), _hw(80, 128)):
            tags, _ = self._choose(hw)
            self.assertIn(config.TITLE_MODEL_ID, tags, str(hw["vram_bytes"]))

    def test_a_choice_too_big_for_the_disk_is_refused(self):
        with self.assertRaises(SystemExit):
            self._choose(_hw(24, 32, disk_gb=5), size=20 * GIB)

    def test_models_already_present_are_not_counted_as_downloads(self):
        tags, out = self._choose(_hw(24, 32), installed={"qwen3.8:27b", "qwen3-coder:30b",
                                                         "qwen2.5:0.5b"})
        self.assertIn("already installed", out)

    def test_the_library_is_pointed_at_for_typing_your_own(self):
        _, out = self._choose(_hw(24, 32))

        self.assertIn("ollama.com/library", out)
        self.assertIn("Type your own", out)


class CustomTagTests(unittest.TestCase):
    """A typed tag is checked against Ollama's registry before anything downloads."""

    def _run(self, answers, sizes):
        import models as reg
        buf = io.StringIO()
        with patch("builtins.input", side_effect=list(answers)), \
             patch.object(reg, "manifest_size", side_effect=lambda t, **k: sizes.get(t, 0)), \
             contextlib.redirect_stdout(buf):
            tags, total = setup.choose_custom(reg.load_catalog(), set())
        return tags, total, buf.getvalue()

    def test_a_real_tag_is_accepted_with_its_size(self):
        tags, total, out = self._run(["qwen3:8b", ""], {"qwen3:8b": 5 * GIB})

        self.assertIn("qwen3:8b", tags)
        self.assertEqual(total, 5 * GIB)
        self.assertIn("5.0 GB", out)

    def test_a_tag_ollama_does_not_have_is_refused_and_re_asked(self):
        tags, _, out = self._run(["nope", "qwen3:8b", ""], {"qwen3:8b": 5 * GIB})

        self.assertNotIn("nope", tags)
        self.assertIn("qwen3:8b", tags)
        self.assertIn("has no model called nope", out)

    def test_it_will_not_finish_with_nothing_chosen(self):
        tags, _, out = self._run(["", "qwen3:8b", ""], {"qwen3:8b": 5 * GIB})

        self.assertIn("At least one model is needed", out)
        self.assertIn("qwen3:8b", tags)

    def test_the_title_model_comes_along(self):
        import config
        tags, _, _ = self._run(["qwen3:8b", ""], {"qwen3:8b": 5 * GIB})

        self.assertEqual(tags[0], config.TITLE_MODEL_ID)


class ManifestConsistencyTests(unittest.TestCase):
    """aether.json is documentation, so nothing catches it drifting on its own."""

    def setUp(self):
        self.manifest = json.loads((ROOT / "aether.json").read_text(encoding="utf-8"))

    def test_the_stated_defaults_are_the_real_ones(self):
        import config
        defaults = self.manifest["models"]["defaults"]

        self.assertEqual(defaults["chat"], config.DEFAULT_CHAT_MODEL)
        self.assertEqual(defaults["agentic"], config.DEFAULT_CODER_MODEL)
        self.assertEqual(defaults["vision"], config.DEFAULT_AGENT_MODEL)
        self.assertEqual(defaults["titles"], config.TITLE_MODEL_ID)

    def test_the_stated_windows_are_the_real_ones(self):
        import config

        self.assertEqual(self.manifest["context"]["role_defaults"], config.ROLE_CONTEXT)
        self.assertEqual(self.manifest["context"]["reasoning_ceiling"],
                         config.ROLE_CONTEXT_MAX["reasoning"])

    def test_the_catalog_it_names_exists(self):
        self.assertTrue((ROOT / self.manifest["models"]["catalog"]).is_file())

    def test_the_version_matches_the_code(self):
        import config

        self.assertEqual(self.manifest["version"], config.VERSION)
