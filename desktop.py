# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Launch Aether as a native desktop window (WebView2 on Windows, GTK/Qt on Linux)."""
from __future__ import annotations

import atexit
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 7878
URL = f"http://{HOST}:{PORT}"
ICON = ROOT / "static" / "assets" / "aether.ico"
ICON_PNG = ROOT / "static" / "assets" / "aether.png"
DESKTOP_PID = ROOT / "data" / "aether-desktop.pid"


def _env() -> dict:
    from ollama_client import ollama_env
    env = ollama_env()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _port_open() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((HOST, PORT)) == 0


def _is_aether_server() -> bool:
    if not _port_open():
        return False
    try:
        import urllib.request

        with urllib.request.urlopen(f"{URL}/openapi.json", timeout=2) as response:
            document = json.loads(response.read().decode("utf-8"))
        return (document.get("info") or {}).get("title") == "Aether"
    except Exception:
        return False


def _activate_existing() -> bool:
    """Make repeated launcher clicks harmless while Aether is already running."""
    if not _is_aether_server():
        return False
    try:
        owner_pid = int(DESKTOP_PID.read_text(encoding="utf-8").strip())
        os.kill(owner_pid, 0)
    except (OSError, ValueError):
        # Healthy API but no live desktop owner: it is an orphaned backend. Let
        # normal startup replace it and create a usable window.
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, "Aether")
            if hwnd:
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
    # On Linux the existing window remains open; do not kill its backend merely
    # because the desktop icon was clicked twice.
    return True


def _record_desktop_owner() -> None:
    DESKTOP_PID.parent.mkdir(parents=True, exist_ok=True)
    DESKTOP_PID.write_text(str(os.getpid()), encoding="utf-8")


def _clear_desktop_owner() -> None:
    try:
        if DESKTOP_PID.read_text(encoding="utf-8").strip() == str(os.getpid()):
            DESKTOP_PID.unlink(missing_ok=True)
    except OSError:
        pass


def _kill_port() -> None:
    """Stop a stale Aether process without touching unrelated applications."""
    if not _port_open():
        return
    if not _is_aether_server():
        raise RuntimeError(
            f"Port {PORT} is already used by another application. "
            "Close that application or change Aether's port before launching."
        )
    try:
        if sys.platform == "win32":
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-NetTCPConnection -LocalPort {PORT} -ErrorAction SilentlyContinue | "
                 f"ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }}"],
                capture_output=True,
                timeout=15,
                creationflags=creation,
            )
        else:
            subprocess.run(
                ["fuser", "-k", f"{PORT}/tcp"],
                capture_output=True,
                timeout=10,
            )
    except Exception:
        pass
    for _ in range(30):
        if not _port_open():
            return
        time.sleep(0.15)


def _start_server() -> subprocess.Popen | None:
    if _port_open():
        if not _is_aether_server():
            raise RuntimeError(
                f"Port {PORT} is already used by another application. "
                "Close that application or change Aether's port before launching."
            )
        try:
            import urllib.request
            req = urllib.request.Request(f"{URL}/api/shutdown", data=b"{}", method="POST")
            req.add_header("Content-Type", "application/json")
            urllib.request.urlopen(req, timeout=15).read()
        except Exception:
            try:
                from ollama_client import unload_all_models_sync
                unload_all_models_sync()
            except Exception:
                pass
    else:
        try:
            from ollama_client import unload_all_models_sync
            unload_all_models_sync()
        except Exception:
            pass
    _kill_port()
    py = Path(sys.executable)
    if sys.platform == "win32":
        candidates = [py.with_name("pythonw.exe"), py]
        exe = next((c for c in candidates if c.exists()), py)
        kwargs: dict = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    else:
        exe = py
        kwargs = {"start_new_session": True}
    # Send the server's output to a rolling log instead of DEVNULL. Without it a
    # backend traceback is invisible: the UI just stops mid-stream with no trace.
    log_path = ROOT / "data" / "aether-server.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists() and log_path.stat().st_size > 5_000_000:
            log_path.replace(log_path.with_suffix(".log.1"))
        log = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
        log.write(f"\n===== Aether server start {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        sink: dict = {"stdout": log, "stderr": subprocess.STDOUT}
    except Exception:
        sink = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    return subprocess.Popen(
        [str(exe), "-m", "uvicorn", "server:app", "--host", HOST, "--port", str(PORT)],
        cwd=str(ROOT),
        env=_env(),
        **sink,
        **kwargs,
    )


def _wait_ready(timeout: float = 45.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open():
            return
        time.sleep(0.2)
    raise RuntimeError("Aether server failed to start")


def _windows_branding() -> None:
    """Taskbar AppUserModelID + window icon (EdgeChromium ignores pywebview icon=)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Aether.Desktop")
    except Exception:
        pass

    def _apply_icon() -> None:
        if not ICON.exists():
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            WM_SETICON = 0x0080
            IMAGE_ICON = 1
            LR_LOADFROMFILE = 0x0010
            LR_DEFAULTSIZE = 0x0040
            ico = str(ICON)
            for _ in range(80):
                hwnd = user32.FindWindowW(None, "Aether")
                if hwnd:
                    hicon = user32.LoadImageW(None, ico, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
                    if hicon:
                        user32.SendMessageW(hwnd, WM_SETICON, 1, hicon)  # big
                        user32.SendMessageW(hwnd, WM_SETICON, 0, hicon)  # small
                    break
                time.sleep(0.1)
        except Exception:
            pass

    threading.Thread(target=_apply_icon, name="aether-icon", daemon=True).start()


_cleaned = False


def _free_vram(oc=None) -> None:
    """Unload warm models immediately on window close / SIGTERM / crash-path exit."""
    global _cleaned
    if _cleaned:
        return
    _cleaned = True
    try:
        import urllib.request
        req = urllib.request.Request(f"{URL}/api/shutdown", data=b"{}", method="POST")
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req, timeout=25).read()
        return
    except Exception:
        pass
    try:
        if oc is None:
            from ollama_client import unload_all_models_sync
            unload_all_models_sync()
        else:
            oc.unload_all_models_sync()
    except Exception:
        pass


def main() -> None:
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    _windows_branding()

    if _activate_existing():
        return
    _record_desktop_owner()
    atexit.register(_clear_desktop_owner)

    import ollama_client as oc
    oc.ensure_ollama_sync()
    atexit.register(_free_vram, oc)
    if sys.platform != "win32":
        def _die(signum, _frame):
            _free_vram(oc)
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, _die)
        signal.signal(signal.SIGINT, _die)

    server = _start_server()
    try:
        _wait_ready()
        import webview

        webview.create_window(
            title="Aether",
            url=URL,
            width=1280,
            height=860,
            min_size=(880, 600),
            background_color="#070a12",
            text_select=True,
        )
        if sys.platform == "win32":
            icon = str(ICON) if ICON.exists() else (str(ICON_PNG) if ICON_PNG.exists() else None)
        else:
            # GTK cannot load compressed .ico files
            icon = str(ICON_PNG) if ICON_PNG.exists() else None
        gui = "edgechromium" if sys.platform == "win32" else "gtk"
        try:
            webview.start(gui=gui, icon=icon)
        except TypeError:
            try:
                webview.start(gui=gui)
            except Exception:
                webview.start()
        except Exception:
            if sys.platform != "win32":
                try:
                    webview.start(icon=icon)
                except Exception:
                    raise RuntimeError(
                        "Could not open the Aether window. On Ubuntu install:\n"
                        "  sudo apt install -y python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1"
                    ) from None
            else:
                webview.start(icon=icon)
    finally:
        # Always free VRAM immediately. Waits until Ollama /api/ps is empty.
        _free_vram(oc)
        if server and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except Exception:
                server.kill()


if __name__ == "__main__":
    main()
