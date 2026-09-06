# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""The updater's contract: compare honestly, replace program files only, and
never let a failure leave a half-written install behind."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import update

ROOT = Path(__file__).resolve().parents[1]


class VersionTests(unittest.TestCase):
    def test_tag_spellings_all_parse_to_the_same_release(self):
        for text in ("v1.3.0", "1.3.0", "Aether v1.3.0"):
            self.assertEqual(update.parse_version(text)[0], (1, 3, 0), text)

    def test_ordering_is_numeric_not_lexical(self):
        # "1.10.0" < "1.9.0" as strings, and the wrong way round would tell a
        # user on 1.9.0 that they are already current.
        self.assertEqual(update.compare_versions("1.9.0", "1.10.0"), -1)
        self.assertEqual(update.compare_versions("1.3.0", "1.3.0"), 0)
        self.assertEqual(update.compare_versions("2.0.0", "1.9.9"), 1)

    def test_short_and_long_versions_compare_by_value(self):
        self.assertEqual(update.compare_versions("1.3", "1.3.0"), 0)
        self.assertEqual(update.compare_versions("1.3", "1.3.1"), -1)

    def test_a_prerelease_sorts_below_the_release_it_leads_to(self):
        self.assertEqual(update.compare_versions("1.4.0-rc1", "1.4.0"), -1)
        self.assertEqual(update.compare_versions("1.4.0-rc1", "1.3.0"), 1)

    def test_unparseable_names_do_not_claim_to_be_newer(self):
        self.assertEqual(update.compare_versions("nightly", "1.3.0"), -1)


class ReadVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_config_is_the_source_of_truth(self):
        (self.tmp / "config.py").write_text('VERSION = "9.9.9"\n', encoding="utf-8")
        (self.tmp / "aether.json").write_text('{"version": "0.0.1"}', encoding="utf-8")
        self.assertEqual(update.read_version(self.tmp), "9.9.9")

    def test_aether_json_is_the_fallback(self):
        (self.tmp / "aether.json").write_text('{"version": "1.2.3"}', encoding="utf-8")
        self.assertEqual(update.read_version(self.tmp), "1.2.3")

    def test_config_and_aether_json_agree_on_the_version(self):
        # They must be bumped together: config.py decides whether an installed
        # copy is current, and a stale aether.json would report a different
        # version to anything reading it.
        declared = json.loads((ROOT / "aether.json").read_text(encoding="utf-8"))
        self.assertEqual(update.read_version(ROOT), declared["version"])

    def test_the_shipped_tree_reports_a_real_version(self):
        import config

        self.assertEqual(update.read_version(ROOT), config.VERSION)
        self.assertRegex(update.read_version(ROOT), r"^\d+\.\d+")


class ReleaseTests(unittest.TestCase):
    def test_a_published_zip_asset_is_preferred_over_the_zipball(self):
        release = {
            "tag_name": "v1.4.0",
            "assets": [{"name": "aether-v1.4.0.zip",
                        "browser_download_url": "https://example/aether-v1.4.0.zip"}],
            "zipball_url": "https://example/zipball",
        }
        self.assertEqual(update.pick_download(release),
                         ("https://example/aether-v1.4.0.zip", "aether-v1.4.0.zip"))

    def test_the_zipball_is_used_when_a_release_has_no_assets(self):
        url, name = update.pick_download({"tag_name": "v1.4.0", "assets": [],
                                          "zipball_url": "https://example/zipball"})
        self.assertEqual(url, "https://example/zipball")
        self.assertEqual(name, "aether-v1.4.0.zip")

    @patch.object(update, "fetch_json")
    def test_a_repo_with_no_releases_stops_instead_of_installing_nothing(self, fetch):
        fetch.side_effect = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        with self.assertRaises(SystemExit), patch("sys.stderr", io.StringIO()):
            update.latest_release()

    @patch.object(update, "fetch_json")
    def test_prereleases_are_only_considered_when_asked_for(self, fetch):
        fetch.return_value = [{"tag_name": "v1.5.0-rc1", "prerelease": True}]
        self.assertEqual(update.latest_release(True)["tag_name"], "v1.5.0-rc1")
        fetch.assert_called_once()
        self.assertIn("per_page", fetch.call_args[0][0])


class ProtectedPathTests(unittest.TestCase):
    def test_user_data_and_the_venv_are_never_installable(self):
        for name in ("data/chats/a.json", "data/settings/settings.json",
                     ".venv/bin/python", ".git/config", "aether.local.json",
                     "__pycache__/config.cpython-312.pyc"):
            self.assertTrue(update.is_protected(Path(name)), name)

    def test_program_files_are_installable(self):
        for name in ("server.py", "static/app.js", "scripts/install-windows.ps1"):
            self.assertFalse(update.is_protected(Path(name)), name)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "aether"
        self.source = self.tmp / "new"
        for path in (self.root, self.source):
            path.mkdir(parents=True)
        patcher = patch.multiple(
            update,
            ROOT=self.root,
            STATE=self.root / "data" / "updates",
            MANIFEST=self.root / "data" / "updates" / "installed.json",
            BACKUPS=self.root / "data" / "updates" / "backups",
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        quiet = patch.object(update, "log")
        quiet.start()
        self.addCleanup(quiet.stop)

    def write(self, base: Path, rel: str, text: str) -> None:
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def test_tree_files_lists_program_files_and_skips_user_data(self):
        self.write(self.source, "server.py", "new")
        self.write(self.source, "static/app.js", "new")
        self.write(self.source, "data/chats/private.json", "secret")
        self.write(self.source, ".venv/bin/python", "binary")
        self.assertEqual(
            sorted(p.as_posix() for p in update.tree_files(self.source)),
            ["server.py", "static/app.js"],
        )

    def test_installing_replaces_program_files_and_leaves_data_alone(self):
        self.write(self.root, "server.py", "old")
        self.write(self.root, "data/chats/private.json", "secret")
        self.write(self.source, "server.py", "new")
        self.write(self.source, "static/app.js", "added")

        created = update.install_files(self.source, update.tree_files(self.source))

        self.assertEqual((self.root / "server.py").read_text(), "new")
        self.assertEqual((self.root / "static/app.js").read_text(), "added")
        self.assertEqual((self.root / "data/chats/private.json").read_text(), "secret")
        self.assertEqual([p.as_posix() for p in created], ["static/app.js"])

    def test_nothing_is_deleted_without_a_manifest_from_a_previous_update(self):
        self.write(self.root, "hand_placed.py", "mine")
        self.assertEqual(update.stale_files([Path("server.py")]), [])

    def test_files_a_release_dropped_are_removed_on_the_next_update(self):
        self.write(self.root, "old_module.py", "gone in 1.4")
        self.write(self.root, "server.py", "old")
        update.save_manifest("1.3.0", "v1.3.0",
                             [Path("server.py"), Path("old_module.py")])

        stale = update.stale_files([Path("server.py")])
        self.assertEqual([p.as_posix() for p in stale], ["old_module.py"])

        update.remove_files(stale)
        self.assertFalse((self.root / "old_module.py").exists())
        self.assertTrue((self.root / "server.py").exists())

    def test_a_manifest_naming_user_data_can_never_delete_it(self):
        self.write(self.root, "data/chats/private.json", "secret")
        update.save_manifest("1.3.0", "v1.3.0", [Path("server.py")])
        update.MANIFEST.write_text(json.dumps(
            {"files": ["data/chats/private.json"]}), encoding="utf-8")

        self.assertEqual(update.stale_files([Path("server.py")]), [])
        self.assertTrue((self.root / "data/chats/private.json").exists())

    def test_a_failed_install_restores_the_previous_version(self):
        self.write(self.root, "server.py", "old")
        self.write(self.root, "config.py", "old config")
        self.write(self.source, "server.py", "new")
        self.write(self.source, "config.py", "new config")

        files = update.tree_files(self.source)
        backup = update.back_up(files, "1.3.0")
        created = update.install_files(self.source, files[:1])
        update.restore(backup, created)

        self.assertEqual((self.root / "server.py").read_text(), "old")
        self.assertEqual((self.root / "config.py").read_text(), "old config")

    def test_rollback_removes_files_the_new_release_had_added(self):
        self.write(self.root, "server.py", "old")
        self.write(self.source, "server.py", "new")
        self.write(self.source, "brand_new.py", "added")

        files = update.tree_files(self.source)
        backup = update.back_up(files, "1.3.0")
        created = update.install_files(self.source, files)
        update.restore(backup, created)

        self.assertEqual((self.root / "server.py").read_text(), "old")
        self.assertFalse((self.root / "brand_new.py").exists())

    def test_only_the_last_few_backups_are_kept(self):
        for stamp in range(6):
            (update.BACKUPS / f"1.3.0-2026010{stamp}-000000").mkdir(parents=True)
        update.prune_backups(keep=3)
        self.assertEqual(len(list(update.BACKUPS.iterdir())), 3)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _zip(self, entries: dict[str, str]) -> Path:
        import zipfile

        path = self.tmp / "release.zip"
        with zipfile.ZipFile(path, "w") as zf:
            for name, text in entries.items():
                zf.writestr(name, text)
        return path

    def test_a_release_archive_unwraps_to_its_single_top_level_folder(self):
        archive = self._zip({
            "aether-v1.4.0/config.py": 'VERSION = "1.4.0"\n',
            "aether-v1.4.0/server.py": "",
            "aether-v1.4.0/aether.json": "{}",
        })
        root = update.extract(archive, self.tmp / "out")
        self.assertEqual(root.name, "aether-v1.4.0")
        self.assertEqual(update.read_version(root), "1.4.0")

    def test_an_archive_that_is_not_aether_is_refused(self):
        archive = self._zip({"something-else/readme.txt": "hello"})
        with self.assertRaises(SystemExit), patch("sys.stderr", io.StringIO()):
            update.extract(archive, self.tmp / "out")

    def test_a_path_traversing_archive_is_refused_by_name(self):
        # Otherwise valid, so the refusal has to come from the path check
        # rather than from the "this is not Aether" check behind it.
        archive = self._zip({
            "aether-v1.4.0/config.py": 'VERSION = "1.4.0"\n',
            "aether-v1.4.0/server.py": "",
            "aether-v1.4.0/aether.json": "{}",
            "aether-v1.4.0/../../escaped.py": "malicious",
        })
        err = io.StringIO()
        with self.assertRaises(SystemExit), patch("sys.stderr", err):
            update.extract(archive, self.tmp / "out")
        self.assertIn("escaping path", err.getvalue())
        self.assertFalse((self.tmp / "escaped.py").exists())


class EntryPointTests(unittest.TestCase):
    """The two clickable wrappers have to reach update.py the same way the
    installers reach setup.py."""

    def test_windows_wrapper_runs_the_updater_and_waits_for_the_user(self):
        text = (ROOT / "Update-Aether.bat").read_text(encoding="utf-8")
        self.assertIn('cd /d "%~dp0"', text)
        self.assertIn("py -3 update.py %*", text)
        self.assertIn("python update.py %*", text)
        self.assertIn("pause", text)

    def test_windows_wrapper_reports_the_updaters_exit_code(self):
        text = (ROOT / "Update-Aether.bat").read_text(encoding="utf-8")
        # %ERRORLEVEL% has to be captured on its own line: read inside a
        # parenthesised block it expands before the updater has even run.
        self.assertIn('set "AETHER_UPDATE_EXIT=%ERRORLEVEL%"', text)
        self.assertIn("exit /b %AETHER_UPDATE_EXIT%", text)

    def test_linux_wrapper_prefers_the_distro_python_like_setup_does(self):
        text = (ROOT / "update.sh").read_text(encoding="utf-8")
        self.assertIn("/usr/bin/python3", text)
        self.assertIn('"$PY" update.py "$@"', text)
        self.assertIn('cd "$(dirname "$0")"', text)

    def test_linux_wrapper_is_executable(self):
        import os

        self.assertTrue(os.access(ROOT / "update.sh", os.X_OK))


class EndToEndTests(unittest.TestCase):
    """A whole update, start to finish, against a release on disk."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "installed"
        self.root.mkdir()

        # An Aether 1.3.0 that has been used: it has chats and settings.
        self._write(self.root / "config.py", 'VERSION = "1.3.0"\n')
        self._write(self.root / "aether.json", '{"version": "1.3.0"}')
        self._write(self.root / "server.py", "old server")
        self._write(self.root / "requirements.txt", "fastapi\n")
        self._write(self.root / "retired.py", "dropped in 1.4.0")
        self._write(self.root / "data" / "chats" / "c1.json", "my conversation")
        self._write(self.root / "data" / "settings" / "settings.json", "{}")
        self._write(self.root / ".venv" / "bin" / "python", "venv binary")

        patcher = patch.multiple(
            update,
            ROOT=self.root,
            VENV=self.root / ".venv",
            STATE=self.root / "data" / "updates",
            MANIFEST=self.root / "data" / "updates" / "installed.json",
            BACKUPS=self.root / "data" / "updates" / "backups",
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # 1.3.0 was itself installed by the updater, so a manifest exists.
        update.save_manifest("1.3.0", "v1.3.0", [
            Path("config.py"), Path("aether.json"), Path("server.py"),
            Path("requirements.txt"), Path("retired.py"),
        ])

    @staticmethod
    def _write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _release_zip(self, version: str) -> str:
        import zipfile

        path = self.tmp / f"aether-v{version}.zip"
        prefix = f"aether-v{version}/"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(prefix + "config.py", f'VERSION = "{version}"\n')
            zf.writestr(prefix + "aether.json", json.dumps({"version": version}))
            zf.writestr(prefix + "server.py", "new server")
            zf.writestr(prefix + "requirements.txt", "fastapi\n")
            zf.writestr(prefix + "brand_new.py", "arrived in " + version)
        return path.as_uri()

    def _run(self, argv, release, answers=None):
        # Nothing here is interactive, so a prompt is a bug, not a hang.
        replies = list(answers or [])

        def answer(prompt=""):
            if not replies:
                raise AssertionError(f"unexpected prompt: {prompt!r}")
            return replies.pop(0)

        with (
            patch.object(update, "fetch_json", return_value=release),
            patch.object(update.sys, "argv", ["update.py"] + argv),
            patch.object(update, "aether_is_running", return_value=False),
            patch.object(update, "refresh_dependencies") as deps,
            patch("builtins.input", answer),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            update.main()
        self.assertEqual(replies, [], "an expected prompt never appeared")
        return out.getvalue(), deps

    def test_an_update_replaces_the_program_and_keeps_the_users_data(self):
        release = {"tag_name": "v1.4.0", "assets": [
            {"name": "aether-v1.4.0.zip", "browser_download_url": self._release_zip("1.4.0")}]}

        text, deps = self._run(["--yes"], release)

        self.assertIn("Updated to 1.4.0", text)
        self.assertEqual((self.root / "config.py").read_text(), 'VERSION = "1.4.0"\n')
        self.assertEqual((self.root / "server.py").read_text(), "new server")
        self.assertEqual((self.root / "brand_new.py").read_text(), "arrived in 1.4.0")
        # Dropped by 1.4.0, and the manifest from 1.3.0 says the updater put it there.
        self.assertFalse((self.root / "retired.py").exists())
        # Untouched.
        self.assertEqual((self.root / "data/chats/c1.json").read_text(), "my conversation")
        self.assertEqual((self.root / ".venv/bin/python").read_text(), "venv binary")
        # requirements.txt did not move, so packages are left alone.
        deps.assert_not_called()
        self.assertEqual(update.load_manifest()["version"], "1.4.0")

    def test_being_on_the_latest_release_downloads_nothing(self):
        release = {"tag_name": "v1.3.0", "assets": [
            {"name": "aether-v1.3.0.zip", "browser_download_url": "https://unreachable/x.zip"}]}

        with patch.object(update, "download") as download:
            text, _ = self._run([], release)

        self.assertIn("You are on the latest release (1.3.0)", text)
        download.assert_not_called()
        self.assertEqual((self.root / "server.py").read_text(), "old server")

    def test_check_reports_an_update_without_installing_it(self):
        release = {"tag_name": "v1.4.0", "body": "Faster agent loop",
                   "assets": [{"name": "aether-v1.4.0.zip",
                               "browser_download_url": self._release_zip("1.4.0")}]}

        text, _ = self._run(["--check"], release)

        # The tag is v1.4.0; the line the user reads compares versions.
        self.assertIn("An update is available: 1.3.0 \u2192 1.4.0", text)
        self.assertIn("Faster agent loop", text)
        self.assertIn("nothing was changed", text)
        self.assertEqual((self.root / "server.py").read_text(), "old server")

    def test_declining_the_prompt_changes_nothing(self):
        release = {"tag_name": "v1.4.0", "assets": [
            {"name": "aether-v1.4.0.zip", "browser_download_url": self._release_zip("1.4.0")}]}

        with patch.object(update, "download") as download:
            text, _ = self._run([], release, answers=["n"])

        self.assertIn("Cancelled.", text)
        download.assert_not_called()
        self.assertEqual((self.root / "server.py").read_text(), "old server")

    def test_accepting_the_prompt_installs_the_release(self):
        release = {"tag_name": "v1.4.0", "assets": [
            {"name": "aether-v1.4.0.zip", "browser_download_url": self._release_zip("1.4.0")}]}

        text, _ = self._run([], release, answers=["y"])

        self.assertIn("Updated to 1.4.0", text)
        self.assertEqual((self.root / "server.py").read_text(), "new server")

    def test_a_release_older_than_this_copy_is_not_installed(self):
        release = {"tag_name": "v1.2.0", "assets": []}
        with patch.object(update, "download") as download:
            text, _ = self._run(["--yes"], release)
        self.assertIn("newer than the latest release", text)
        download.assert_not_called()

    def test_moved_requirements_trigger_a_package_refresh(self):
        import zipfile

        path = self.tmp / "aether-v1.4.0.zip"
        prefix = "aether-v1.4.0/"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(prefix + "config.py", 'VERSION = "1.4.0"\n')
            zf.writestr(prefix + "aether.json", '{"version": "1.4.0"}')
            zf.writestr(prefix + "server.py", "new server")
            zf.writestr(prefix + "requirements.txt", "fastapi\nhttpx\n")
        release = {"tag_name": "v1.4.0", "assets": [
            {"name": "aether-v1.4.0.zip", "browser_download_url": path.as_uri()}]}

        _, deps = self._run(["--yes"], release)

        deps.assert_called_once()


if __name__ == "__main__":
    unittest.main()
