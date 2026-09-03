# Aether

A local AI desktop app. Chat and a real coding agent, both running on your own
machine through Ollama.

Two tabs. **Chat** is a normal conversation with Qwen3.8 27B, with a deeper
reasoning mode and vision. **Agentic** is a full tool-calling agent that reads
and writes files in a project you point it at, runs shell commands, keeps a task
list, and asks you when it needs a decision.

Everything stays on the computer. Model weights are downloaded by Ollama and are
not part of this repository. Web search is optional and off by default.

> **Before you install:** Aether is currently built for two specific models,
> Qwen3.8 27B and Qwen3-Coder 30B, and it needs a GPU with at least **24 GB of
> VRAM** to be usable. On a smaller card the model will either crawl or fail to
> load outright.
>
> A future version will detect your hardware and pull smaller models that fit, so
> Aether can run on weaker cards too. That is not in this release. For now, plan
> on those two models and 24 GB of VRAM.

## What it can do

- Read, write and edit files in a registered project, with anchored edits, syntax
  validation before every commit, and automatic backups
- Run shell commands, with an approval mode that auto-runs safe work and asks
  about anything that could change the machine
- Keep a visible plan and work through it without supervision
- Ask clarifying questions
- Delegate expensive survey work to a read-only sub-agent on large projects
- Handle images and attachments, and drive the desktop in computer-use mode
- Compact its own context so long runs do not fall over

If you want to know how any of that works, [ARCHITECTURE.md](ARCHITECTURE.md) is
the reference.

## Hardware

Aether needs a GPU with at least 24 GB of VRAM. Qwen3.8 27B is about 17.7 GB
resident, and the KV cache at the agentic default adds roughly 5.8 GB on top, so
the working set sits near 21 GB with a little headroom on a 24 GB card. Below
that, the model spills into system RAM and becomes unusably slow, or does not
load at all. 32 GB of system RAM is also worth having.

| Profile | Models | Download |
|---|---|---:|
| Chat | `qwen2.5:0.5b`, `qwen3.8:27b` | ~19 GB |
| Full | Chat plus `qwen3-coder:30b` | ~38 GB |

The small model only writes chat titles and runs on CPU.

## Windows

1. Download and extract the latest release.
2. Double-click **`Install-Aether.bat`**.
3. Accept the suggested model profile or pick another.
4. Launch from the new Desktop or Start Menu shortcut.

The installer is a wizard. It detects or installs Python 3.11+ and Ollama through
`winget`, checks RAM, NVIDIA VRAM and free disk, inventories the models you
already have and pulls only what is missing, configures Ollama for flash
attention and a q8 KV cache, builds an isolated `.venv`, and creates the
shortcuts. It is safe to run again: existing weights are kept and shortcuts are
repointed at the current folder.

For unattended installs:

```bat
Install-Aether.bat -Models Chat -NonInteractive
Install-Aether.bat -Models Full -NonInteractive
Install-Aether.bat -SkipModels -NonInteractive
```

## Ubuntu

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip python3-gi \
  gir1.2-gtk-3.0 gir1.2-webkit2-4.1
curl -fsSL https://ollama.com/install.sh | sh

chmod +x setup.sh aether.sh install-desktop.sh
./setup.sh
./aether.sh
```

If `gir1.2-webkit2-4.1` is not available, install `gir1.2-webkit2-4.0`. Use
`./setup.sh --models chat` for the smaller profile, or `--skip-models` if Ollama
already has the weights.

`setup.bat` and `setup.py` are the direct entry points if Python and Ollama are
already set up and you do not want the wizard.

## Your data

Chats, memory, projects, settings and uploads live under `data/`, which is
gitignored so a commit or a release cannot publish your conversations.

The agent can only touch registered projects, Aether's own folder, Documents,
Desktop, and files you upload. Shell commands are classified before they run:
reads, builds, tests and installs go through, and anything that could destroy
data, escalate privilege or change the system asks first. You can widen or narrow
this in Settings.

## Configuration

Defaults assume a standard local Ollama install. For custom layouts:

| Variable | Purpose |
|---|---|
| `AETHER_OLLAMA` | Path to `ollama` or `ollama.exe` |
| `AETHER_OLLAMA_MODELS` | Custom Ollama model directory |
| `AETHER_AI_ROOT` | A workspace containing a sibling `ollama` directory |
| `OLLAMA_HOST` | Ollama API address |

Model metadata, context sizes and system prompts are in `config.py`. Per-user
settings are written to `data/settings/settings.json` at runtime.

## Development

```bash
python3 setup.py --skip-models
.venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 7878
```

On Windows use `py -3` and `.venv\Scripts\python.exe`.

Tests:

```bash
.venv/bin/python -m unittest discover -s tests
```

The suite skips the server tests when FastAPI is not importable, so run it from
the venv rather than a bare `python3`. Quick static checks:

```bash
python3 -m compileall -q -x '(^|/)(data|\.venv)(/|$)' .
node --check static/app.js
```

## Releases

CI validates Python and JavaScript on every push, and a Windows runner parses the
PowerShell wizard and runs its model-detection tests. Pushing a version tag builds
a source ZIP and publishes it as a GitHub release:

```bash
git tag v1.3.0
git push origin v1.3.0
```

The archive excludes `.venv`, model weights, and everything under `data/`.

## License

Aether is licensed under the GNU Affero General Public License v3.0. The full
text is in [LICENSE](LICENSE).

Copyright (C) 2026 Danial Syed

In plain terms: use it, modify it, and share it freely, including running it
yourself for whatever you like. If you distribute a modified version, or run a
modified version as a service other people can use over a network, you have to
publish your source under the same license. That summary is not a substitute for
the license text.
