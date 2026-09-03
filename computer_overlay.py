# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Faint multi-color screen border while Computer Use is active (Claude-style cue)."""
from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
_thread: threading.Thread | None = None
_root: Any = None
_enabled = False

# Brand palette: red -> purple -> blue -> cyan
_COLORS = ("#ff2d55", "#7c5cff", "#4f7cff", "#00d4ff")
_BORDER = 10
_ALPHA = 0.42


def is_active() -> bool:
    return _enabled


def set_overlay(enabled: bool) -> dict:
    global _enabled
    with _lock:
        _enabled = bool(enabled)
        if _enabled:
            _ensure_thread()
        else:
            _request_close()
    return {"ok": True, "enabled": _enabled}


def _ensure_thread() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_run_ui, name="aether-cu-overlay", daemon=True)
    _thread.start()


def _request_close() -> None:
    root = _root
    if root is not None:
        try:
            root.after(0, root.destroy)
        except Exception:
            pass


def _run_ui() -> None:
    global _root
    try:
        import tkinter as tk
    except Exception:
        return

    root = tk.Tk()
    _root = root
    root.title("Aether Computer Use")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    try:
        root.attributes("-alpha", _ALPHA)
    except Exception:
        pass
    root.configure(bg="black")

    try:
        w = int(root.winfo_vrootwidth())
        h = int(root.winfo_vrootheight())
        x = int(root.winfo_vrootx())
        y = int(root.winfo_vrooty())
    except Exception:
        w, h, x, y = 1920, 1080, 0, 0
    root.geometry(f"{w}x{h}+{x}+{y}")

    try:
        import ctypes

        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        if not hwnd:
            hwnd = root.winfo_id()
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x80000
        WS_EX_TRANSPARENT = 0x20
        WS_EX_TOOLWINDOW = 0x80
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    except Exception:
        pass

    canvas = tk.Canvas(root, width=w, height=h, highlightthickness=0, bd=0, bg="black")
    canvas.pack(fill="both", expand=True)

    b = _BORDER
    canvas.create_rectangle(0, 0, w, b, fill=_COLORS[0], outline="")
    canvas.create_rectangle(w - b, 0, w, h, fill=_COLORS[1], outline="")
    canvas.create_rectangle(0, h - b, w, h, fill=_COLORS[2], outline="")
    canvas.create_rectangle(0, 0, b, h, fill=_COLORS[3], outline="")
    for i, (cx, cy) in enumerate(((0, 0), (w - b, 0), (w - b, h - b), (0, h - b))):
        canvas.create_rectangle(cx, cy, cx + b, cy + b, fill=_COLORS[i % 4], outline="")

    try:
        root.wm_attributes("-transparentcolor", "black")
        canvas.create_rectangle(b, b, w - b, h - b, fill="black", outline="")
    except Exception:
        pass

    def poll() -> None:
        if not _enabled:
            try:
                root.destroy()
            except Exception:
                pass
            return
        root.after(200, poll)

    root.after(200, poll)
    try:
        root.mainloop()
    finally:
        _root = None
