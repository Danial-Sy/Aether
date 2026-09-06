# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Model and path configuration, portable across Windows and Linux."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION = "1.4.0"


def _path_from_env(*names: str) -> Path | None:
    for name in names:
        raw = (os.environ.get(name) or "").strip()
        if raw:
            return Path(raw).expanduser()
    return None


def _detect_ai_root() -> Path:
    env = _path_from_env("AETHER_AI_ROOT")
    if env:
        return env
    sibling = ROOT.parent
    if (sibling / "ollama" / "ollama.exe").exists() or (sibling / "ollama" / "ollama").exists():
        return sibling
    return ROOT


def _detect_ollama_exe() -> Path:
    env = _path_from_env("AETHER_OLLAMA", "OLLAMA_BIN")
    if env:
        return env
    if sys.platform == "win32":
        candidates = [
            AI_ROOT / "ollama" / "ollama.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
            Path(r"C:\Program Files\Ollama\ollama.exe"),
        ]
    else:
        candidates = [
            Path("/usr/local/bin/ollama"),
            Path("/usr/bin/ollama"),
            Path.home() / ".local" / "bin" / "ollama",
            AI_ROOT / "ollama" / "ollama",
        ]
    for p in candidates:
        if p and p.exists():
            return p
    which = shutil.which("ollama")
    return Path(which) if which else Path("ollama")


def _detect_ollama_models() -> Path | None:
    env = _path_from_env("AETHER_OLLAMA_MODELS", "OLLAMA_MODELS")
    if env:
        return env
    sibling = AI_ROOT / "ollama" / "models"
    if sibling.exists():
        return sibling
    return None


AI_ROOT = _detect_ai_root()
DATA = ROOT / "data"
CHATS = DATA / "chats"
MEMORY = DATA / "memory"
PROJECTS = DATA / "projects"
UPLOADS = DATA / "uploads"
SETTINGS_PATH = DATA / "settings" / "settings.json"

OLLAMA_EXE = _detect_ollama_exe()
OLLAMA_MODELS = _detect_ollama_models()
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
if not OLLAMA_HOST.startswith("http"):
    OLLAMA_HOST = f"http://{OLLAMA_HOST}"

# The pair Aether was built and tuned on. Any installed model can take either
# role; these are only what a fresh install starts with.
DEFAULT_CHAT_MODEL = "qwen3.8:27b"
DEFAULT_AGENT_MODEL = "qwen3.8:27b"
DEFAULT_CODER_MODEL = "qwen3-coder:30b"
TITLE_MODEL_ID = "qwen2.5:0.5b"

REQUIRED_MODELS = (TITLE_MODEL_ID, DEFAULT_CHAT_MODEL, DEFAULT_CODER_MODEL)

ROLES = ("chat", "reasoning", "agentic")

DEFAULT_COLOR = "#8b95a5"
DEFAULT_KEEP_ALIVE = "30m"

# Static profiles for the models that ship as the default. Anything else is
# profiled from /api/show at runtime; these are the fallback before a probe and
# the source of the tuning the two of them were measured with.
MODELS = {
    DEFAULT_CHAT_MODEL: {
        "id": DEFAULT_CHAT_MODEL,
        "label": "Qwen3.8 27B",
        "context_max": 262144,
        "keep_alive": DEFAULT_KEEP_ALIVE,
        "color": "#ff4d6d",
        "temperature": 0.7,
        "top_p": 0.95,
        "top_k": 20,
        "repeat_penalty": 1.0,
        "vision": True,
        "think": True,
        "tools": True,
    },
    DEFAULT_CODER_MODEL: {
        "id": DEFAULT_CODER_MODEL,
        "label": "Qwen3-Coder 30B",
        "context_max": 262144,
        "keep_alive": DEFAULT_KEEP_ALIVE,
        "color": "#00d4ff",
        "temperature": 0.7,
        "top_p": 0.8,
        "top_k": 20,
        "repeat_penalty": 1.05,
        "vision": False,
        "think": False,
        "tools": True,
    },
}

# Sampling that belongs to the job rather than the model. Reasoning runs the
# chat model cooler and with a wider top_k than ordinary chat does.
ROLE_TUNING = {
    "reasoning": {"temperature": 0.6, "top_k": 40, "repeat_penalty": 1.05},
}

# Qwen3.8 over-deliberates on "high", so reasoning caps the thinking budget.
ROLE_EFFORT = {"chat": "high", "reasoning": "medium", "agentic": "high"}

# Reasoning is the chat model wearing a different hat, so it needs its own name
# and colour in the picker, and a lower ceiling: thinking fills KV fast enough
# that the model's native window is not a safe limit for it.
ROLE_LABEL_SUFFIX = {"reasoning": " (Reasoning)"}
ROLE_COLOR = {"reasoning": "#ff2d55"}
ROLE_CONTEXT_MAX = {"reasoning": 131072}

# Chat and reasoning answer once, so they stay small. Agentic needs history:
# 32768 leaves it about 7k tokens after fixed overhead, under two tool results,
# which is where reread loops came from. 65536 leaves ~20.7k and still fits in
# ~21.1GB resident on a 24GB card.
ROLE_CONTEXT = {"chat": 32768, "reasoning": 32768, "agentic": 65536}

# Saved chats from before roles named the agentic model one of two ways.
LEGACY_MODEL = {
    "general": DEFAULT_CHAT_MODEL,
    "reasoning": DEFAULT_CHAT_MODEL,
    "agent": DEFAULT_AGENT_MODEL,
    "coder": DEFAULT_CODER_MODEL,
}

# Lightweight model used only for conversation compaction (same family, already installed).
COMPACT_MODEL_ROLE = "chat"

DEFAULT_SETTINGS = {
    "theme": "aether",
    "auto_compact_at": 0.85,
    "compact_keep_recent": 10,
    "show_thinking": True,
    # No global agent-step ceiling: productive Codex/Ollama-style loops continue
    # until the model returns a normal assistant message. This only controls the
    # deterministic circuit breaker for repeated, non-progressing calls.
    "agent_repeat_limit": 3,
    "web_search_default": False,
    # When on, the agent must ask at least one clarifying question before it starts
    # real work on a new task. Off = it asks only when it judges it necessary.
    "clarify_first_turn": False,
    # Let file tools reach the whole filesystem instead of allowed_roots.
    "unrestricted_fs": False,
    # Which roles each installed model serves, as {model_id: ["chat", "agentic"]}.
    # A model absent from this map falls back to its capabilities, so a fresh
    # install works with whatever Ollama already holds.
    "model_roles": {},
    # Last model picked per role, as {role: model_id}.
    "model_active": {},
    # Context ceiling per role, and a per-model override that wins over it. The
    # window is the job's property, not the model's: the same model needs more
    # history agentically than it does in chat.
    "role_context": dict(ROLE_CONTEXT),
    "model_context": {},
    # Bound one Ollama sampling request so a malformed/overthinking tool turn
    # falls into the agent's compact recovery lane instead of hanging forever.
    "agent_sample_timeout": 150,
    "allowed_roots": [
        str(AI_ROOT),
        str(Path.home() / "Documents"),
        str(Path.home() / "Desktop"),
    ],
    "confirm_shell_commands": True,
    # run_shell approvals:
    #   always_ask  confirm every command
    #   safe_auto   auto-run checks, builds, tests and installs; confirm
    #               anything that can modify the system
    #   never_ask   run everything without confirming
    "shell_approval_mode": "safe_auto",
    "default_project": None,
}

# Effort tiers, defaulted per model via MODELS[key]["effort"]. num_predict is the
# real lever on runaway thinking: it caps generated tokens, so a model that
# spirals inside <think> is cut off instead of burning the window.
EFFORT = {
    "minimal": {
        "temp_scale": 0.6,
        "num_predict": 1024,
        "think": False,
        "label": "Minimal",
        "hint": "Fastest. Short, direct answers.",
    },
    "low": {
        "temp_scale": 0.7,
        "num_predict": 2048,
        "think": False,
        "label": "Low",
        "hint": "Quick replies, with a little more room to answer.",
    },
    "medium": {
        "temp_scale": 0.8,
        "num_predict": 4096,
        "think": True,
        "label": "Medium",
        "hint": "Balanced. Enough room for everyday work.",
    },
    "high": {
        "temp_scale": 0.85,
        "num_predict": 8192,
        "think": True,
        "label": "High",
        "hint": "A deeper pass for harder problems.",
    },
    "max": {
        "temp_scale": 0.9,
        "num_predict": 16384,
        "think": True,
        "label": "Max",
        "hint": "The longest budget. Slowest, for the hardest tasks.",
    },
}
# Which tier thinks is a property of the tier; whether it can is a property of
# the model. Users could not tell the two apart, so every level says so.
THINKING_ON = " Thinking on."
THINKING_OFF = " Thinking off."
THINKING_UNSUPPORTED = " This model has no thinking mode, so this level is direct."

# Slider order, low → high. The UI indexes its slider straight off this.
EFFORT_ORDER = ("minimal", "low", "medium", "high", "max")
DEFAULT_EFFORT = "high"


def effort_for(role: str) -> str:
    """Default effort tier for a role (falls back to DEFAULT_EFFORT)."""
    tier = ROLE_EFFORT.get(role) or DEFAULT_EFFORT
    return tier if tier in EFFORT else DEFAULT_EFFORT

SYSTEM_PROMPTS = {
    "chat": (
        "You are Aether. Be clear and direct. Use markdown when it helps. For math and symbols, prefer Unicode (√, ², π, ≈, ≤, ≥, ∞, →, ≠) and write i for √−1. Do not use LaTeX ($...$, \\frac, \\sqrt) unless the user asks for LaTeX source. Honor lasting memory notes when provided. Lasting memory is saved only when the user explicitly asks (remember / memorize / store this). If they ask whether you remember something, use Memory recall results or lasting notes: say yes with the note, or honestly say it is not in lasting memory. If images are attached, inspect them carefully. You can reference other past chats listed under Chat history. When the user asks about another conversation, use any attached past-chat excerpts and the history index. Prefer recalling specifics over saying you cannot access past chats."
    ),
    "reasoning": (
        "You are Aether Deep Reasoning. "
        "Reason inside <think>...</think>, then give a precise final answer. "
        "Prefer correctness and structured logic. For math use Unicode, not LaTeX $...$. "
        "Budget your reasoning: think long enough to be right, then commit. "
        "Do not re-derive the same result multiple ways, second-guess a settled "
        "conclusion, or enumerate cases you have already ruled out. "
        "Use Chat history and Memory when the user asks about prior conversations or lasting notes."
    ),
    "agentic": (
        "You are Aether Agentic, an agentic coding companion with tools for files, "
        "search, shell and git. "
        "Read before editing. Prefer minimal correct diffs. Follow the tool protocol exactly. "
        "ONE CONTINUOUS JOB per user message: use tools quietly until the plan is done, then give ONE final debrief. "
        "Do not treat [agent-loop] lines or tool results as new user requests. "
        "Put working notes in thinking; the visible reply is only the final debrief "
        "(what you changed, what you found, what you recommend next). "
        "If the user interrupts with a correction mid-job, update the plan (cancel obsolete steps) and continue. Do not restart from scratch. "
        "For multi-step work, maintain a plan with todo_write. "
        "When the user attaches files, they appear as path pointers, so use read_file/list_dir on those paths; "
        "do not assume file contents were pasted into the chat. "
        "DECIDE, do not ask. Infer what the user wants from the request and sensible defaults, "
        "make the call, and state the assumption in one line of your final debrief. "
        "ask_user is for a decision you genuinely cannot make for them: work that would be "
        "wasted or destroyed if you guessed wrong, or a choice only they can know. Taste, "
        "naming, layout, styling and scope-of-flourish are yours to choose. If you do ask, "
        "ask ONCE, before you start building, never mid-build. "
        "Consult the Established canon first so you never re-ask a settled question."
    ),
}

# Which model is actually answering. Sits in the cached prompt prefix, so it
# costs nothing per step, and it stops the model guessing at its own identity.
MODEL_IDENTITY = " You are running {label} locally through Ollama."

# Appended for any model that reports the vision capability.
VISION_ADDON = (
    " You can also see images. When a screenshot is attached, inspect it carefully and "
    "describe on-screen text, layout and UI elements precisely."
)

# The brief is the only thing that survives a compaction boundary, so it is
# structured rather than prose. A prose blob dropped exactly what a half-finished
# run needs: which files were touched and what the next action was.
COMPACT_PROMPT = (
    "You are compacting an in-progress coding session so another model can pick it "
    "up with no other context. Write a dense brief under these exact headings:\n"
    "## Objective: what the user asked for, in their terms, including constraints "
    "and preferences they stated.\n"
    "## Done: what has actually been completed and verified. Only claim work that "
    "a tool result confirmed.\n"
    "## Files: every path read or modified, with one line on what it contains or "
    "what changed in it.\n"
    "## State: where the work stands right now: the active step, anything "
    "half-finished, and errors or rejections still unresolved.\n"
    "## Next: the concrete next action, specific enough to execute immediately.\n"
    "Bullets under each heading. Keep exact identifiers, paths, signatures and "
    "error text verbatim; they cannot be recovered later. No preamble, no "
    "commentary, and never invent progress that the transcript does not show."
)

TITLE_PROMPT = (
    "Create a short chat title (3–6 words) for this conversation. "
    "No quotes, no trailing punctuation, no emoji. Title case when natural. "
    "Reply with the title only."
)
