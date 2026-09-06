# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Update Aether in place from the latest GitHub release.

Reads the version this copy is running, asks GitHub for the latest release,
and stops there if the two match. Otherwise it downloads that release, backs
up every file it is about to touch, replaces the program files, removes files
the new release dropped, and reinstalls Python packages if requirements moved.

Nothing under `data/` or `.venv/` is ever read as program code or overwritten,
so chats, settings, projects and the virtualenv survive an update.

Usage:
  python update.py            # check, ask, then update
  python update.py --check    # report only, change nothing
  python update.py --yes      # update without asking
  python update.py --force    # reinstall even if already on the latest
  python update.py --pre      # consider pre-releases too
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
STATE = ROOT / "data" / "updates"
MANIFEST = STATE / "installed.json"
BACKUPS = STATE / "backups"

REPO = (os.environ.get("AETHER_UPDATE_REPO") or "Danial-Sy/Aether").strip()
API = f"https://api.github.com/repos/{REPO}/releases"

# Never touched by an update: user data, the virtualenv, local overrides, and
# the git metadata of a development checkout.
PROTECTED = (
    "data",
    ".venv",
    "venv",
    ".git",
    "__pycache__",
    ".pytest_cache",
    "aether.local.json",
    ".env",
)

# A downloaded tree that is missing any of these is not Aether, and is refused
# before a single file is replaced.
REQUIRED = ("config.py", "server.py", "aether.json")


def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


# --------------------------------------------------------------------------
# versions


def parse_version(text: str) -> tuple[tuple[int, ...], int]:
    """Sort key for a release name: numbers first, then final-beats-prerelease.

    Accepts "v1.3.0", "1.3.0", "Aether v1.4.0" and "1.4.0-rc1". A prerelease
    suffix sorts below the same numbers without one, so 1.4.0-rc1 < 1.4.0.
    """
    raw = (text or "").strip()
    match = re.search(r"\d+(?:\.\d+)*", raw)
    if not match:
        return (), 0
    numbers = tuple(int(part) for part in match.group(0).split("."))
    rest = raw[match.end():]
    is_final = 0 if re.match(r"[-_.]?[A-Za-z]", rest) else 1
    return numbers, is_final


def compare_versions(left: str, right: str) -> int:
    """-1 if left is older than right, 0 if the same, 1 if newer."""
    (ln, lf), (rn, rf) = parse_version(left), parse_version(right)
    width = max(len(ln), len(rn))
    ln += (0,) * (width - len(ln))
    rn += (0,) * (width - len(rn))
    a, b = (ln, lf), (rn, rf)
    return (a > b) - (a < b)


def read_version(root: Path) -> str:
    """The version an Aether tree reports, without importing it.

    config.py is the source of truth the app itself uses; aether.json is the
    fallback so a release whose config moved still reports something sane.
    """
    config = root / "config.py"
    if config.exists():
        text = config.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"""^VERSION\s*=\s*["']([^"']+)["']""", text, re.M)
        if match:
            return match.group(1)
    meta = root / "aether.json"
    if meta.exists():
        try:
            value = json.loads(meta.read_text(encoding="utf-8")).get("version")
        except (ValueError, OSError):
            value = None
        if value:
            return str(value)
    return "0"


# --------------------------------------------------------------------------
# GitHub


def _request(url: str) -> urllib.request.Request:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"Aether-Updater/{read_version(ROOT)}",
    }
    token = (os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def fetch_json(url: str, timeout: int = 30):
    with urllib.request.urlopen(_request(url), timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def latest_release(include_prereleases: bool = False) -> dict:
    """The release to compare against, as GitHub describes it."""
    try:
        if not include_prereleases:
            return fetch_json(f"{API}/latest")
        for release in fetch_json(f"{API}?per_page=20"):
            if not release.get("draft"):
                return release
        die(f"{REPO} has no published releases yet.")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            die(f"{REPO} has no published releases yet.")
        if e.code in (403, 429):
            die("GitHub rate-limited this check. Wait a few minutes, or set "
                "GITHUB_TOKEN to raise the limit.")
        die(f"GitHub returned HTTP {e.code} for {REPO}.")
    except urllib.error.URLError as e:
        die(f"Could not reach GitHub: {e.reason}")
    except ValueError:
        die("GitHub returned something that is not a release.")
    raise AssertionError("unreachable")


def release_version(release: dict) -> str:
    return str(release.get("tag_name") or release.get("name") or "").strip()


def display_version(text: str) -> str:
    """Tags are written v1.4.0; the versions either side of an arrow are not."""
    return (text or "").strip().lstrip("vV") or "unknown"


def pick_download(release: dict) -> tuple[str, str]:
    """(url, filename) of the source archive to install."""
    for asset in release.get("assets") or []:
        name = str(asset.get("name") or "")
        url = asset.get("browser_download_url")
        if url and name.lower().endswith(".zip"):
            return url, name
    url = release.get("zipball_url")
    if not url:
        die("That release has no downloadable source archive.")
    return url, f"aether-{release_version(release) or 'latest'}.zip"


def download(url: str, dest: Path) -> Path:
    log(f"Downloading {dest.name}…")
    try:
        with urllib.request.urlopen(_request(url), timeout=120) as response:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            with dest.open("wb") as out:
                while True:
                    chunk = response.read(262144)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = done * 100 // total
                        print(f"\r    {pct:3d}%  {done / 1048576:.1f} MB",
                              end="", flush=True)
    except (urllib.error.URLError, OSError) as e:
        print()
        die(f"Download failed: {e}")
    if total:
        print()
    return dest


def extract(archive: Path, dest: Path) -> Path:
    """Unpack the archive and return the Aether tree inside it."""
    try:
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                target = (dest / member).resolve()
                if not str(target).startswith(str(dest.resolve())):
                    die(f"Refusing archive with an escaping path: {member}")
            zf.extractall(dest)
    except zipfile.BadZipFile:
        die("The downloaded file is not a valid ZIP archive.")

    root = dest
    entries = [p for p in root.iterdir() if p.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_dir():
        root = entries[0]
    missing = [name for name in REQUIRED if not (root / name).exists()]
    if missing:
        die(f"The downloaded release is missing {', '.join(missing)}; "
            "nothing was changed.")
    return root


# --------------------------------------------------------------------------
# applying


def is_protected(rel: Path) -> bool:
    return any(part in PROTECTED for part in rel.parts)


def tree_files(root: Path) -> list[Path]:
    """Every installable file in a tree, relative to it, protected ones dropped."""
    out = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if not is_protected(rel):
            out.append(rel)
    return out


def load_manifest() -> dict:
    if not MANIFEST.exists():
        return {}
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def save_manifest(version: str, tag: str, files: list[Path]) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps({
        "version": version,
        "tag": tag,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "files": sorted(p.as_posix() for p in files),
    }, indent=2), encoding="utf-8")


def stale_files(new_files: list[Path]) -> list[Path]:
    """Files the previous release installed that this one no longer ships.

    Only files this updater put there are ever removed. Without a manifest —
    the first update, or a copy installed by hand — nothing is deleted.
    """
    previous = load_manifest().get("files")
    if not isinstance(previous, list):
        return []
    keep = {p.as_posix() for p in new_files}
    out = []
    for name in previous:
        rel = Path(str(name))
        if rel.as_posix() in keep or is_protected(rel):
            continue
        if (ROOT / rel).is_file():
            out.append(rel)
    return out


def back_up(files: list[Path], version: str) -> Path:
    """Copy everything about to be replaced or removed, so a failure can undo."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = BACKUPS / f"{version}-{stamp}"
    saved = 0
    for rel in files:
        source = ROOT / rel
        if not source.is_file():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        saved += 1
    log(f"Backed up {saved} file(s) to {dest.relative_to(ROOT)}")
    return dest


def restore(backup: Path, created: list[Path]) -> None:
    for rel in created:
        target = ROOT / rel
        if target.is_file():
            target.unlink()
    if not backup.exists():
        return
    for path in backup.rglob("*"):
        if not path.is_file():
            continue
        target = ROOT / path.relative_to(backup)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def install_files(source_root: Path, files: list[Path]) -> list[Path]:
    """Copy the new release over the current one. Returns files newly created."""
    created = []
    for rel in files:
        target = ROOT / rel
        if not target.exists():
            created.append(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root / rel, target)
        # git archive does not carry the executable bit into a zip, so restore
        # it for the launchers that need to be clickable on Linux.
        if os.name != "nt" and rel.suffix in (".sh", ".desktop"):
            target.chmod(0o755)
    return created


def remove_files(files: list[Path]) -> None:
    for rel in files:
        target = ROOT / rel
        try:
            target.unlink()
        except OSError:
            continue
        parent = target.parent
        while parent != ROOT and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent


def venv_python() -> Path:
    if sys.platform == "win32":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def refresh_dependencies() -> None:
    py = venv_python()
    if not py.exists():
        log("No virtualenv here yet — run setup before launching.")
        return
    log("Requirements changed; updating Python packages…")
    try:
        subprocess.check_call(
            [str(py), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")],
            cwd=str(ROOT),
        )
    except (subprocess.CalledProcessError, OSError) as e:
        log(f"Package update failed ({e}). Run setup to finish: "
            f"{'setup.bat' if sys.platform == 'win32' else './setup.sh'} --skip-models")


def aether_is_running() -> bool:
    import socket

    host, _, port = (os.environ.get("AETHER_URL") or "127.0.0.1:7878").rpartition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(port or 7878)), 0.4):
            return True
    except (OSError, ValueError):
        return False


def prune_backups(keep: int = 3) -> None:
    if not BACKUPS.is_dir():
        return
    kept = sorted((p for p in BACKUPS.iterdir() if p.is_dir()), reverse=True)
    for old in kept[keep:]:
        shutil.rmtree(old, ignore_errors=True)


# --------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(description="Update Aether from its latest GitHub release.")
    p.add_argument("--check", action="store_true", help="Report only, change nothing")
    p.add_argument("--yes", "-y", action="store_true", help="Do not ask before updating")
    p.add_argument("--force", action="store_true",
                   help="Reinstall the latest release even if already on it")
    p.add_argument("--pre", action="store_true", help="Consider pre-releases too")
    args = p.parse_args()

    print("Aether update")
    print("=============")
    current = read_version(ROOT)
    log(f"Installed: {current}")

    release = latest_release(args.pre)
    latest = release_version(release)
    log(f"Latest {'pre-release' if release.get('prerelease') else 'release'}: "
        f"{latest or 'unknown'}  ({REPO})")

    order = compare_versions(current, latest)
    if order > 0:
        log("This copy is newer than the latest release. Nothing to do.")
        return
    if order == 0 and not args.force:
        print()
        print(f"You are on the latest release ({current}). Nothing to update.")
        return
    if order < 0:
        print()
        print(f"An update is available: {current} → {display_version(latest)}")
        notes = (release.get("body") or "").strip().splitlines()
        for line in notes[:12]:
            print(f"    {line}")
        if len(notes) > 12:
            print(f"    … {release.get('html_url') or ''}")

    if args.check:
        print()
        print("Check only; nothing was changed.")
        return

    if not args.yes:
        print()
        answer = input(f"Install {display_version(latest)} now? [Y/n]: ").strip().lower()
        if answer and not answer.startswith("y"):
            print("Cancelled.")
            return

    if (ROOT / ".git").exists():
        log("This is a git checkout; the update will overwrite tracked files.")
        if not args.yes:
            answer = input("Continue anyway? [y/N]: ").strip().lower()
            if not answer.startswith("y"):
                print("Cancelled.")
                return

    if aether_is_running():
        log("Aether looks like it is still running. Close it, then run this again.")
        if not args.yes:
            answer = input("Update anyway? [y/N]: ").strip().lower()
            if not answer.startswith("y"):
                print("Cancelled.")
                return

    url, name = pick_download(release)
    with tempfile.TemporaryDirectory(prefix="aether-update-") as tmp:
        work = Path(tmp)
        archive = download(url, work / name)
        source = extract(archive, work / "unpacked")

        new_version = read_version(source)
        files = tree_files(source)
        if not files:
            die("The downloaded release contains no files; nothing was changed.")
        stale = stale_files(files)

        requirements_moved = True
        old_req, new_req = ROOT / "requirements.txt", source / "requirements.txt"
        if old_req.exists() and new_req.exists():
            requirements_moved = old_req.read_bytes() != new_req.read_bytes()

        backup = back_up(files + stale, current)
        created: list[Path] = []
        try:
            log(f"Installing {len(files)} file(s)…")
            created = install_files(source, files)
            if stale:
                log(f"Removing {len(stale)} file(s) dropped by this release…")
                remove_files(stale)
        except (OSError, shutil.Error) as e:
            log(f"Update failed: {e}")
            log("Restoring the previous version…")
            restore(backup, created)
            die("Nothing was changed. The previous version is back in place.")

        save_manifest(new_version, latest, files)

    if requirements_moved:
        refresh_dependencies()
    prune_backups()

    print()
    print(f"Updated to {new_version}. Restart Aether to run it.")
    print()


if __name__ == "__main__":
    main()
