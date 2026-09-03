# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
import json
import io
import unittest
from unittest.mock import MagicMock, patch

import setup


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
