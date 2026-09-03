# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Native file pickers: tkinter on Windows, zenity or kdialog on Linux."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _gui_env() -> dict:
    env = dict(os.environ)
    # Ensure a display for dialogs when launched from a desktop shortcut
    if sys.platform != "win32" and not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        env["DISPLAY"] = ":0"
    return env


def _run(cmd: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_gui_env(),
    )


def pick_folder(title: str = "Select folder") -> str | None:
    if sys.platform == "win32":
        return _pick_folder_tk(title)
    path = _pick_folder_linux(title)
    if path is not None:
        return path or None
    # Last resort; fails if python3-tk is missing
    return _pick_folder_tk(title)


def pick_files(title: str = "Select files") -> list[str]:
    if sys.platform == "win32":
        return _pick_files_tk(title)
    paths = _pick_files_linux(title)
    if paths is not None:
        return paths
    return _pick_files_tk(title)


def _pick_folder_linux(title: str) -> str | None:
    """Return selected path, '' if cancelled, or None if no dialog backend is installed."""
    if shutil.which("zenity"):
        r = _run(["zenity", "--file-selection", "--directory", f"--title={title}"])
        if r.returncode == 0:
            return (r.stdout or "").strip() or ""
        if r.returncode == 1:
            return ""  # cancel
        raise RuntimeError((r.stderr or r.stdout or "zenity failed").strip())
    if shutil.which("kdialog"):
        r = _run(["kdialog", "--getexistingdirectory", str(Path.home()), "--title", title])
        if r.returncode == 0:
            return (r.stdout or "").strip() or ""
        if r.returncode == 1:
            return ""
        raise RuntimeError((r.stderr or r.stdout or "kdialog failed").strip())
    return None


def _pick_files_linux(title: str) -> list[str] | None:
    """Return selected paths, [] if cancelled, or None if no dialog backend is installed."""
    if shutil.which("zenity"):
        r = _run(["zenity", "--file-selection", "--multiple", "--separator=\n", f"--title={title}"])
        if r.returncode == 0:
            return [p for p in (r.stdout or "").splitlines() if p.strip()]
        if r.returncode == 1:
            return []
        raise RuntimeError((r.stderr or r.stdout or "zenity failed").strip())
    if shutil.which("kdialog"):
        r = _run(["kdialog", "--getopenfilename", str(Path.home()), "*", "--title", title, "--multiple"])
        if r.returncode == 0:
            raw = (r.stdout or "").strip()
            if not raw:
                return []
            if "\n" in raw:
                return [p for p in raw.splitlines() if p.strip()]
            if "|" in raw:
                return [p.strip() for p in raw.split("|") if p.strip()]
            return [raw]
        if r.returncode == 1:
            return []
        raise RuntimeError((r.stderr or r.stdout or "kdialog failed").strip())
    return None


def _pick_folder_tk(title: str) -> str | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as e:
        raise RuntimeError(
            "Folder picker unavailable. On Ubuntu install: sudo apt install -y zenity python3-tk"
        ) from e
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    try:
        path = filedialog.askdirectory(title=title) or ""
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    return path or None


def _pick_files_tk(title: str) -> list[str]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as e:
        raise RuntimeError(
            "File picker unavailable. On Ubuntu install: sudo apt install -y zenity python3-tk"
        ) from e
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    try:
        paths = filedialog.askopenfilenames(title=title)
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    return list(paths or [])
