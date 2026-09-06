# Aether

A local AI desktop app. Chat and a real coding agent, both running on your own
machine through Ollama.

Two tabs. **Chat** is a normal conversation, with a deeper reasoning mode and
vision when the model has it. **Agentic** is a full tool-calling agent that
reads and writes files in a project you point it at, runs shell commands, keeps
a task list, and asks you when it needs a decision.

Everything stays on the computer. Model weights are downloaded by Ollama and are
not part of this repository. Web search is optional and off by default.

**Any Ollama model works, and any GGUF on Hugging Face.** The installer detects
your hardware and suggests models that suit it. Afterwards, a browser inside
Aether covers both libraries: everything on
[ollama.com/library](https://ollama.com/library), and the tens of thousands of
GGUF repositories on [Hugging Face](https://huggingface.co/models?library=gguf),
searchable, sortable and filtered to what your machine can hold. Picking a model
shows every version of it with real download sizes, and says up front what a
model is missing — no tool support, no chat template, vision that will not
survive the import. Ollama does the download either way, so a Hugging Face model
is probed, assigned and run by the same code as any other. Each one is assigned
to Chat, Agentic, or both, and capabilities are read from Ollama, so a model
that cannot call tools is never offered for agentic work.

The defaults are Qwen3.8 27B and Qwen3-Coder 30B, which is what Aether was
built and tuned on. They need a 24 GB card. Everything below still works on
less.

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

Aether runs on what you have. The installer reads your GPU, memory and free
disk, then suggests models sized for it:

| Usable memory | Suggested |
|---|---|
| under 6 GB | one 4B model doing both jobs |
| 6 to 12 GB | 8B for chat, 7B coder for agentic |
| 12 to 20 GB | 14B pair |
| 20 to 64 GB | Qwen3.8 27B and Qwen3-Coder 30B, the tuned default |
| 64 GB and up | adds a 70B for chat |
| no GPU | 8B and 7B, sized for speed rather than for what fits |

VRAM is what counts when you have a GPU. Without one, models run on the CPU, and
the suggestion is capped well below what would merely fit: 128 GB of RAM will
hold a 70B, and it will answer at about a word a second.

The tuned default needs a 24 GB card. Qwen3.8 27B is about 17.7 GB resident and
the KV cache at the agentic window adds roughly 5.8 GB, so the working set sits
near 21 GB. Aether works out the largest window each model can have on your
machine and offers exactly those.

A small model (`qwen2.5:0.5b`, about 400 MB) comes with every choice. It only
writes chat titles, on the CPU, so naming a conversation never interrupts the
model you are talking to.

## Windows

1. Download and extract the latest release.
2. Double-click **`Install-Aether.bat`**.
3. Choose Default, Recommended for your PC, or type your own model tag.
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

To move to a newer version later, double-click **`Update-Aether.bat`**.

## Ubuntu

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip python3-gi \
  gir1.2-gtk-3.0 gir1.2-webkit2-4.1
curl -fsSL https://ollama.com/install.sh | sh

chmod +x setup.sh aether.sh install-desktop.sh update.sh
./setup.sh
./aether.sh
```

If `gir1.2-webkit2-4.1` is not available, install `gir1.2-webkit2-4.0`.

`setup.sh` asks which models you want: Default, Recommended for your machine, or
a tag you type. Use `--yes` to take the recommendation without being asked,
`--tags qwen3:8b` to name models directly, or `--skip-models` if Ollama already
has the weights.

`setup.bat` and `setup.py` are the direct entry points if Python and Ollama are
already set up and you do not want the wizard.

## Updating

```bash
./update.sh          # Ubuntu
```

```bat
Update-Aether.bat
```

`data/` and `.venv/` are never touched, so chats, memory, projects, settings and
uploads survive an update.

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
| `AETHER_UPDATE_REPO` | GitHub repository the updater tracks |
| `GITHUB_TOKEN` | Raises the updater's GitHub rate limit |

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

The archive excludes `.venv`, model weights, and everything under `data/`. That
published release is what `update.py` compares against, so bump `VERSION` in
`config.py` and `aether.json` in the same commit as the tag — the updater reads
`config.py` to decide whether a copy is current.

## License

Aether is licensed under the GNU Affero General Public License v3.0. The full
text is in [LICENSE](LICENSE).

Copyright (C) 2026 Danial Syed

In plain terms: use it, modify it, and share it freely, including running it
yourself for whatever you like. If you distribute a modified version, or run a
modified version as a service other people can use over a network, you have to
publish your source under the same license. That summary is not a substitute for
the license text.
