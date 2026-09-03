# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""One-shot installer: venv, Python deps, Ollama check, model pulls.

Usage:
  python setup.py              # full install + all models
  python setup.py --skip-models
  python setup.py --models chat   # title + chat only
  python setup.py --models all
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
if not OLLAMA_HOST.startswith("http"):
    OLLAMA_HOST = f"http://{OLLAMA_HOST}"

MODEL_SETS = {
    "titles": ["qwen2.5:0.5b"],
    "chat": ["qwen2.5:0.5b", "qwen3.8:27b"],
    "all": [
        "qwen2.5:0.5b",
        "qwen3.8:27b",
        "qwen3-coder:30b",
    ],
}


def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def venv_python() -> Path:
    if sys.platform == "win32":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def ensure_python() -> None:
    if sys.version_info < (3, 11):
        die(f"Python 3.11+ required (found {sys.version.split()[0]})")
    log(f"Python {sys.version.split()[0]}")


def _create_venv_via_virtualenv(distro_py: str) -> None:
    """Ubuntu often lacks `python3-venv`; virtualenv can still target /usr/bin/python3."""
    bootstrap = sys.executable
    try:
        subprocess.check_call([bootstrap, "-m", "pip", "install", "virtualenv"])
    except (subprocess.CalledProcessError, FileNotFoundError):
        die(
            "Could not create venv. On Ubuntu run:\n"
            "  sudo apt install -y python3.12-venv python3-gi python3-gi-cairo"
        )
    cmd = [bootstrap, "-m", "virtualenv", "-p", distro_py]
    if sys.platform != "win32":
        cmd.append("--system-site-packages")
    cmd.append(str(VENV))
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError:
        die(
            "Could not create venv. On Ubuntu run:\n"
            "  sudo apt install -y python3.12-venv python3-gi python3-gi-cairo"
        )


def _venv_needs_recreate(py: Path) -> bool:
    if not py.exists():
        return True
    if sys.platform == "win32":
        return False
    cfg = VENV / "pyvenv.cfg"
    if not cfg.exists():
        return True
    text = cfg.read_text(encoding="utf-8", errors="ignore")
    keys = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            keys[k.strip().lower()] = v.strip().lower()
    home = keys.get("home", "")
    executable = keys.get("executable", "") + " " + keys.get("base-executable", "")
    # Anaconda/conda Pythons cannot load Ubuntu WebKitGTK (`gi`).
    if any(s in home or s in executable for s in ("anaconda", "miniconda", "/envs/")):
        return True
    if keys.get("include-system-site-packages") != "true":
        return True
    return False


def ensure_venv() -> Path:
    py = venv_python()
    if _venv_needs_recreate(py):
        if VENV.exists():
            log("Recreating virtualenv for Ubuntu desktop (system Python + GTK)…")
            shutil.rmtree(VENV)
        else:
            log("Creating virtualenv…")
        distro_py = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
        args = [distro_py if sys.platform != "win32" else sys.executable, "-m", "venv"]
        if sys.platform != "win32":
            args.append("--system-site-packages")
        args.append(str(VENV))
        try:
            subprocess.check_call(args)
        except (subprocess.CalledProcessError, FileNotFoundError):
            log("stdlib venv unavailable; using virtualenv…")
            _create_venv_via_virtualenv(distro_py)
    if not py.exists():
        die("venv created but python not found")
    log(f"venv → {py}")
    return py


def pip_install(py: Path) -> None:
    log("Installing Python packages…")
    subprocess.check_call([str(py), "-m", "pip", "install", "--upgrade", "pip"], cwd=str(ROOT))
    req = ROOT / "requirements.txt"
    subprocess.check_call([str(py), "-m", "pip", "install", "-r", str(req)], cwd=str(ROOT))
    log("Python packages ready (FastAPI, search/ddgs, pywebview, Pillow).")


def find_ollama() -> str | None:
    # config.py is not importable on a fresh clone, so detect the same way
    env = (os.environ.get("AETHER_OLLAMA") or os.environ.get("OLLAMA_BIN") or "").strip()
    if env and Path(env).exists():
        return env
    which = shutil.which("ollama")
    if which:
        return which
    extras = []
    if sys.platform == "win32":
        extras = [
            Path(r"C:\AI\ollama\ollama.exe"),
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
            Path(r"C:\Program Files\Ollama\ollama.exe"),
        ]
    else:
        extras = [
            Path("/usr/local/bin/ollama"),
            Path("/usr/bin/ollama"),
            Path.home() / ".local" / "bin" / "ollama",
        ]
    for p in extras:
        if p.exists():
            return str(p)
    return None


def print_ollama_install_help() -> None:
    print()
    print("Ollama is not installed (or not on PATH).")
    print("  Windows: https://ollama.com/download")
    print("  Ubuntu:  curl -fsSL https://ollama.com/install.sh | sh")
    print()


def ollama_up(bin_path: str) -> bool:
    try:
        urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=2)
        return True
    except Exception:
        pass
    env = dict(os.environ)
    kwargs: dict = {"env": env}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([bin_path, "serve"], **kwargs)
    except Exception as e:
        log(f"could not start ollama serve: {e}")
        return False
    for _ in range(40):
        try:
            urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=2)
            return True
        except Exception:
            time.sleep(0.4)
    return False


def normalize_model_tag(name: str) -> str:
    """Normalize Ollama's CLI/API spellings without conflating custom models."""
    value = (name or "").strip().lower()
    for prefix in ("registry.ollama.ai/library/", "library/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    if value and ":" not in value:
        value += ":latest"
    return value


def model_is_installed(models: set[str], tag: str) -> bool:
    wanted = normalize_model_tag(tag)
    return any(normalize_model_tag(model) == wanted for model in models)


def _installed_models_from_api() -> set[str]:
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST.rstrip('/')}/api/tags", timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return set()
    names = set()
    for model in payload.get("models", []):
        if not isinstance(model, dict):
            continue
        name = str(model.get("name") or model.get("model") or "").strip()
        if name:
            names.add(name)
    return names


def installed_models(bin_path: str) -> set[str]:
    # The JSON API is stable across CLI formatting/localization changes and also
    # works when Ollama is not on PATH. Keep the CLI as a compatibility fallback.
    names = _installed_models_from_api()
    if names:
        return names
    try:
        out = subprocess.check_output(
            [bin_path, "list"], text=True, errors="replace", timeout=30
        )
    except Exception:
        return set()
    names = set()
    for line in out.splitlines()[1:]:
        name = (line.split() or [""])[0].strip()
        if name:
            names.add(name)
    return names


def pull_models(bin_path: str, tags: list[str]) -> None:
    have = installed_models(bin_path)
    missing = [tag for tag in tags if not model_is_installed(have, tag)]
    if not missing:
        log("All requested models are already installed; no downloads needed.")
    for tag in tags:
        if model_is_installed(have, tag):
            log(f"already have {tag}")
            continue
        log(f"pulling {tag}  (this can take a while; weights are not in git)")
        rc = subprocess.call([bin_path, "pull", tag])
        if rc != 0:
            die(f"ollama pull {tag} failed (exit {rc})")
        # Do not trust exit status alone: verify the model is visible in the same
        # Ollama store Aether will use. This catches wrong-host/store setups.
        have = installed_models(bin_path)
        if not model_is_installed(have, tag):
            die(
                f"ollama pull {tag} returned success, but the model is not visible. "
                "Check OLLAMA_HOST / OLLAMA_MODELS and run setup again."
            )
    unresolved = [tag for tag in tags if not model_is_installed(have, tag)]
    if unresolved:
        die(f"Models still missing after setup: {', '.join(unresolved)}")
    log("Models ready.")


def linux_notes() -> None:
    if sys.platform == "win32":
        return
    print()
    print("Ubuntu window (pywebview) needs WebKitGTK:")
    print("  sudo apt update")
    print("  sudo apt install -y python3-venv python3-pip python3-gi")
    print("  sudo apt install -y gir1.2-gtk-3.0 gir1.2-webkit2-4.1")
    print("  # if the last package is missing, try: gir1.2-webkit2-4.0")
    print()


def install_desktop_entry() -> None:
    """Put a clickable launcher in the app menu and on the Desktop (Linux only)."""
    if sys.platform == "win32":
        return
    script = ROOT / "install-desktop.sh"
    if not script.exists():
        return
    try:
        script.chmod(0o755)
        rc = subprocess.call(["bash", str(script)])
        if rc != 0:
            log("desktop launcher install skipped (non-fatal)")
    except Exception as e:
        log(f"desktop launcher install skipped: {e}")


def main() -> None:
    p = argparse.ArgumentParser(description="Install Aether (venv, deps, Ollama models).")
    p.add_argument("--skip-models", action="store_true", help="Skip ollama pull")
    p.add_argument(
        "--models",
        choices=sorted(MODEL_SETS),
        default="all",
        help="Which models to pull (default: all)",
    )
    args = p.parse_args()

    print("Aether setup")
    print("============")
    ensure_python()
    (ROOT / "data" / "chats").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "uploads").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "settings").mkdir(parents=True, exist_ok=True)
    py = ensure_venv()
    pip_install(py)

    ollama = find_ollama()
    if not ollama:
        print_ollama_install_help()
        if not args.skip_models:
            die("Install Ollama, then re-run setup.py")
        log("Continuing without models.")
    else:
        log(f"Ollama → {ollama}")
        if not ollama_up(ollama):
            die("Ollama did not start. Open a terminal and run: ollama serve")
        if not args.skip_models:
            pull_models(ollama, MODEL_SETS[args.models])

    install_desktop_entry()

    linux_notes()
    print()
    print("Done. Launch with:")
    if sys.platform == "win32":
        print("  Aether.bat")
        print("  or:  .venv\\Scripts\\pythonw.exe desktop.py")
    else:
        print("  the Aether icon on your Desktop or in the app menu")
        print("  or:  ./aether.sh")
        print("  or:  .venv/bin/python desktop.py")
    print()


if __name__ == "__main__":
    main()
