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

# models.py imports only the standard library, so it works here, before pip has
# run. Everything about which model is which lives in catalog.json, not here.
sys.path.insert(0, str(ROOT))
import models as reg  # noqa: E402

GIB = 1024 ** 3


def _tier_tags(tier: dict) -> list[str]:
    seen, out = set(), []
    for entry in list(tier.get("utility") or []) + list(tier.get("models") or []):
        tag = entry.get("tag")
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def default_tags() -> list[str]:
    """The pair Aether was built and tuned on, plus the title model."""
    catalog = reg.load_catalog()
    tier = next((t for t in catalog.get("tiers") or [] if t.get("id") == "default"), None)
    if not tier:
        return [reg.config.TITLE_MODEL_ID, reg.config.DEFAULT_CHAT_MODEL,
                reg.config.DEFAULT_CODER_MODEL]
    return _tier_tags({**tier, "utility": catalog.get("utility")})


MODEL_SETS = {
    "titles": [reg.config.TITLE_MODEL_ID],
    "chat": [reg.config.TITLE_MODEL_ID, reg.config.DEFAULT_CHAT_MODEL],
    "all": default_tags(),
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


def describe_hardware(hw: dict) -> None:
    if hw["vram_bytes"]:
        log(f"GPU: {hw['gpu']} ({hw['vram_bytes'] / GIB:.0f} GB VRAM)")
    else:
        log("GPU: none detected, models will run on the CPU")
    log(f"RAM: {hw['ram_bytes'] / GIB:.0f} GB")
    log(f"Free disk for models: {hw['free_disk_bytes'] / GIB:.0f} GB at {hw['model_store']}")


def download_size(tags: list[str], installed: set[str]) -> tuple[int, list[str]]:
    """Bytes this choice will actually download, skipping what is already here."""
    missing = [t for t in tags if not model_is_installed(installed, t)]
    return sum(reg.manifest_size(t) for t in missing), missing


def _print_choice(number: int, title: str, note: str, tags: list[str],
                  installed: set[str]) -> int:
    size, missing = download_size(tags, installed)
    have = len(tags) - len(missing)
    detail = f"{size / GIB:.1f} GB to download" if missing else "already installed"
    if missing and have:
        detail += f", {have} already here"
    print(f"  {number}. {title} ({detail})")
    print(f"     {note}")
    for tag in tags:
        mark = "have" if model_is_installed(installed, tag) else "get "
        print(f"       [{mark}] {tag}")
    return size


def choose_models(installed: set[str], non_interactive: bool = False) -> list[str]:
    """Default, Recommended for this machine, or a tag the user types."""
    hw = reg.hardware()
    catalog = reg.load_catalog()
    print()
    print("This computer")
    print("-------------")
    describe_hardware(hw)

    usable_gb = (hw.get("recommend_bytes") or hw["usable_bytes"] or 0) / GIB
    default = default_tags()
    tier = reg.recommend(usable_gb, catalog)
    recommended = _tier_tags(tier)

    print()
    print("Which models?")
    print("-------------")
    default_size = _print_choice(1, "Default", "What Aether was built and tuned on. Needs about 24 GB of VRAM.", default, installed)
    print()
    sized_for = ("CPU only, so this is sized for speed rather than for what fits."
                 if hw.get("cpu_only") else f"Sized for {usable_gb:.0f} GB of usable memory.")
    rec_size = _print_choice(2, "Recommended", tier.get("note") or sized_for, recommended, installed)
    print()
    print("  3. Type your own")
    print(f"     Any tag from {catalog.get('library_url')}. Checked before anything downloads.")
    print()

    # Recommended is sized for this machine, so it is the default answer unless
    # it is already the tuned pair, in which case the two choices are the same.
    pick = "1" if set(recommended) == set(default) else "2"
    if non_interactive:
        log(f"Non-interactive: choosing {pick}")
    else:
        answer = input(f"Choose 1, 2 or 3 [recommended: {pick}]: ").strip()
        pick = answer or pick

    if pick == "1":
        chosen, size = default, default_size
    elif pick == "3":
        chosen, size = choose_custom(catalog, installed)
    else:
        chosen, size = recommended, rec_size

    free = hw["free_disk_bytes"]
    if size and free > 0 and size > free:
        die(f"That needs about {size / GIB:.1f} GB free; only {free / GIB:.1f} GB is available "
            f"at {hw['model_store']}.")
    return chosen


def choose_custom(catalog: dict, installed: set[str]) -> tuple[list[str], int]:
    """Type tags. Each is checked against Ollama's registry before it is kept."""
    print()
    print(f"Browse models at {catalog.get('library_url')} and copy the tag, for example qwen3:8b.")
    print("Enter one tag per line. Blank line when done.")
    chosen: list[str] = [reg.config.TITLE_MODEL_ID]
    total = 0
    while True:
        raw = input("  tag: ").strip()
        if not raw:
            if len(chosen) > 1:
                break
            print("     At least one model is needed.")
            continue
        tag = reg.normalize_tag(raw)
        if model_is_installed(installed, tag):
            print(f"     {tag} is already installed, adding it.")
            chosen.append(tag)
            continue
        size = reg.manifest_size(tag)
        if not size:
            print(f"     Ollama has no model called {raw}. Check the tag and try again.")
            continue
        total += size
        chosen.append(tag)
        print(f"     {tag} found, {size / GIB:.1f} GB.")
    return chosen, total


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
        default=None,
        help="Skip the wizard and pull a named set instead",
    )
    p.add_argument(
        "--yes", action="store_true",
        help="Take the wizard's recommendation without asking",
    )
    p.add_argument(
        "--tags", nargs="+", metavar="TAG",
        help="Pull exactly these model tags (used by the Windows installer)",
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
            if args.tags:
                tags = list(args.tags)
            elif args.models:
                tags = MODEL_SETS[args.models]
            else:
                tags = choose_models(installed_models(ollama), non_interactive=args.yes)
            pull_models(ollama, tags)

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
