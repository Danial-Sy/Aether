# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Agentic tools: project-scoped file and shell operations."""
from __future__ import annotations

import difflib
import hashlib
import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from config import DATA
from storage import load_settings

READ_FILE_CONTENT_MAX_CHARS = 7200
MAX_EDIT_RANGES = 64

# Every tool overwrite snapshots the previous contents here first.
BACKUP_DIR = DATA / "backups"
BACKUP_KEEP = 10
# Oversized tool results are written here and replaced in the prompt by a
# head/tail excerpt plus the path. Truncating throws away the tail the model
# usually needs next. Offloading loses nothing: it can re-read any range.
TOOLOUT_DIR = DATA / "toolout"
TOOLOUT_KEEP = 60
# Above this the result is offloaded. Matches the render-time truncation point,
# so anything that would have been cut is now a file instead.
OFFLOAD_CHARS = 8000
OFFLOAD_HEAD_CHARS = 3000
OFFLOAD_TAIL_CHARS = 1200
# Shell output is captured whole and offloaded, not sliced at capture time.
# This cap only guards against a runaway command eating memory.
SHELL_OUTPUT_HARD_CAP = 400_000
MAX_EDIT_TOTAL_CHARS = 120_000
REPLAY_ELISION_PREFIX = "[Aether replay elision:"

TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and folders in a directory (relative to project root unless absolute).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path"},
                    "recursive": {"type": "boolean", "default": False},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file. Optionally slice by line range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create a NEW text file. If the path already exists this is rejected, use "
                "edit_file to change it or append_file to add to it. A file is written once, "
                "when it is created, and edited from then on. Python, JavaScript, JSON and "
                "HTML are syntax-checked automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {
                        "type": "string",
                        "maxLength": 12000,
                        "description": "Complete file content, at most 12,000 characters.",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": (
                            "Replace an existing file. Only when the user explicitly asked to "
                            "rewrite or replace that file from scratch, never to apply edits."
                        ),
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": (
                "Add one coherent chunk to an existing text file without resending its earlier "
                "contents. Omit before to append at EOF. For a closed HTML document, set before "
                "to a closing marker such as </script>, </style>, or </body> so the chunk remains "
                "inside the document. Keep each chunk comfortably below the generation limit, "
                "then call validate_file after the final chunk."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {
                        "type": "string",
                        "maxLength": 12000,
                        "description": "One coherent source chunk, at most 12,000 characters.",
                    },
                    "before": {
                        "type": "string",
                        "description": (
                            "Optional marker. Insert immediately before its final occurrence "
                            "instead of appending at EOF."
                        ),
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Change an existing text file. PREFERRED: anchored edits. Give find (text "
                "copied verbatim from the file, including indentation) and replace. find must "
                "match exactly one place, so include enough surrounding lines to be unique. "
                "Anchored edits survive each other, so several can be batched and none of them "
                "go stale as the file changes. Line ranges (start_line/end_line) still work but "
                "refer to the file as it is right now, so every applied edit shifts the lines "
                "below it and forces a reread. Do not mix the two forms in one call. The result "
                "is syntax-checked before it replaces the original."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "find": {
                        "type": "string",
                        "description": "Exact text to locate, copied byte for byte from the file.",
                    },
                    "replace": {"type": "string", "description": "Text to put in its place."},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                    "content": {"type": "string"},
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "find": {"type": "string"},
                                "replace": {"type": "string"},
                                "start_line": {"type": "integer", "minimum": 1},
                                "end_line": {"type": "integer", "minimum": 1},
                                "content": {"type": "string"},
                            },
                        },
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for a regex/text pattern in project files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "glob": {"type": "string", "description": "e.g. **/*.py"},
                    "path": {"type": "string", "description": "Subfolder to search"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a shell command inside the project root. Prefer non-interactive commands.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_sec": {"type": "integer", "default": 60},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_toolchains",
            "description": (
                "List which language toolchains are installed (Python, Node/TypeScript, Java, "
                "C/C++, Rust, Go, .NET, PHP, Ruby) and how to obtain missing ones. Use this for "
                "projects that require a compiler or external runtime; self-contained HTML and "
                "built-in validate_file checks do not need a separate toolchain probe."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_file",
            "description": (
                "Run a non-destructive syntax check on a Python, JavaScript, JSON, or HTML file. "
                "Use this instead of run_shell when verifying a file you just wrote."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Show git status / recent log for the project if it is a repo.",
            "parameters": {"type": "object", "properties": {}},
        },
    },

    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Save a lasting memory note about the user or project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "scope": {"type": "string", "enum": ["global", "project"]},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_write",
            "description": (
                "Create or update the session task list (Cursor-style). "
                "Use for multi-step jobs (≈3+ steps). Plan distinct tool phases, not separate "
                "features that will all be implemented inside one file-write call. Keep exactly "
                "one item in_progress. Complete at most one milestone per update. "
                "When a step is done, call todo_write with merge=true and status=completed for that id "
                "BEFORE starting the next step or giving the final answer. "
                "Never recreate the same open plan with merge=false; that does not mark work done. "
                "merge=true upserts by id and is what you want for almost every call, including "
                "adding a step to an existing plan. merge=false replaces the whole list and "
                "discards every status in it, so use it only for a genuinely different plan."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "merge": {
                        "type": "boolean",
                        "description": "If true, upsert by id into the existing list. If false, replace the entire list.",
                    },
                    "todos": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "content": {"type": "string", "description": "Imperative task, e.g. Fix auth bug"},
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed", "cancelled"],
                                },
                                "activeForm": {
                                    "type": "string",
                                    "description": "Present-continuous label while in_progress, e.g. Fixing auth bug",
                                },
                            },
                            "required": ["id", "content", "status"],
                        },
                    },
                },
                "required": ["merge", "todos"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Ask the user a decision you genuinely cannot make yourself, and WAIT. "
                "Use it only when guessing wrong would waste or destroy real work, or when "
                "the answer is something only they can know (a credential, an external "
                "constraint, which of two existing files to change). "
                "Do NOT use it for taste, naming, layout, styling, difficulty, scope of "
                "flourish, or anything you can reasonably infer from the request. Decide, "
                "and say what you assumed in your debrief. "
                "Ask at most once, before you start building. Once you have begun writing "
                "files it is too late: finish on your best judgement instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 4,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "Short stable id"},
                                "question": {"type": "string", "description": "The full question"},
                                "header": {
                                    "type": "string",
                                    "description": "Very short chip label, max 12 chars (e.g. Scope, Library)",
                                },
                                "multi_select": {"type": "boolean", "default": False},
                                "options": {
                                    "type": "array",
                                    "minItems": 2,
                                    "maxItems": 4,
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "label": {"type": "string", "description": "Concise choice, 1-5 words"},
                                            "description": {"type": "string", "description": "What picking this means"},
                                        },
                                        "required": ["label"],
                                    },
                                },
                            },
                            "required": ["question", "options"],
                        },
                    },
                },
                "required": ["questions"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate",
            "description": (
                "Run a focused sub-task in a FRESH context and get back only its "
                "findings. The sub-task sees none of this conversation, so `task` "
                "must be completely self-contained. Use this when answering a "
                "question would mean reading a lot of material you do not need to "
                "keep: surveying an unfamiliar area of a codebase, tracing where "
                "something is used across many files, digging through a long log. "
                "Its reading costs you nothing but the summary it returns. "
                "Do not use it for work you can do in two or three tool calls, and "
                "do not use it to make edits: it reports, you decide."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "Self-contained instructions. Name the paths or areas to "
                            "look at and state exactly what to report back. The "
                            "sub-agent cannot see your conversation, your plan, or "
                            "anything you have already read."
                        ),
                    },
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional tool allowlist for the sub-task. Defaults to "
                            "read-only inspection tools."
                        ),
                    },
                },
                "required": ["task"],
            },
        },
    },
]

# A sub-agent inspects and reports, never mutates. Read-only means a delegation
# can always be re-run and the parent stays the only thing that writes.
SUBAGENT_DEFAULT_TOOLS = ("list_dir", "read_file", "search_files", "run_shell", "git_status")
# Bounded so a confused sub-task cannot burn the run. It ends the sub-run
# cleanly rather than refusing the model's next move.
SUBAGENT_MAX_STEPS = 16
# Gated on project size. The schema costs 324 tokens on every request, and on a
# small project that is a permanent tax for a tool the model never reaches for:
# there is no context pressure a fresh window would relieve.
DELEGATE_MIN_FILES = 300
_large_project_cache: dict[str, bool] = {}


def project_is_large(project_root: Path | None) -> bool:
    """Whether a project is big enough that delegation earns its schema.

    Stops counting at the threshold, so the walk is bounded, and caches per root
    for the process, since this is consulted several times per agent step.
    """
    if project_root is None:
        return False
    key = str(project_root)
    cached = _large_project_cache.get(key)
    if cached is not None:
        return cached
    count = 0
    try:
        for path in project_root.rglob("*"):
            if path.is_file() and not _is_ignored(path, project_root):
                count += 1
                if count >= DELEGATE_MIN_FILES:
                    break
    except OSError:
        count = 0
    result = count >= DELEGATE_MIN_FILES
    _large_project_cache[key] = result
    return result


_TOOL_DEFS_NO_DELEGATE = [t for t in TOOL_DEFS if t["function"]["name"] != "delegate"]


def tool_defs_for(chat: dict | None) -> list[dict]:
    """Tool schemas for this run. Sub-agents get a restricted, non-recursive set."""
    # Presence of the key marks a sub-run, not its truthiness. An empty spec is
    # falsy, and reading that as "not a sub-agent" hands the child delegate.
    if not isinstance(chat, dict) or "subagent" not in chat:
        # Set once per run by the loop, the only place that knows the project
        # root. Absent means gated off, which is the safe default.
        return TOOL_DEFS if (chat or {}).get("delegate_available") else _TOOL_DEFS_NO_DELEGATE
    spec = chat.get("subagent") or {}
    allowed = {str(t) for t in (spec.get("tools") or SUBAGENT_DEFAULT_TOOLS)}
    # Never recursive: a sub-agent that can delegate can spawn without bound.
    allowed.discard("delegate")
    picked = [t for t in TOOL_DEFS if t["function"]["name"] in allowed]
    # An empty allowlist would leave the sub-agent unable to do anything at all.
    return picked or [
        t for t in TOOL_DEFS if t["function"]["name"] in SUBAGENT_DEFAULT_TOOLS
    ]


# Never the answer to "where is my code". Scanning them floods results with
# dependency noise and makes every failed path lookup crawl thousands of files.
IGNORE_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea", ".vscode",
    "site-packages", "dist-info", "build", "dist", ".next", ".cache",
    # ML project noise: HF caches, checkpoints and vendored C++ trees swamp every
    # search in a training repo.
    "blobs", "snapshots", "checkpoints", ".ollama", "wandb",
})

# Binary-ish payloads that are technically decodable as text and so slip past the
# null-byte check. Tokenizer vocabs and arrow caches give meaningless hits.
BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".gguf", ".pt", ".pth",
    ".safetensors", ".onnx", ".arrow", ".model", ".npy", ".npz",
    ".h5", ".ckpt", ".pack", ".idx", ".zip", ".tar", ".gz", ".7z",
})


def _is_ignored(path: Path, base: Path) -> bool:
    try:
        parts = path.relative_to(base).parts
    except ValueError:
        parts = path.parts
    for part in parts:
        if part in IGNORE_DIRS:
            return True
        # HuggingFace cache dirs: models--org--name/
        if part.startswith("models--"):
            return True
    return False


def _allowed_roots(project_root: Path | None) -> list[Path]:
    from config import UPLOADS

    settings = load_settings()
    roots = [Path(r).resolve() for r in settings.get("allowed_roots", [])]
    if project_root:
        roots.insert(0, project_root.resolve())
    # Always allow reading user uploads / attachments, and offloaded tool output
    # A pointer the model cannot follow is worse than a truncated result.
    try:
        roots.append(UPLOADS.resolve())
    except Exception:
        pass
    try:
        roots.append(TOOLOUT_DIR.resolve())
    except Exception:
        pass
    return roots


def resolve_path(path: str, project_root: Path | None) -> Path:
    raw = Path(path or ".").expanduser()
    if not raw.is_absolute():
        # Relative paths hang off the project when there is one; otherwise the home
        # directory, so the agent is still usable with no project attached.
        base = project_root or Path.home()
        candidate = (base / raw).resolve()
    else:
        candidate = raw.resolve()
    if load_settings().get("unrestricted_fs"):
        return candidate
    allowed = _allowed_roots(project_root)
    for root in allowed:
        try:
            candidate.relative_to(root)
            return candidate
        except ValueError:
            continue
    raise PermissionError(
        f"Path not in allowed roots: {candidate}. "
        "Enable 'Unrestricted file access' in Settings, or add the folder to allowed roots."
    )


def _path_suggestions(rel: str, project_root: Path, limit: int = 6) -> list[str]:
    """Find likely paths when the model omits a parent folder (e.g. reedus_llm/)."""
    if not project_root or not rel:
        return []
    needle = rel.strip().lstrip("./")
    if not needle or ".." in Path(needle).parts:
        return []
    hits: list[str] = []
    scanned = 0
    # Exact suffix match first: **/reedus_companion/scripts/foo.py
    for p in project_root.rglob(Path(needle).name):
        # Hard cap: an unbounded rglob over a repo with a .venv takes seconds and
        # returns nothing useful, and this fires on EVERY missed path.
        scanned += 1
        if scanned > 20000:
            break
        if _is_ignored(p, project_root):
            continue
        try:
            if not p.exists():
                continue
            rp = str(p.relative_to(project_root)).replace("\\", "/")
            if rp == needle or rp.endswith("/" + needle) or rp.endswith(needle):
                hits.append(rp)
        except ValueError:
            continue
        if len(hits) >= 40:
            break
    # Prefer shorter / closer matches
    hits = sorted(set(hits), key=lambda s: (len(s), s))
    return hits[:limit]


def resolve_existing(path: str, project_root: Path | None, *, want_dir: bool | None = None) -> tuple[Path | None, str | None]:
    """Resolve a path; if missing, try unique project-relative fuzzy match.

    Returns (path, error_json_or_None).
    """
    try:
        candidate = resolve_path(path, project_root)
    except PermissionError as e:
        return None, json.dumps({"error": str(e)})
    if candidate.exists():
        if want_dir is True and not candidate.is_dir():
            return None, json.dumps({"error": "Not a directory", "path": str(candidate)})
        if want_dir is False and not candidate.is_file():
            return None, json.dumps({"error": "Not a file", "path": str(candidate)})
        return candidate, None
    # Fuzzy: model often uses paths relative to a subfolder while project root is parent
    if project_root and not Path(path or "").is_absolute():
        suggestions = _path_suggestions(str(path), project_root)
        filtered = []
        for s in suggestions:
            p = (project_root / s).resolve()
            if want_dir is True and not p.is_dir():
                continue
            if want_dir is False and not p.is_file():
                continue
            filtered.append(s)
        if len(filtered) == 1:
            return (project_root / filtered[0]).resolve(), None
        if filtered:
            return None, json.dumps({
                "error": "Not found",
                "tried": str(candidate),
                "suggestions": filtered,
                "hint": "Paths are relative to the project root. Use one of the suggestions.",
            })
    return None, json.dumps({
        "error": "Not found" if want_dir is not False else "Not a file",
        "tried": str(candidate),
        "hint": "Paths are relative to the project root (see Active project Root).",
    })


def run_tool(
    name: str,
    args: dict,
    project_root: Path | None,
    project_id: str | None = None,
    chat: dict | None = None,
) -> str:
    try:
        if name == "list_dir":
            return _list_dir(args, project_root)
        if name == "read_file":
            return _read_file(args, project_root)
        if name == "write_file":
            return _write_file(args, project_root)
        if name == "append_file":
            return _append_file(args, project_root)
        if name == "edit_file":
            return _edit_file(args, project_root)
        if name == "search_files":
            return _search_files(args, project_root)
        if name == "run_shell":
            return _run_shell(args, project_root)
        if name == "check_toolchains":
            return _check_toolchains()
        if name == "validate_file":
            return _validate_file(args, project_root)
        if name == "git_status":
            return _git_status(project_root)
        if name == "remember":
            return _remember(args, project_id)
        if name == "todo_write":
            return _todo_write(args, chat)
        return json.dumps({"error": f"Unknown tool: {name}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


_TODO_STATUSES = frozenset({"pending", "in_progress", "completed", "cancelled"})


def _normalize_todo(raw: dict, allow_partial: bool = False) -> dict | None:
    """Normalize one todo. allow_partial permits a status-only merge update.

    Requiring content on every update rejected the obvious way to close a
    milestone, {"id": "copy-mp3", "status": "completed"}, and the model then
    burned steps guessing at the shape.
    """
    if not isinstance(raw, dict):
        return None
    tid = str(raw.get("id") or "").strip()
    content = str(raw.get("content") or "").strip()
    status = str(raw.get("status") or "pending").strip().lower()
    if status == "in-progress":
        status = "in_progress"
    if not tid or status not in _TODO_STATUSES:
        return None
    if not content:
        if not allow_partial:
            return None
        item = {"id": tid, "status": status}
        active = raw.get("activeForm") or raw.get("active_form")
        if active:
            item["activeForm"] = str(active).strip()
        return item
    item = {"id": tid, "content": content, "status": status}
    active = raw.get("activeForm") or raw.get("active_form")
    if active:
        item["activeForm"] = str(active).strip()
    return item


def _todo_match_key(item: dict) -> str:
    """Loose identity for a todo across a plan rewrite (ids get regenerated)."""
    return re.sub(r"[^a-z0-9]+", "", str(item.get("content") or "").lower())


def carry_forward_completions(old: list[dict], new_todos: list[dict]) -> list[str]:
    """Re-apply terminal statuses that a merge=false rewrite silently dropped.

    Qwen re-emits the whole plan mid-run, usually to expand the step it is about
    to start, and regenerates every item as "pending" because it does not track
    its own statuses. merge=false replaced the list wholesale, so a run that had
    finished four milestones and was partway through the fifth came back reading
    zero of six and started the job over.

    A deliberate reopen still works. A rewrite that keeps ANY previously finished
    item finished is treated as the model knowing the state, and is left exactly
    as sent; only a rewrite that has forgotten all of them is repaired. Matching
    is by id first, then by normalized content, because a regenerated plan often
    keeps the wording and changes the ids.
    """
    finished_by_id = {
        t["id"]: t["status"]
        for t in old
        if t.get("id") and t.get("status") in ("completed", "cancelled")
    }
    if not finished_by_id:
        return []
    finished_by_content: dict[str, str] = {}
    for t in old:
        if t.get("status") in ("completed", "cancelled"):
            key = _todo_match_key(t)
            if key:
                finished_by_content.setdefault(key, t["status"])

    def prior_status(item: dict) -> str | None:
        by_id = finished_by_id.get(item.get("id"))
        if by_id:
            return by_id
        return finished_by_content.get(_todo_match_key(item))

    if any(
        t.get("status") in ("completed", "cancelled") and prior_status(t)
        for t in new_todos
    ):
        return []

    restored = []
    for t in new_todos:
        prior = prior_status(t)
        if prior and t.get("status") in ("pending", "in_progress"):
            t["status"] = prior
            restored.append(t.get("id"))
    return restored


def todos_progress(todos: list[dict] | None) -> dict:
    items = list(todos or [])
    total = len(items)
    done = sum(1 for t in items if t.get("status") in ("completed", "cancelled"))
    open_n = sum(1 for t in items if t.get("status") in ("pending", "in_progress"))
    active = next((t for t in items if t.get("status") == "in_progress"), None)
    pct = int(round(100 * done / total)) if total else 0
    return {
        "todos": items,
        "total": total,
        "done": done,
        "open": open_n,
        "pct": pct,
        "active": active,
    }


def todos_incomplete(chat: dict | None) -> bool:
    return todos_progress((chat or {}).get("agent_todos")).get("open", 0) > 0


_GENERIC_TODO_IDS = frozenset({
    "build", "test", "tests", "verify", "validation", "wire", "wiring",
    "finish", "done", "implement", "implementation", "inspect", "review",
})


def todos_fingerprint(todos: list[dict] | None) -> str:
    """Stable signature of open todo ids and status, for detecting stalled plans."""
    items = []
    for t in todos or []:
        st = t.get("status") or "pending"
        if st in ("pending", "in_progress"):
            items.append(f"{t.get('id')}:{st}")
    return "|".join(items)


def complete_open_todos(chat: dict | None, reason: str = "auto") -> dict:
    """Mark every open todo completed. Used when the model finished but forgot to check off."""
    if chat is None:
        return todos_progress([])
    items = list(chat.get("agent_todos") or [])
    for t in items:
        if t.get("status") in ("pending", "in_progress"):
            t["status"] = "completed"
    chat["agent_todos"] = items
    prog = todos_progress(items)
    prog["auto_completed"] = reason
    return prog


def todos_prompt_block(chat: dict | None) -> str:
    todos = (chat or {}).get("agent_todos") or []
    if not todos:
        return ""
    lines = [
        "# Agent plan (live)",
        "When a step is finished, todo_write merge=true with status=completed for that id. "
        "Do not stop while open items remain, and do not recreate the same open plan.",
    ]
    for t in todos:
        st = t.get("status") or "pending"
        mark = {
            "completed": "[x]",
            "cancelled": "[-]",
            "in_progress": "[>]",
            "pending": "[ ]",
        }.get(st, "[ ]")
        label = t.get("activeForm") if st == "in_progress" and t.get("activeForm") else t.get("content")
        lines.append(f"- {mark} {t.get('id')}: {label}")
    return "\n".join(lines)


def _todo_write(args: dict, chat: dict | None) -> str:
    if chat is None:
        return json.dumps({"error": "No active chat for todos"})
    incoming = args.get("todos") or []
    if not isinstance(incoming, list) or not incoming:
        return json.dumps({"error": "todos must be a non-empty array"})
    merge = bool(args.get("merge"))
    old = list(chat.get("agent_todos") or [])
    known_ids = {t.get("id") for t in old if t.get("id")}
    normalized: list[dict] = []
    for raw in incoming:
        # A merge update to a todo that already exists may carry status alone.
        partial = merge and isinstance(raw, dict) and str(
            raw.get("id") or ""
        ).strip() in known_ids
        item = _normalize_todo(raw, allow_partial=partial)
        if item:
            normalized.append(item)
    if not normalized:
        return json.dumps({
            "error": "No valid todos",
            "hint": (
                "A new todo needs id, content and status. Updating an existing one with "
                "merge=true needs only id and status."
            ),
        })

    old_fp = todos_fingerprint(old)
    old_open = todos_progress(old).get("open", 0)
    if merge:
        by_id = {t["id"]: dict(t) for t in old if t.get("id")}
        order = [t["id"] for t in old if t.get("id")]
        for item in normalized:
            tid = item["id"]
            if tid in by_id:
                by_id[tid].update(item)
            else:
                by_id[tid] = item
                order.append(tid)
            if tid not in order:
                order.append(tid)
        new_todos = [by_id[i] for i in order if i in by_id]
        restored_ids: list[str] = []
    else:
        new_todos = normalized
        restored_ids = carry_forward_completions(old, new_todos)

    old_by_id = {t.get("id"): t for t in old if t.get("id")}
    partial_note: dict | None = None
    if merge and old_by_id:
        newly_finished = [
            t
            for t in new_todos
            if t.get("status") in ("completed", "cancelled")
            and (old_by_id.get(t.get("id")) or {}).get("status")
            in ("pending", "in_progress")
        ]
        if len(newly_finished) > 1:
            active_id = next(
                (t.get("id") for t in old if t.get("status") == "in_progress"),
                None,
            )
            attempted = [t.get("id") for t in newly_finished]
            if active_id in attempted:
                # Apply the active milestone and defer the rest instead of
                # rejecting the call. A rejection advances nothing, so a model
                # closing out its plan could never reach open=0. Partial
                # acceptance moves the plan forward by exactly one milestone.
                deferred = [tid for tid in attempted if tid != active_id]
                deferred_set = set(deferred)
                for t in new_todos:
                    if t.get("id") in deferred_set:
                        t["status"] = (old_by_id.get(t.get("id")) or {}).get(
                            "status", "pending"
                        )
                partial_note = {
                    "completed_now": active_id,
                    "deferred": deferred,
                    "why": (
                        "One milestone completes per call. Verify each deferred item's work "
                        "actually landed, then complete it on its own call."
                    ),
                }
            else:
                return json.dumps({
                    "error": "Complete only one plan milestone per todo_write call",
                    "changed": False,
                    "attempted_ids": attempted,
                    "todos": old,
                    "send_exactly": {
                        "merge": True,
                        "todos": [{"id": active_id or attempted[0], "status": "completed"}],
                    },
                    "hint": (
                        f"None of these is the active milestone ({active_id!r}). Complete that "
                        "one first; Aether promotes the next pending item automatically."
                    ),
                })
        old_active = next(
            (t for t in old if t.get("status") == "in_progress"),
            None,
        )
        if (
            newly_finished
            and old_active
            and newly_finished[0].get("id") != old_active.get("id")
        ):
            return json.dumps({
                "error": "Complete the current in_progress milestone before a pending one",
                "changed": False,
                "active_id": old_active.get("id"),
                "attempted_id": newly_finished[0].get("id"),
                "todos": old,
                "send_exactly": {
                    "merge": True,
                    "todos": [{"id": old_active.get("id"), "status": "completed"}],
                },
                "hint": (
                    f"The plan is ordered: {old_active.get('id')!r} is in progress and must be "
                    f"completed before {newly_finished[0].get('id')!r}. If its work is already "
                    "done, send the send_exactly call above."
                ),
            })

    # Prefer a single in_progress item (Cursor rule). Keep first; demote extras to pending.
    seen_ip = False
    for t in new_todos:
        if t.get("status") == "in_progress":
            if seen_ip:
                t["status"] = "pending"
            else:
                seen_ip = True
    if not seen_ip:
        next_pending = next(
            (t for t in new_todos if t.get("status") == "pending"),
            None,
        )
        if next_pending:
            next_pending["status"] = "in_progress"

    chat["agent_todos"] = new_todos
    prog = todos_progress(new_todos)
    new_fp = todos_fingerprint(new_todos)
    stalled = prog["open"] > 0 and new_fp == old_fp and old_open > 0
    instructions = (
        "Todos updated. Keep exactly one item in_progress. "
        "Mark completed immediately after each distinct tool phase with merge=true; "
        "complete only the current milestone and do not batch later milestones. "
        "Continue until open=0, then give the final answer."
    )
    if stalled:
        instructions = (
            "WARNING: Plan did not advance. Same open items as before. "
            "This does NOT mark work done. If the step (or whole task) is finished, call "
            'todo_write with merge=true and status="completed" for those ids, e.g. '
            '{"merge":true,"todos":[{"id":"STEP_ID","content":"...","status":"completed"}]}. '
            "Do not call merge=false to recreate the same in_progress plan."
        )
    elif prog["open"] == 0:
        instructions = "All todos completed (open=0). Give the final answer now. Do not restart the plan."
    if restored_ids:
        instructions = (
            f"This merge=false rewrite dropped {len(restored_ids)} milestone(s) you had already "
            f"finished ({', '.join(str(i) for i in restored_ids)}); their completed status was "
            "restored. That work is done, do not redo it. Resume from the item now in_progress. "
            "Use merge=true to change statuses; merge=false is only for a genuinely new plan. "
        ) + instructions
    return json.dumps(
        {
            "ok": True,
            "merge": merge,
            "restored": restored_ids,
            "todos": new_todos,
            "done": prog["done"],
            "total": prog["total"],
            "open": prog["open"],
            "pct": prog["pct"],
            "stalled": stalled,
            "partial": partial_note,
            "instructions": instructions,
        },
        indent=2,
    )


def _list_dir(args: dict, project_root: Path | None) -> str:
    path, err = resolve_existing(args.get("path") or ".", project_root, want_dir=True)
    if err:
        return err
    assert path is not None
    recursive = bool(args.get("recursive"))
    items = []
    if recursive:
        for p in path.rglob("*"):
            if any(part.startswith(".") for part in p.parts):
                continue
            if _is_ignored(p, path):
                continue
            if p.is_file():
                items.append(str(p.relative_to(path)))
            if len(items) >= 400:
                items.append("…truncated…")
                break
    else:
        for p in sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if p.name.startswith("."):
                continue
            items.append(("📁 " if p.is_dir() else "📄 ") + p.name)
    return json.dumps({"path": str(path), "entries": items}, indent=2)


def _read_file(args: dict, project_root: Path | None) -> str:
    path, err = resolve_existing(args["path"], project_root, want_dir=False)
    if err:
        return err
    assert path is not None
    if path.stat().st_size > 2_000_000:
        return json.dumps({"error": "File too large (>2MB)", "path": str(path), "bytes": path.stat().st_size})
    raw = path.read_bytes()
    if b"\x00" in raw[:8192]:
        return json.dumps({
            "path": str(path),
            "binary": True,
            "bytes": len(raw),
            "hint": "Binary file, contents not shown as text. Use run_shell (e.g. file, strings) if needed.",
        })
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    start = max(1, int(args.get("start_line") or 1))
    end = int(args.get("end_line") or len(lines))
    end = min(end, len(lines))
    sliced = lines[start - 1 : end]
    rendered: list[str] = []
    rendered_chars = 0
    partial_line = False
    for offset, line in enumerate(sliced):
        numbered_line = f"{offset + start}|{line}"
        added = len(numbered_line) + (1 if rendered else 0)
        if rendered and rendered_chars + added > READ_FILE_CONTENT_MAX_CHARS:
            break
        # A single minified line cannot be safely addressed with line edits, but
        # still return a bounded diagnostic instead of flooding prompt storage.
        if not rendered and added > READ_FILE_CONTENT_MAX_CHARS:
            numbered_line = numbered_line[:READ_FILE_CONTENT_MAX_CHARS]
            rendered.append(numbered_line)
            rendered_chars = len(numbered_line)
            partial_line = True
            break
        rendered.append(numbered_line)
        rendered_chars += added
    numbered = "\n".join(rendered)
    actual_end = start + len(rendered) - 1 if rendered else end
    result = {
        "path": str(path),
        "start": start,
        "end": actual_end,
        "content": numbered,
        # Lets the controller safely reuse read coverage on later agent turns.
        # Size alone is not sufficient: an in-place edit can keep it unchanged.
        "revision": _file_revision(path),
    }
    if partial_line:
        result["truncated"] = True
        result["line_truncated"] = True
        result["hint"] = (
            f"Line {start} alone exceeds the output cap. Use search_files or run_shell "
            "to inspect this minified/generated file."
        )
    elif actual_end < end:
        result["truncated"] = True
        result["requested_end"] = end
        result["hint"] = f"Output capped at line {actual_end}; continue at line {actual_end + 1}."
    return json.dumps(result)


# A write_file that replaces a substantial file with less content is almost
# always a regeneration that drops whatever the model did not reproduce. One
# such call destroyed ~10,000 characters of working code.
OVERWRITE_MIN_EXISTING_CHARS = 4000
OVERWRITE_SHRINK_RATIO = 0.9


def _overwrite_guard(path: Path, content: str, overwrite: bool) -> dict | None:
    """write_file creates files. Changing one that exists is edit_file's job.

    A file is written once, when it is created, and edited from then on. Letting
    write_file replace an existing file means every call is a chance to silently
    drop whatever the model did not retype, which is how a 687-line
    game became 16k chars of unrelated code.
    """
    try:
        if not path.is_file():
            return None
        existing = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not existing.strip():
        return None
    lines = existing.count("\n") + 1
    if not overwrite:
        return {
            "error": "File already exists, use edit_file not write_file",
            "path": str(path),
            "changed": False,
            "existing_chars": len(existing),
            "existing_lines": lines,
            "hint": (
                f"{path.name} already has {lines} lines. write_file is for creating a file; "
                "changing one is edit_file with find/replace anchors, or append_file to add "
                "to the end. Rewriting the whole file loses everything you do not retype. "
                "Only if the user explicitly asked you to replace this file from scratch, "
                "call write_file again with overwrite=true."
            ),
        }
    # Even an explicit rewrite does not get to quietly delete most of the file.
    if (
        len(existing) >= OVERWRITE_MIN_EXISTING_CHARS
        and len(content) < len(existing) * OVERWRITE_SHRINK_RATIO
    ):
        return {
            "error": "Refusing to shrink an existing file, even with overwrite",
            "path": str(path),
            "changed": False,
            "existing_chars": len(existing),
            "existing_lines": lines,
            "submitted_chars": len(content),
            "hint": (
                f"This would drop about {len(existing) - len(content)} characters. If the user "
                "really asked for a smaller replacement, confirm it with ask_user first, "
                "quoting the current size. Otherwise use edit_file."
            ),
        }
    return None


def offload_tool_output(name: str, content: str, chat_id: str, seq: int) -> str | None:
    """Write an oversized tool result to disk, returning a pointer payload.

    The reference behaviour from Claude Code and Cursor: keep the head and tail
    in context, put the whole thing on the filesystem, and let the agent read the
    range it actually wants. Truncation is lossy in the worst place, since the
    tail is usually what the model needs next, and a one-line receipt was lossy
    everywhere.

    Returns None when the result is small enough to send verbatim, so the common
    case costs one len() check.
    """
    if len(content or "") <= OFFLOAD_CHARS:
        return None

    def _write(body: str, field: str = "") -> str | None:
        try:
            out_dir = TOOLOUT_DIR / (chat_id or "unknown")
            out_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^a-zA-Z0-9_-]", "", name) or "tool"
            suffix = f"-{re.sub(r'[^a-zA-Z0-9_-]', '', field)}" if field else ""
            path = out_dir / f"{seq:04d}-{safe}{suffix}.txt"
            path.write_text(body, encoding="utf-8")
            # Bound growth: a long run can produce hundreds of these.
            for old in sorted(out_dir.iterdir(), key=lambda f: f.name)[:-TOOLOUT_KEEP]:
                old.unlink(missing_ok=True)
            return str(path)
        except OSError:
            # Offloading must never be the reason a tool call fails; the caller
            # falls back to sending (and truncating) the result as usual.
            return None

    def _pointer(path: str, body: str) -> dict:
        return {
            "path": path,
            "chars": len(body),
            "lines": body.count("\n") + 1,
            "note": (
                "This output was large. The head and tail are shown here and the "
                "complete text was saved to `path`, nothing was lost. To see a "
                "part that is not shown, call read_file on that path with a line "
                "range."
            ),
        }

    # Keep the envelope and offload only the big payload field. Offloading the
    # whole JSON hands the model the head and tail of truncated JSON.
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict):
        # Every oversized field, not just the biggest: run_shell returns both
        # stdout and stderr, and either can be at the full capture cap.
        offloaded: dict[str, dict] = {}
        for key in ("content", "stdout", "stderr", "output", "text", "matches"):
            body = payload.get(key)
            if not isinstance(body, str):
                continue
            if len(body) <= OFFLOAD_HEAD_CHARS + OFFLOAD_TAIL_CHARS:
                continue
            path = _write(body, key)
            if path is None:
                return None
            payload[key] = (
                body[:OFFLOAD_HEAD_CHARS]
                + "\n…[middle saved to the offload file]…\n"
                + body[-OFFLOAD_TAIL_CHARS:]
            )
            offloaded[key] = _pointer(path, body)
        if offloaded:
            payload["offloaded"] = offloaded
            return json.dumps(payload)

    path = _write(content)
    if path is None:
        return None
    return json.dumps({
        "offloaded": _pointer(path, content),
        "tool": name,
        "head": content[:OFFLOAD_HEAD_CHARS],
        "tail": content[-OFFLOAD_TAIL_CHARS:],
    })


def _snapshot_before_write(path: Path) -> None:
    """Keep the previous contents of a file before a tool overwrites it.

    Guards catch the destructive shapes we know about. A backup catches the ones
    we do not: recovering the game destroyed by one bad write meant stitching it
    out of chat transcripts, which only worked because the reads happened to
    cover every line.
    """
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return
        backups = BACKUP_DIR / path.name
        backups.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        # Hash, not size: two edits in the same second that happen to produce the
        # same length are different versions and both are worth keeping.
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:8]
        target = backups / f"{stamp}-{digest}{path.suffix or '.bak'}"
        if target.exists():
            return
        shutil.copy2(path, target)
        keep = sorted(backups.iterdir(), key=lambda f: f.name)[-BACKUP_KEEP:]
        for stale in backups.iterdir():
            if stale not in keep:
                stale.unlink(missing_ok=True)
    except OSError:
        # A backup must never be the reason an edit fails.
        return


def _write_file(args: dict, project_root: Path | None) -> str:
    path = resolve_path(args["path"], project_root)
    content = args.get("content") or ""
    if REPLAY_ELISION_PREFIX in content:
        return json.dumps({
            "error": "Refusing to write an internal replay-elision marker",
            "path": str(path),
            "changed": False,
            "hint": "Use read_file to inspect the current file, then send only real source text.",
        })
    blocked = _overwrite_guard(path, content, bool(args.get("overwrite")))
    if blocked:
        return json.dumps(blocked)
    path.parent.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "ok": True,
        "path": str(path),
        "bytes": len(content.encode("utf-8")),
    }
    validated_suffixes = {".py", ".js", ".mjs", ".cjs", ".json", ".html", ".htm"}
    if path.suffix.lower() in validated_suffixes:
        candidate: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=f".{path.stem}.aether-write-",
                suffix=path.suffix,
                delete=False,
            ) as handle:
                handle.write(content)
                candidate = Path(handle.name)
            validation = json.loads(_validate_file({"path": str(candidate)}, project_root))
            validation["path"] = str(path)
            # The check ran on a temp copy; naming it sends the model looking for
            # a file it cannot open.
            if isinstance(validation.get("error"), str):
                validation["error"] = validation["error"].replace(str(candidate), str(path))
            result["validation"] = validation
            if not validation.get("ok"):
                result.update({
                    "ok": False,
                    "changed": False,
                    "error": "Write validation failed; the original file was preserved",
                })
                return json.dumps(result)
            if path.exists():
                candidate.chmod(path.stat().st_mode)
            _snapshot_before_write(path)
            os.replace(candidate, path)
            candidate = None
        finally:
            if candidate:
                candidate.unlink(missing_ok=True)
    else:
        _snapshot_before_write(path)
        path.write_text(content, encoding="utf-8")
    result["changed"] = True
    return json.dumps(result)


def _append_file(args: dict, project_root: Path | None) -> str:
    path, err = resolve_existing(args.get("path") or "", project_root, want_dir=False)
    if err:
        return err
    assert path is not None
    chunk = str(args.get("content") or "")
    if REPLAY_ELISION_PREFIX in chunk:
        return json.dumps({
            "error": "Refusing to write an internal replay-elision marker",
            "path": str(path),
            "changed": False,
            "hint": "Use read_file to inspect the current file, then send only the next real chunk.",
        })
    existing = path.read_text(encoding="utf-8", errors="replace")
    before = str(args.get("before") or "")
    if (
        not before
        and path.suffix.lower() in {".html", ".htm"}
        and "</html>" in existing.lower()
        and chunk.strip()
    ):
        return json.dumps({
            "error": "Refusing to append source after a closed HTML document",
            "path": str(path),
            "changed": False,
            "hint": "Set before to </script>, </style>, or </body> as appropriate.",
        })
    if before:
        insert_at = existing.rfind(before)
        if insert_at < 0:
            return json.dumps({
                "error": "Insertion marker was not found",
                "path": str(path),
                "before": before,
                "changed": False,
                "hint": "Read the end of the file and use an exact closing marker.",
            })
        updated = existing[:insert_at] + chunk + existing[insert_at:]
        mode = "insert"
    else:
        updated = existing + chunk
        mode = "append"
    if len(updated.encode("utf-8")) > 2_000_000:
        return json.dumps({
            "error": "Resulting file is too large (>2MB)",
            "path": str(path),
            "changed": False,
        })
    path.write_text(updated, encoding="utf-8")
    return json.dumps({
        "ok": True,
        "path": str(path),
        "mode": mode,
        "written_bytes": len(chunk.encode("utf-8")),
        "appended_bytes": len(chunk.encode("utf-8")) if mode == "append" else 0,
        "inserted_bytes": len(chunk.encode("utf-8")) if mode == "insert" else 0,
        "total_bytes": len(updated.encode("utf-8")),
        "revision": _file_revision(path),
        "hint": "Chunk write succeeded. Validate the completed file after the final chunk.",
    })


def _commit_edit(
    path: Path, updated_source: str, result: dict, project_root: Path | None
) -> str:
    """Validate a candidate file and swap it in, or keep the original untouched."""
    validated_suffixes = {".py", ".js", ".mjs", ".cjs", ".json", ".html", ".htm"}
    if path.suffix.lower() in validated_suffixes:
        candidate: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=f".{path.stem}.aether-",
                suffix=path.suffix,
                delete=False,
            ) as handle:
                handle.write(updated_source)
                candidate = Path(handle.name)
            validation = json.loads(_validate_file({"path": str(candidate)}, project_root))
            validation["path"] = str(path)
            # The check ran on a temp copy; naming it sends the model looking for
            # a file it cannot open.
            if isinstance(validation.get("error"), str):
                validation["error"] = validation["error"].replace(str(candidate), str(path))
            result["validation"] = validation
            if not validation.get("ok"):
                result.update({
                    "ok": False,
                    "changed": False,
                    "error": "Edit validation failed; the original file was preserved",
                })
                return json.dumps(result)
            candidate.chmod(path.stat().st_mode)
            _snapshot_before_write(path)
            os.replace(candidate, path)
            candidate = None
        finally:
            if candidate:
                candidate.unlink(missing_ok=True)
    else:
        _snapshot_before_write(path)
        path.write_text(updated_source, encoding="utf-8")
    result["changed"] = True
    return json.dumps(result)


def _edit_file_anchored(
    path: Path, source: str, specs: list[dict], project_root: Path | None
) -> str:
    """Apply find/replace edits in order, each against the evolving buffer."""
    updated_source = source
    applied = []
    for index, spec in enumerate(specs):
        find = str(spec.get("find") or "")
        # `replace` absent is not the same as `replace: ""`. Coercing a missing
        # key to empty turns the call into a silent deletion that still reports
        # ok, and later anchors then fail because their code is gone.
        if spec.get("replace") is not None:
            replace = str(spec["replace"])
        elif spec.get("content") is not None:
            # `content` is the line-range form's field. A model that sends
            # find + content plainly means it as the replacement.
            replace = str(spec["content"])
        else:
            return json.dumps({
                "error": "Anchored edit has no replacement text",
                "path": str(path),
                "changed": False,
                "edit_index": index,
                "find_preview": find[:200],
                "hint": (
                    "Send the new text as \"replace\". To delete the anchor on purpose, "
                    "pass \"replace\": \"\" explicitly."
                ),
            })
        if REPLAY_ELISION_PREFIX in replace:
            return json.dumps({
                "error": "Refusing to write an internal replay-elision marker",
                "path": str(path),
                "changed": False,
                "hint": "Use read_file to inspect the current file, then send only real source text.",
            })
        count = updated_source.count(find)
        if count == 0:
            # Refusing without showing what is there costs a reread per attempt,
            # which is what the redundant-read guard blocks. Hand back the
            # closest real lines so the anchor can be fixed in one step.
            probe = next((ln for ln in find.splitlines() if ln.strip()), "")
            nearest = []
            if probe:
                scored = sorted(
                    (
                        (difflib.SequenceMatcher(None, probe, line).ratio(), number, line)
                        for number, line in enumerate(updated_source.splitlines(), 1)
                    ),
                    key=lambda item: (-item[0], item[1]),
                )[:3]
                nearest = [
                    {"line": number, "text": line[:200], "similarity": round(ratio, 2)}
                    for ratio, number, line in scored if ratio >= 0.5
                ]
            return json.dumps({
                "error": "Anchor text not found",
                "path": str(path),
                "changed": False,
                "edit_index": index,
                "find_preview": find[:200],
                "nearest": nearest,
                "hint": (
                    "find must match the file byte for byte, including indentation and line "
                    "breaks. Earlier edits in this same call have already been applied to the "
                    "buffer. 'nearest' shows the closest lines actually in the file right now. "
                    "copy from those rather than rereading."
                    if nearest else
                    "find must match the file byte for byte, including indentation and line "
                    "breaks. Read the exact lines and copy them verbatim. Earlier edits in "
                    "this same call have already been applied to the buffer."
                ),
            })
        if count > 1:
            return json.dumps({
                "error": "Anchor text is not unique",
                "path": str(path),
                "changed": False,
                "edit_index": index,
                "occurrences": count,
                "find_preview": find[:200],
                "hint": (
                    f"This text appears {count} times. Include more surrounding lines so the "
                    "anchor identifies exactly one place."
                ),
            })
        updated_source = updated_source.replace(find, replace, 1)
        applied.append({
            "edit_index": index,
            "removed_lines": len(find.splitlines()),
            "inserted_lines": len(replace.splitlines()),
        })
    if updated_source == source:
        # Every anchor matched and the file is byte-identical, so nothing
        # happened. Reporting ok here resets the stall counters and lets a
        # milestone complete with no real change behind it.
        return json.dumps({
            "error": "Edit changed nothing",
            "path": str(path),
            "changed": False,
            "edits_applied": 0,
            "hint": (
                "Every anchor matched but replace was identical to find, so the file is "
                "unchanged. Send the new text you actually want in place of the anchor."
            ),
        })
    result: dict[str, Any] = {
        "ok": True,
        "path": str(path),
        "mode": "anchored",
        "edits_applied": len(applied),
        "replaced": applied,
        "file_lines": len(updated_source.splitlines()),
    }
    return _commit_edit(path, updated_source, result, project_root)


def _edit_file(args: dict, project_root: Path | None) -> str:
    path, err = resolve_existing(args.get("path") or "", project_root, want_dir=False)
    if err:
        return err
    assert path is not None
    if path.stat().st_size > 2_000_000:
        return json.dumps({"error": "File too large to edit (>2MB)", "path": str(path)})

    source = path.read_text(encoding="utf-8", errors="replace")
    lines = source.splitlines(keepends=True)
    raw_edits = args.get("edits")
    if isinstance(raw_edits, list) and raw_edits:
        specs = raw_edits
    elif args.get("find"):
        specs = [{
            "find": args.get("find"),
            "replace": args.get("replace"),
            "content": args.get("content"),
        }]
    else:
        specs = [{
            "start_line": args.get("start_line"),
            "end_line": args.get("end_line"),
            "content": args.get("content"),
        }]
    if len(specs) > MAX_EDIT_RANGES:
        return json.dumps({
            "error": "Too many edit ranges in one batch",
            "path": str(path),
            "requested": len(specs),
            "maximum": MAX_EDIT_RANGES,
            "hint": (
                f"Split this into coherent batches of at most {MAX_EDIT_RANGES} ranges, "
                "validating each batch before continuing."
            ),
        })
    # Anchored edits. Line numbers go stale the moment an earlier edit lands, so
    # a model has to reread between every change. Matching on surrounding text
    # survives its own edits.
    anchored = [sp for sp in specs if isinstance(sp, dict) and sp.get("find")]
    if anchored:
        if len(anchored) != len(specs):
            return json.dumps({
                "error": "Mixed anchored and line-numbered edits in one call",
                "path": str(path),
                "hint": (
                    "Line ranges refer to the original file while anchored edits apply in "
                    "order, so mixing them is ambiguous. Send one kind per call, prefer find/replace."
                ),
            })
        return _edit_file_anchored(path, source, anchored, project_root)

    total_edit_chars = sum(
        len(str(spec.get("content") or ""))
        for spec in specs
        if isinstance(spec, dict)
    )
    if total_edit_chars > MAX_EDIT_TOTAL_CHARS:
        return json.dumps({
            "error": "Edit batch content is too large",
            "path": str(path),
            "requested_chars": total_edit_chars,
            "maximum_chars": MAX_EDIT_TOTAL_CHARS,
            "changed": False,
            "hint": "Split the edit into smaller coherent phases and validate each phase.",
        })

    normalized = []
    for spec in specs:
        if not isinstance(spec, dict):
            return json.dumps({"error": "Each edit must be an object", "path": str(path)})
        start = int(spec.get("start_line") or 0)
        end = int(spec.get("end_line") or 0)
        if start < 1 or end < start or end > len(lines):
            return json.dumps({
                "error": "Invalid line range",
                "path": str(path),
                "start_line": start,
                "end_line": end,
                "file_lines": len(lines),
                "hint": "Read the current range again; earlier edits may have shifted line numbers.",
            })
        replacement = str(spec.get("content") or "")
        if REPLAY_ELISION_PREFIX in replacement:
            return json.dumps({
                "error": "Refusing to write an internal replay-elision marker",
                "path": str(path),
                "changed": False,
                "hint": "Use read_file to inspect the current file, then send only real source text.",
            })
        selected_had_newline = bool(lines[end - 1].endswith(("\n", "\r")))
        if replacement and selected_had_newline and not replacement.endswith(("\n", "\r")):
            replacement += "\n"
        normalized.append({
            "start": start,
            "end": end,
            "lines": replacement.splitlines(keepends=True),
        })

    ordered = sorted(normalized, key=lambda item: item["start"])
    for previous, current in zip(ordered, ordered[1:]):
        if current["start"] <= previous["end"]:
            return json.dumps({
                "error": "Edit ranges overlap",
                "path": str(path),
                "first": [previous["start"], previous["end"]],
                "second": [current["start"], current["end"]],
            })

    updated = list(lines)
    for edit in reversed(ordered):
        updated[edit["start"] - 1 : edit["end"]] = edit["lines"]
    updated_source = "".join(updated)
    result: dict[str, Any] = {
        "ok": True,
        "path": str(path),
        "edits_applied": len(ordered),
        "replaced": [
            {
                "start_line": edit["start"],
                "end_line": edit["end"],
                "inserted_lines": len(edit["lines"]),
            }
            for edit in ordered
        ],
        "file_lines": len(updated),
    }
    return _commit_edit(path, updated_source, result, project_root)


def _search_files_grep(grep_bin: str, base: Path, pattern: str, glob: str) -> str | None:
    """grep-backed search. Returns None if grep could not run, so caller can fall back."""
    cmd = [grep_bin, "-r", "-I", "-n", "-i"]
    # Prefer PCRE so Python-style patterns (\d, \b, lookarounds) behave the same.
    probe = subprocess.run([grep_bin, "-P", "x", "-q"], input="x", text=True, capture_output=True)
    cmd.append("-P" if probe.returncode in (0, 1) else "-E")
    # --exclude-dir behaves the same across GNU grep, BSD grep and ugrep, so it
    # is the only filter we delegate. --include/--exclude precedence differs
    # between them, so suffix and glob filtering happen in Python below.
    for d in sorted(IGNORE_DIRS):
        cmd.append(f"--exclude-dir={d}")
    cmd.append("--exclude-dir=models--*")
    cmd += ["-e", pattern, str(base)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except Exception:
        return None
    # 0 = matches, 1 = no matches; anything else means grep itself failed.
    if proc.returncode not in (0, 1):
        return None
    from fnmatch import fnmatch
    inc = None
    if glob and glob not in ("**/*", "**"):
        inc = glob[3:] if glob.startswith("**/") else glob
    hits = []
    truncated = False
    skipped = 0
    for line in (proc.stdout or "").splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        fpath, lno, text = parts
        fp = Path(fpath)
        if fp.suffix.lower() in BINARY_SUFFIXES:
            skipped += 1
            continue
        try:
            rel = str(fp.relative_to(base)).replace("\\", "/")
        except ValueError:
            rel = fpath
        if inc and not (fnmatch(rel, glob) or fnmatch(rel, inc) or fnmatch(fp.name, inc)):
            skipped += 1
            continue
        try:
            lineno = int(lno)
        except ValueError:
            continue
        hits.append({"file": rel, "line": lineno, "text": text.strip()[:200]})
        if len(hits) >= 80:
            truncated = True
            break
    out: dict = {"hits": hits}
    if skipped:
        out["skipped_vendor_files"] = skipped
    if truncated:
        out["truncated"] = True
        out["hint"] = "80-hit cap reached, narrow with a stricter pattern, `path`, or `glob`."
    return json.dumps(out, indent=2)


def _search_files(args: dict, project_root: Path | None) -> str:
    base = resolve_path(args.get("path") or ".", project_root)
    pattern = args["pattern"]
    glob = args.get("glob") or "**/*"
    rx = re.compile(pattern, re.IGNORECASE)
    hits = []
    skipped_vendor = 0
    # os.walk with in-place dirnames pruning, not Path.glob: glob descends into
    # node_modules and HF caches before letting us filter, ~38s on a real ML
    # repo. Pruning never enters those trees.
    # Fast path is grep, which does the same work in under a second and skips
    # binaries with -I. Falls back to the Python walk when grep is missing.
    grep_bin = shutil_which("grep")
    if grep_bin:
        got = _search_files_grep(grep_bin, base, pattern, glob)
        if got is not None:
            return got

    deadline = time.monotonic() + 12.0  # soft budget: never stall the agent loop
    matcher = None
    if glob and glob not in ("**/*", "**"):
        from fnmatch import fnmatch as _fnmatch
        pat = glob[3:] if glob.startswith("**/") else glob
        matcher = lambda rel: _fnmatch(rel, glob) or _fnmatch(Path(rel).name, pat)

    for dirpath, dirnames, filenames in os.walk(base):
        here = Path(dirpath)
        # Prune before descending.
        keep = []
        for d in dirnames:
            if d in IGNORE_DIRS or d.startswith("models--") or d.startswith("."):
                skipped_vendor += 1
                continue
            keep.append(d)
        dirnames[:] = keep

        if time.monotonic() > deadline:
            return json.dumps({
                "hits": hits, "truncated": True, "timed_out": True,
                "skipped_vendor_files": skipped_vendor,
                "hint": "Search budget exceeded, narrow it with `path` or a stricter `glob` (e.g. **/*.py).",
            }, indent=2)
        for fn in filenames:
            p = here / fn
            if p.suffix.lower() in BINARY_SUFFIXES:
                skipped_vendor += 1
                continue
            try:
                rel = str(p.relative_to(base)).replace("\\", "/")
            except ValueError:
                continue
            if matcher and not matcher(rel):
                continue
            try:
                if p.stat().st_size > 1_000_000:
                    continue
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append({"file": rel, "line": i, "text": line.strip()[:200]})
                    if len(hits) >= 80:
                        return json.dumps({
                            "hits": hits, "truncated": True,
                            "skipped_vendor_files": skipped_vendor,
                            "hint": "80-hit cap reached, narrow with a stricter pattern or a path/glob.",
                        }, indent=2)
    out = {"hits": hits}
    if skipped_vendor:
        out["skipped_vendor_files"] = skipped_vendor
    return json.dumps(out, indent=2)


# The only operations safe_auto stops. Everything else runs unattended, because
# the point of the mode is an agent that finishes a job.
_DESTRUCTIVE_BINS = frozenset({
    # irreversible data loss
    "rm", "rmdir", "shred", "truncate",
    # disks and filesystems
    "dd", "mkfs", "fdisk", "parted", "mkswap", "wipefs",
    # privilege escalation and accounts
    "sudo", "su", "doas", "passwd", "usermod", "userdel", "groupdel", "visudo",
    # machine state
    "shutdown", "reboot", "halt", "poweroff", "init",
})

# Commands that write where they are pointed. Harmless in a project, not in /etc.
_WRITES_FILES = frozenset({
    "cp", "mv", "tee", "install", "ln", "chmod", "chown", "chgrp", "rsync",
})

# git subcommands that publish work or discard it irreversibly
_GIT_REVIEW = frozenset({"push", "reset", "clean"})

# `curl … | sh` is remote code execution however friendly the URL looks.
_PIPE_TO_SHELL = re.compile(
    r"\b(?:curl|wget|fetch)\b[^|]*\|\s*(?:sudo\s+)?(?:ba|z|k|da)?sh\b"
)

# Commands that run another command, so their arguments are program names too.
_COMMAND_RUNNERS = frozenset({
    "find", "xargs", "sh", "bash", "zsh", "env", "nohup", "time", "watch", "parallel",
})

# Raw text that is dangerous regardless of tokenization.
_REVIEW_PATTERNS = ("> /etc", "> /usr", "> /bin", ">/etc", ">/usr", ">/bin", ":(){")

# Writing into any of these needs a human, whatever the command claims to do.
_SYSTEM_PREFIXES = (
    "/etc", "/usr", "/bin", "/sbin", "/boot", "/lib", "/lib64", "/opt",
    "/var", "/root", "/sys", "/proc", "/dev",
)


def _is_destructive(binary: str) -> bool:
    """Whether a program name is on the review list, including variants.

    Filesystem tools ship as mkfs.ext4, mkfs.vfat and friends, so an exact
    match on "mkfs" alone let a disk format through.
    """
    name = (binary or "").lower()
    return name in _DESTRUCTIVE_BINS or name.split(".", 1)[0] in {"mkfs", "fsck"}


def _writes_system_path(words: list[str]) -> bool:
    """Whether a write target lands outside the user's own files."""
    return any(
        not word.startswith("-") and word.startswith(_SYSTEM_PREFIXES)
        for word in words
    )


# Operators that end one command segment and begin the next.
_SHELL_SEPARATORS = frozenset({"|", "||", "&&", "&", ";", ";;", "\n", "(", ")"})


def _shell_segments(cmd: str) -> list[list[str]] | None:
    """Split a command line into argv segments, honouring quotes.

    Splitting on the metacharacters with a regex before parsing quotes tore
    patterns such as ``grep "a\\|b"`` in half; the halves then failed to lex and
    an ordinary search was reported as "unbalanced quotes". Lexing first keeps
    quoted metacharacters inside their argument. Returns None if the command
    genuinely does not parse.
    """
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    segments: list[list[str]] = []
    current: list[str] = []
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        # Redirections are checked as raw text, not commands. Drop the operator
        # and its target so `pytest 2>&1 | tail` leaves no bare "1".
        if tok and ("<" in tok or ">" in tok) and all(ch in "<>&" for ch in tok):
            skip_next = True
            continue
        if tok in _SHELL_SEPARATORS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(tok)
    if current:
        segments.append(current)
    return segments

_INSPECTION_SHELL_BINS = frozenset({
    "ls", "cat", "head", "tail", "wc", "file", "stat", "find", "grep", "rg",
    "which", "pwd", "env", "date", "tree", "du", "df", "basename", "dirname",
    "realpath", "sort", "uniq", "cut", "sed", "awk", "diff", "cmp", "md5sum",
    "sha256sum", "jq", "git",
})


def shell_is_inspection(cmd: str) -> bool:
    """Return whether every shell segment only reads/searches existing state."""
    text = (cmd or "").strip()
    if not text:
        return False
    segments = _shell_segments(text)
    if segments is None:
        return False
    saw_segment = False
    for words in segments:
        while words and re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", words[0]):
            words = words[1:]
        if not words or Path(words[0]).name not in _INSPECTION_SHELL_BINS:
            return False
        saw_segment = True
    return saw_segment


def shell_primary_binary(cmd: str) -> str:
    """The program name of the first segment, for 'always allow this program'."""
    segments = _shell_segments(cmd or "")
    if not segments:
        return ""
    words = segments[0]
    while words and re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", words[0]):
        words = words[1:]
    return Path(words[0]).name if words else ""


def classify_shell(cmd: str) -> tuple[str, str]:
    """Return ("safe"|"review", reason) for a shell command.

    "safe" means it runs without interrupting the user when shell_approval_mode
    is "safe_auto". The policy is a denylist, not an allowlist: an agent that
    asks before every unrecognised command is not autonomous, and the earlier
    allowlist stopped ordinary work (cp, arbitrary python, docker) far more
    often than it stopped anything dangerous. Review is reserved for what a
    user would actually want to catch: destroying data, escalating privilege,
    changing the machine, or running code fetched from the network.
    """
    text = (cmd or "").strip()
    if not text:
        return "review", "empty command"
    low = text.lower()
    for bad in _REVIEW_PATTERNS:
        if bad in low:
            return "review", f"writes to a system path ({bad.strip()!r})"
    if _PIPE_TO_SHELL.search(text):
        return "review", "pipes downloaded content into a shell"
    segments = _shell_segments(text)
    if segments is None:
        return "review", "unbalanced quotes"
    # Command substitution can hide a destructive call from the scan below, so
    # only inspect it when it is present rather than refusing outright.
    for inner in re.findall(r"\$\(([^()]*)\)|`([^`]*)`", text):
        nested = (inner[0] or inner[1] or "").strip()
        if nested:
            verdict, why = classify_shell(nested)
            if verdict == "review":
                return "review", f"command substitution runs something risky: {why}"
    for words in segments:
        while words and re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", words[0]):
            words = words[1:]
        if not words:
            continue
        binary = Path(words[0]).name
        rest = words[1:]
        if _is_destructive(binary):
            return "review", f"{binary!r} can destroy data or change the system"
        # A runner's arguments are program names too (find -exec rm, xargs shred).
        if binary in _COMMAND_RUNNERS and any(
            _is_destructive(Path(word).name) for word in rest
        ):
            risky = next(word for word in rest if _is_destructive(Path(word).name))
            return "review", f"runs {risky!r} through {binary!r}"
        if binary in _WRITES_FILES and _writes_system_path(rest):
            return "review", f"{binary} writes to a system path"
        if binary == "git":
            sub = rest[0] if rest else ""
            if sub in _GIT_REVIEW:
                return "review", f"git {sub} publishes or discards work"
            if sub == "reset" and any(a == "--hard" for a in rest):
                return "review", "git reset --hard discards uncommitted work"
            if sub == "clean" and any(a.startswith("-") and "f" in a for a in rest):
                return "review", "git clean -f deletes untracked files"
    return "safe", "no destructive or privileged operation"


def _run_shell(args: dict, project_root: Path | None) -> str:
    cwd = project_root or Path.home()
    cmd = args.get("command") or ""
    banned = ["format ", "shutdown", "rm -rf /", "del /s /q C:\\", "Remove-Item -Recurse -Force C:\\"]
    low = cmd.lower()
    if any(b.lower() in low for b in banned):
        return json.dumps({"error": "Command blocked for safety"})
    timeout = int(args.get("timeout_sec") or 60)
    env = os.environ.copy()
    if os.name == "nt":
        shell = shutil_which("powershell.exe") or shutil_which("powershell")
        command = (
            [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", cmd]
            if shell
            else ["cmd.exe", "/d", "/s", "/c", cmd]
        )
    else:
        # pip installs and python3 test runs must resolve to the same
        # interpreter. Ubuntu's /usr/bin/python3 ships without pip, so a conda
        # prefix that has both leads the PATH.
        env["PATH"] = _toolchain_path(env.get("PATH", ""))
        # Lets `npm i -g` succeed without sudo instead of failing on /usr/lib.
        env.setdefault("npm_config_prefix", str(Path.home() / ".npm-global"))
        if re.search(r"(^|[;&|]\s*)python(\s|$)", cmd) and not shutil_which("python", env["PATH"]):
            cmd = re.sub(r"(^|[;&|]\s*)python(\s|$)", r"\1python3\2", cmd)
        command = ["/bin/bash", "-lc", cmd]
    try:
        proc = subprocess.run(
            command,
            shell=False,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        # Keep it whole up to a sanity cap and let the harness offload it.
        # Slicing the tail drops the head of stdout, where a build log's first
        # error is, and that loss is permanent: the command already ran.
        out = (proc.stdout or "")[-SHELL_OUTPUT_HARD_CAP:]
        err = (proc.stderr or "")[-SHELL_OUTPUT_HARD_CAP:]
        return json.dumps({"exit_code": proc.returncode, "stdout": out, "stderr": err, "cwd": str(cwd)})
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"Timed out after {timeout}s"})


def _map_inline_line(line_no: int, line_map: list[tuple[int, int]]) -> int:
    """Translate a concatenated-script line back to its line in the HTML file."""
    html_line = line_no
    for concat_start, html_start in line_map:
        if line_no >= concat_start:
            html_line = html_start + (line_no - concat_start)
        else:
            break
    return html_line


# Node prints its own internals after the syntax error. They are never
# actionable and they crowd out the one line that is.
_NODE_NOISE = re.compile(
    r"^[ \t]*(?:at\s+.*|Node\.js\s+v[\d.]+.*)$", re.MULTILINE
)


def _clean_node_error(
    raw: str, target: Path, real: Path, line_map: list[tuple[int, int]]
) -> str:
    """Rewrite a `node --check` failure to point at the file the model can edit.

    Left alone, the message names a temp file and a line number from the
    concatenated inline scripts, so a model correcting "line 412" edits an
    unrelated part of the HTML and fails again.
    """
    text = raw
    if line_map:
        pattern = re.escape(str(target)) + r":(\d+)"

        def _fix(match: re.Match) -> str:
            return f"{real}:{_map_inline_line(int(match.group(1)), line_map)}"

        text = re.sub(pattern, _fix, text)
    text = text.replace(str(target), str(real))
    text = _NODE_NOISE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _validate_file(args: dict, project_root: Path | None) -> str:
    path, err = resolve_existing(args.get("path") or "", project_root, want_dir=False)
    if err:
        return err
    assert path is not None
    if path.stat().st_size > 2_000_000:
        return json.dumps({"ok": False, "error": "File too large to validate", "path": str(path)})

    suffix = path.suffix.lower()
    source = path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".py":
        try:
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            return json.dumps({
                "ok": False,
                "path": str(path),
                "error": f"line {exc.lineno}: {exc.msg}",
            })
        return json.dumps({"ok": True, "path": str(path), "validator": "Python compile"})

    if suffix == ".json":
        try:
            json.loads(source)
        except json.JSONDecodeError as exc:
            return json.dumps({
                "ok": False,
                "path": str(path),
                "error": f"line {exc.lineno}, column {exc.colno}: {exc.msg}",
            })
        return json.dumps({"ok": True, "path": str(path), "validator": "JSON parser"})

    javascript = source
    validator = "Node.js syntax check"
    # Maps a line in the concatenated script back to its line in the HTML file.
    line_map: list[tuple[int, int]] = []
    if suffix in {".html", ".htm"}:
        blocks: list[tuple[str, int]] = []
        for match in re.finditer(
            r"<script(?:\s[^>]*)?>(.*?)</script\s*>", source, re.DOTALL | re.IGNORECASE
        ):
            body = match.group(1)
            if not body.strip():
                continue
            blocks.append((body, source.count("\n", 0, match.start(1)) + 1))
        opened = len(re.findall(r"<script(?:\s[^>]*)?>", source, re.IGNORECASE))
        closed = len(re.findall(r"</script\s*>", source, re.IGNORECASE))
        if opened > closed:
            # Failing open is how a shredded file passes its own gate: with
            # </script> destroyed the regex matches nothing, the scan reports no
            # inline scripts, and every later edit skips the JS check.
            return json.dumps({
                "ok": False,
                "path": str(path),
                "validator": "HTML scan",
                "error": (
                    f"Unclosed <script> block: {opened} opening tag(s), {closed} closing tag(s). "
                    "The file's JavaScript cannot be checked and the document is malformed."
                ),
                "hint": "Restore the missing </script> (and </body></html>) before editing further.",
            })
        if not blocks:
            return json.dumps({"ok": True, "path": str(path), "validator": "HTML scan", "note": "No inline scripts"})
        # Concatenating blocks renumbers every line, so record where each one
        # lands and translate the error back afterwards.
        parts: list[str] = []
        cursor = 1
        for index, (body, html_start) in enumerate(blocks):
            if index:
                parts.append("\n;\n")
                cursor += 2
            line_map.append((cursor, html_start))
            parts.append(body)
            cursor += body.count("\n")
        javascript = "".join(parts)
        validator = "Inline JavaScript syntax check"
    elif suffix not in {".js", ".mjs", ".cjs"}:
        return json.dumps({
            "ok": False,
            "path": str(path),
            "error": f"No built-in validator for {suffix or 'files without an extension'}",
        })

    node = shutil_which("node")
    if not node:
        return json.dumps({"ok": False, "path": str(path), "error": "Node.js is not installed"})
    temp_path: Path | None = None
    target = path
    try:
        if suffix in {".html", ".htm"}:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=f".{path.stem}.aether-inline-",
                suffix=".js",
                delete=False,
            ) as handle:
                handle.write(javascript)
                temp_path = Path(handle.name)
            target = temp_path
        proc = subprocess.run([node, "--check", str(target)], capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raw = (proc.stderr or proc.stdout or "").strip()
            return json.dumps({
                "ok": False,
                "path": str(path),
                "validator": validator,
                "error": _clean_node_error(raw, target, path, line_map)[-2000:] or None,
            })

        # Node --check accepts undefined identifiers that break at runtime, so
        # use ESLint's no-undef as a second gate when it is available. Ignore
        # unrelated ESLint failures so newer syntax still passes on Node.
        eslint = shutil_which("eslint")
        if eslint:
            lint_proc = subprocess.run(
                [
                    eslint,
                    "--no-eslintrc",
                    "--no-ignore",
                    "--env", "browser,es6,node",
                    "--parser-options", "ecmaVersion:2020",
                    "--rule", "no-undef: error",
                    target.name,
                ],
                cwd=str(target.parent),
                capture_output=True,
                text=True,
                timeout=30,
            )
            lint_output = (lint_proc.stdout or "") + (lint_proc.stderr or "")
            if lint_proc.returncode != 0 and "no-undef" in lint_output:
                return json.dumps({
                    "ok": False,
                    "path": str(path),
                    "validator": f"{validator} + ESLint no-undef",
                    "error": lint_output.strip()[-3000:],
                })
        return json.dumps({
            "ok": True,
            "path": str(path),
            "validator": validator if not eslint else f"{validator} + ESLint no-undef",
            "error": None,
        })
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)


def shutil_which(name: str, path: str | None = None) -> str | None:
    from shutil import which
    return which(name, path=path)


def _conda_bin() -> str | None:
    """Bin dir of a conda install whose python3 and pip are the same install."""
    roots: list[Path] = []
    prefix = os.environ.get("CONDA_PREFIX")
    if prefix:
        roots.append(Path(prefix))
    roots += [Path.home() / "anaconda3", Path.home() / "miniconda3"]
    for root in roots:
        bin_dir = root / "bin"
        if (bin_dir / "python3").exists() and (bin_dir / "pip").exists():
            return str(bin_dir)
    return None


def _toolchain_path(base: str = "") -> str:
    """PATH the agent's shell and toolchain probe both use.

    A GUI-launched Aether inherits a bare session PATH, so per-user toolchains
    (npm -g, rustup, go, deno, bun) have to be added back explicitly.
    """
    extra = [p for p in (_conda_bin(), "/usr/bin", "/usr/local/bin") if p]
    extra += [
        str(Path.home() / d)
        for d in (".local/bin", ".npm-global/bin", ".cargo/bin", "go/bin", ".deno/bin", ".bun/bin")
    ]
    return os.pathsep.join(extra + [base or os.environ.get("PATH", "")])


# name → (binaries, how to get it without breaking the user's machine)
TOOLCHAINS = (
    ("Python", ("python3", "pip"), "Already present. pip comes from the same interpreter as python3."),
    ("Node / JavaScript", ("node", "npm", "npx"), "sudo apt install nodejs npm. Ask the user to run it."),
    ("TypeScript", ("tsc", "tsx"), "npm i -g typescript tsx. No sudo needed, Aether points npm at ~/.npm-global. Or run one-off with npx tsx file.ts."),
    ("C / C++", ("gcc", "g++", "make", "cmake"), "sudo apt install build-essential cmake. Ask the user to run it."),
    ("Java", ("java", "javac"), "sudo apt install default-jdk. Ask the user to run it."),
    ("Rust", ("cargo", "rustc"), "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh. Installs to ~/.cargo/bin, no sudo."),
    ("Go", ("go",), "sudo apt install golang. Ask the user to run it."),
    (".NET", ("dotnet",), "sudo apt install dotnet-sdk-8.0. Ask the user to run it."),
    ("PHP", ("php",), "sudo apt install php-cli. Ask the user to run it."),
    ("Ruby", ("ruby",), "sudo apt install ruby. Ask the user to run it."),
    ("Perl", ("perl",), "Ships with Ubuntu."),
    ("SQLite", ("sqlite3",), "sudo apt install sqlite3. Ask the user to run it."),
)


def _check_toolchains() -> str:
    """Report which language toolchains this machine can actually build and run."""
    path = _toolchain_path()
    ready, missing = [], []
    for label, bins, hint in TOOLCHAINS:
        found = {}
        for b in bins:
            exe = shutil_which(b, path)
            if not exe:
                continue
            version = ""
            try:
                pr = subprocess.run(
                    [exe, "--version"], capture_output=True, text=True, timeout=8,
                    env={**os.environ, "PATH": path},
                )
                version = ((pr.stdout or pr.stderr or "").strip().splitlines() or [""])[0][:60]
            except Exception:
                pass
            found[b] = version or exe
        if found:
            ready.append({"toolchain": label, "available": found,
                          "incomplete": [b for b in bins if b not in found] or None})
        else:
            missing.append({"toolchain": label, "install": hint})
    return json.dumps({
        "ready": ready,
        "missing": missing,
        "note": (
            "Aether cannot run sudo. For anything marked 'ask the user', use ask_user to "
            "request it rather than attempting the install. npm -g, pip and rustup work "
            "without sudo and may be used directly."
        ),
    }, indent=2)


def _git_status(project_root: Path | None) -> str:
    if not project_root:
        return json.dumps({"error": "No project attached; git status needs a project root."})
    def run(cmd: list[str]) -> str:
        try:
            p = subprocess.run(cmd, cwd=str(project_root), capture_output=True, text=True, timeout=20)
            return (p.stdout or p.stderr or "").strip()
        except Exception as e:
            return str(e)
    return json.dumps({
        "status": run(["git", "status", "-sb"]),
        "log": run(["git", "log", "-5", "--oneline"]),
    }, indent=2)


def _remember(args: dict, project_id: str | None) -> str:
    from storage import add_memory_note
    scope = args.get("scope") or "global"
    if scope == "project":
        if not project_id:
            return json.dumps({"error": "No project attached"})
        scope = f"project_{project_id}"
    add_memory_note(scope, args.get("text") or "")
    return json.dumps({"ok": True, "scope": scope})


# Parse tool calls: Ollama native tool_calls, plus XML-ish fallbacks.
TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*\{(.*?)\}\s*</tool_call>",
    re.DOTALL | re.IGNORECASE,
)


def extract_tool_calls(message: dict) -> list[dict]:
    calls = []
    native = message.get("tool_calls") or []
    for tc in native:
        fn = tc.get("function") or {}
        name = fn.get("name")
        raw_args = fn.get("arguments")
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError:
                args = {"raw": raw_args}
        else:
            args = raw_args or {}
        if name:
            calls.append({
                "id": tc.get("id") or name,
                "index": fn.get("index", tc.get("index")),
                "name": name,
                "arguments": args,
            })
    content = message.get("content") or ""
    for m in TOOL_CALL_RE.finditer(content):
        try:
            obj = json.loads("{" + m.group(1) + "}")
            name = obj.get("name") or obj.get("tool")
            args = obj.get("arguments") or obj.get("parameters") or {}
            if name:
                calls.append({"id": name, "name": name, "arguments": args})
        except Exception:
            pass
    # Alternative: ```tool ... ```
    alt = re.findall(r"```tool\s*(\{.*?\})\s*```", content, re.DOTALL)
    for block in alt:
        try:
            obj = json.loads(block)
            name = obj.get("name")
            if name:
                calls.append({"id": name, "name": name, "arguments": obj.get("arguments") or {}})
        except Exception:
            pass
    return calls


def normalize_questions(args: dict) -> list[dict]:
    """Validate/clean an ask_user payload. Returns [] if nothing usable."""
    raw = (args or {}).get("questions")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for i, q in enumerate(raw[:4]):
        if not isinstance(q, dict):
            continue
        text = str(q.get("question") or q.get("text") or "").strip()
        if not text:
            continue
        opts = []
        seen = set()
        for o in (q.get("options") or [])[:4]:
            if isinstance(o, str):
                label, desc = o.strip(), ""
            elif isinstance(o, dict):
                label = str(o.get("label") or o.get("value") or "").strip()
                desc = str(o.get("description") or "").strip()
            else:
                continue
            if not label or label.lower() in seen:
                continue
            seen.add(label.lower())
            opts.append({"label": label, "description": desc})
        # A question with no real choices is still askable as free text.
        out.append({
            "id": str(q.get("id") or f"q{i+1}"),
            "question": text,
            "header": str(q.get("header") or "")[:12],
            "multi_select": bool(q.get("multi_select") or q.get("multiSelect")),
            "options": opts,
        })
    return out


def dedupe_tool_calls(calls: list[dict], limit: int = 8) -> list[dict]:
    """Drop duplicate (name, arguments) pairs and cap tools per model step."""
    out: list[dict] = []
    seen: set[str] = set()
    for c in calls or []:
        name = c.get("name") or ""
        if not name:
            continue
        key = name + "::" + json.dumps(c.get("arguments") or {}, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
        if len(out) >= limit:
            break
    return out


def _file_revision(path: Path) -> str:
    """Cheap identity for the exact on-disk version a read came from."""
    stat = path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def normalize_read_request(
    args: dict, project_root: Path | None
) -> tuple[str, int, int, str] | None:
    """Resolve a read call to a canonical path, clamped range, and revision.

    This mirrors ``_read_file`` closely so semantically equivalent argument
    shapes (omitted bounds, ranges extending past EOF) compare the same way.
    Invalid and binary reads are left to the real tool for its normal error.
    """
    try:
        path, err = resolve_existing(args.get("path") or "", project_root, want_dir=False)
        if err or path is None or path.stat().st_size > 2_000_000:
            return None
        raw = path.read_bytes()
        if b"\x00" in raw[:8192]:
            return None
        line_count = len(raw.decode("utf-8", errors="replace").splitlines())
        start = max(1, int(args.get("start_line") or 1))
        end = min(int(args.get("end_line") or line_count), line_count)
        if end < start:
            return None
        return str(path), start, end, _file_revision(path)
    except (KeyError, TypeError, ValueError, OSError):
        return None


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union of line ranges, coalescing anything adjacent or overlapping."""
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1] + 1:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def intersection_size(start: int, end: int, intervals: list[tuple[int, int]]) -> int:
    """How many lines of [start, end] are already covered by `intervals`."""
    return sum(
        max(0, min(end, covered_end) - max(start, covered_start) + 1)
        for covered_start, covered_end in intervals
    )


_INCOMPLETE_RE = re.compile(
    r"(?i)\b("
    r"let me|i('ll| will)|i('m| am) going to|next i|need to|"
    r"checking|looking (at|into|for)|examining|inspecting|reading|"
    r"first[,.]?\s|then i|after (that|this)|continue|todo|to[- ]do|"
    r"i('ll| will) (check|look|read|open|run|search|try)|"
    r"one (moment|sec)|hang on|give me a (sec|moment)|"
    # Present-continuous promises: "I'm writing it now" read as a finished answer,
    # so the loop stopped and the user was told work was happening that never was.
    r"i('m| am) (writing|creating|building|making|adding|implementing|updating|fixing|working)|"
    r"not yet\b"
    r")\b"
)
# A reply that is only a promise to act, e.g. "Writing it now."
_PROMISE_START_RE = re.compile(
    r"(?i)^(writing|creating|building|making|adding|implementing|updating|fixing|"
    r"generating|drafting|starting)\b"
)
# looks_like_final_answer waves anything over 400 chars through as the
# deliverable. A written-out plan defeats that: long, well formed, and closing
# on a promise rather than a result. The loop read those as a finished turn and
# ended runs having called no tools at all.
_PROMISE_TAIL_RE = re.compile(
    r"(?i)\b("
    r"let me (start|begin|now|first|go ahead|proceed|get started)|"
    r"let('s| us) (start|begin|get started|proceed|do this)|"
    r"i('ll| will) (start|begin|now|go ahead|proceed|create|write|build|implement|"
    r"add|update|fix|run|generate|draft|set up)|"
    r"i('m| am) going to (start|begin|create|write|build|implement|add|update|fix|run)|"
    r"(starting|beginning|proceeding) (with|by|now)|"
    r"first[,]? (i|let me|we)('ll| will| need)?"
    r")\b"
)
# Only the closing lines count: "I'll create the file" in the middle of a report
# that then shows the created file is a narration of work already done.
_PROMISE_TAIL_WINDOW = 300


def ends_with_promise(content: str) -> bool:
    """True when a reply signs off by promising the next action instead of taking it."""
    text = (content or "").strip()
    return bool(text) and bool(_PROMISE_TAIL_RE.search(text[-_PROMISE_TAIL_WINDOW:]))


def looks_like_final_answer(content: str) -> bool:
    """True when the model already wrote a substantial deliverable (not mid-narration)."""
    text = (content or "").strip()
    if len(text) < 400:
        return False
    if text.endswith(("...", "…", ":")):
        return False
    # Short planning openers are not final even if long later. Rare, so length wins.
    return True


# Sign-offs that contain trigger words but mean the opposite of "still working".
# "Let me know if you want" used to kick a finished run back into the loop.
_SIGNOFF_RE = re.compile(
    r"(?i)\b(let me know|lmk|let me know if|if you (want|need|would like)|"
    r"happy to|want me to|shall i|anything else|hope (this|that) helps)\b"
)


def looks_incomplete(content: str) -> bool:
    """True when the model is still working / planning instead of finishing."""
    text = (content or "").strip()
    if not text:
        return True
    # Checked before the sign-off and length bail-outs: a plan that ends on a
    # promise is a failed action no matter how long or how politely it closes.
    if ends_with_promise(text):
        return True
    # A sign-off is a finished answer, however short.
    if _SIGNOFF_RE.search(text):
        return False
    # Long replies are usually the deliverable. Words like "looking" or "todo" inside
    # an analysis must not keep the agent loop alive forever.
    if looks_like_final_answer(text):
        return False
    if _INCOMPLETE_RE.search(text):
        return True
    if len(text) < 200 and _PROMISE_START_RE.search(text):
        return True
    # Trailing ellipsis / colon often means "more coming"
    if text.endswith(("...", "…", ":")):
        return True
    # Very short non-answer after starting work. A reply that hands over a path
    # or a run command is the deliverable, so it must not be dragged back in.
    delivers = re.search(r"(?i)(^|\s)(/|~/|\./)\S|\b(python3?|node|npx|bash|sh|cargo|go|java|make)\b", text)
    if len(text) < 80 and not text.endswith((".", "!", "?")) and not delivers:
        return True
    return False


_INTERNAL_CONTINUATION_PLACEHOLDERS = {
    "(continuing tools for the same job)",
    "(working notes, continue the same job; not a final answer yet)",
}


def is_internal_continuation_placeholder(content: str) -> bool:
    """True for loop-control text that must never become a user-facing answer.

    Dashes are normalized so the several punctuation variants older transcripts
    carry all match the one form listed above.
    """
    text = re.sub(r"\s*[-\u2013\u2014,]\s*", ", ", (content or "").strip().lower())
    return text in _INTERNAL_CONTINUATION_PLACEHOLDERS


def to_ollama_tool_calls(calls: list[dict], *, elide_large_inputs: bool = False) -> list[dict]:
    """Normalize stored tool calls into Ollama chat API shape.

    Ollama expects function.arguments as an object, not a JSON string:
    stringifying them causes 400: "Value looks like object, but can't find closing '}'".
    """
    out = []
    for c in calls or []:
        if not isinstance(c, dict):
            continue
        if c.get("function"):
            fn = dict(c.get("function") or {})
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    args = {"raw": args}
            fn["arguments"] = copy.deepcopy(args) if isinstance(args, dict) else {}
            if elide_large_inputs:
                _elide_large_replay_input(fn)
            entry = {k: v for k, v in c.items() if k != "function"}
            entry["function"] = fn
            if "type" not in entry:
                entry["type"] = "function"
            out.append(entry)
            continue
        name = c.get("name")
        if not name:
            continue
        args = c.get("arguments") if "arguments" in c else c.get("parameters")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {"raw": args}
        if not isinstance(args, dict):
            args = {}
        fn = {"name": name, "arguments": copy.deepcopy(args)}
        if elide_large_inputs:
            _elide_large_replay_input(fn)
        function = fn
        if c.get("index") is not None:
            function["index"] = c["index"]
        entry = {
            "type": "function",
            "function": function,
        }
        if c.get("id") and c.get("id") != name:
            entry["id"] = c["id"]
        out.append(entry)
    return out


def _elide_large_replay_input(fn: dict) -> None:
    """Bound large historical tool inputs without modifying the stored transcript."""
    if fn.get("name") not in {"write_file", "append_file"}:
        return
    args = fn.get("arguments")
    if not isinstance(args, dict):
        return
    body = args.get("content")
    bodies = [(args, "content", body)]
    for edit in args.get("edits") or []:
        if isinstance(edit, dict):
            bodies.append((edit, "content", edit.get("content")))
    for owner, key, value in bodies:
        if not isinstance(value, str) or len(value) <= 1200:
            continue
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        owner[key] = (
            f"[Aether replay elision: {len(value)} characters, sha256={digest}. "
            "The following tool result records whether the original edit succeeded; "
            "use read_file to inspect the current file.]"
        )


def tools_system_addon() -> str:
    names = ", ".join(t["function"]["name"] for t in TOOL_DEFS)
    return (
        f"\n\n# Tools\nAvailable tools: {names}.\n"
        "Use native function calling whenever an action or more information is needed. "
        "Never print a tool call as XML, JSON, or a code block. The runtime executes native calls, "
        "appends each named result, and samples you again. A normal assistant message with no tool "
        "calls ends the turn, so emit one only when you are ready to hand the result to the user.\n"
        "Work as one continuous turn: inspect, change, and verify with tools, then give one concise debrief.\n"
        "Editing existing files:\n"
        "- Prefer edit_file for an existing file, especially anything over 200 lines. Read only the "
        "relevant range. Batch independent ranges in one edits[] call when you already know them. "
        "Do not repeatedly read the whole file.\n"
        "- write_file CREATES a file. It is rejected if the path already exists. A file is written "
        "once, when it is created, and edited from then on. To change an existing file use "
        "edit_file; to add to it use append_file. Only pass overwrite=true if the user explicitly "
        "asked you to replace that file from scratch.\n"
        "- For a large NEW artifact, never attempt one enormous write_file call. Write a compact, valid "
        "foundation first, then use append_file for coherent chunks at syntax-safe boundaries. For a "
        "standalone HTML file that already has closing tags, insert JavaScript before </script>, CSS before "
        "</style>, or markup before </body>; never append source after </html>. Keep each call below roughly "
        "500 lines / 12,000 characters and validate_file after the final chunk.\n"
        "Writing code in another language:\n"
        "- Use check_toolchains when a project needs a compiler or external runtime. Skip that probe "
        "for self-contained HTML and for formats validate_file handles directly.\n"
        "- write_file automatically validates Python, JavaScript, JSON, and HTML. If that result says "
        "validation.ok=true, do not spend another tool step rechecking the same file. Otherwise use "
        "validate_file after a later edit; do not build an ad-hoc run_shell syntax check.\n"
        "- edit_file is transactional for those formats: when validation fails it returns changed=false "
        "and preserves the original file. Correct the edit against the same original line numbers; do not "
        f"reread the entire file. Keep batches to {MAX_EDIT_RANGES} ranges or fewer.\n"
        "- If the toolchain is missing and the install needs sudo, ask_user for it; you cannot run sudo.\n"
        "- npm -g, pip, npx and rustup work without sudo, so use them directly. For a one-off "
        "TypeScript run prefer `npx tsx file.ts` over a global install.\n"
        "Clarifying questions (ask_user):\n"
        "- ask_user is the ONLY way to talk to the user mid-job. It blocks until they answer.\n"
        "- Ask when a choice would change what you build: ambiguous scope, two valid approaches,\n"
        "  an unstated preference, an unclear target file. Ask EARLY, before writing code, not after.\n"
        "- Batch related questions into one ask_user call (up to 4). Give 2-4 concrete options each.\n"
        "- Read the Established canon block first. If it already answers something, use that answer\n"
        "  and do NOT ask again.\n"
        "- Do not ask about things you can find out yourself with a tool. Read the file instead.\n"
        "- After they answer, keep going in the same job. Do not restart.\n"
        "Paths: all file paths are relative to the Active project Root. "
        "If the repo has nested folders (e.g. reedus_llm/...), include that prefix. "
        "If a tool returns suggestions, use one of them instead of retrying the same bad path.\n"
        "Shell: use python3 (not python). Prefer bash. conda may be unavailable, use full env paths if needed.\n"
        "Planning before code:\n"
        "- Anchor steps to functions or marker lines, never bare line numbers: earlier edits "
        "shift every line below them.\n"
        "Task list:\n"
        "- todo_write is optional UI state for genuinely multi-step work; it never controls whether the runtime continues.\n"
        "- If you use it, update it at meaningful milestones and do not recreate the same plan.\n"
        "- Plan items must represent distinct tool phases. Do not split one write_file/edit_file action "
        "into several feature todos, and never batch-complete multiple milestones in one todo_write call.\n"
        "You may call multiple tools per step. Do not claim you edited a file unless write_file, append_file, or edit_file succeeded."
    )


CONTINUE_NUDGE = (
    "[agent-loop] Same user job, not a new request. "
    "Continue from the active milestone and newest tool result. Do not restart repository "
    "inspection or reread an entire file; read only a precise missing range if it is essential. "
    "If a plan step is done, todo_write merge=true with status=completed (do NOT recreate the plan). "
    "When open todos are 0, write ONE final debrief only. "
    "Do not restart from scratch. Do not ask the user to continue."
)

ACTION_ONLY_NUDGE = (
    "[agent-loop] ACTION-ONLY RETRY for the same job. Do not narrate, plan, or reason. "
    "The previous sample exhausted its output allowance before completing an action. "
    "Emit the next required tool call now. If a new artifact is large, write a compact foundation "
    "and continue it with append_file chunks instead of one enormous call. The native schema limits "
    "write_file and append_file content to 12,000 characters; do not attempt to exceed it. For closed HTML, use "
    "append_file.before with </script>, </style>, or </body>; never append after </html>. Keep each chunk below "
    "roughly 500 lines / 12,000 characters. When changing an existing large file, use edit_file "
    "with find/replace anchors instead of rereading or regenerating the whole file; batch "
    "independent edits in edits[] when possible. "
    "If the implementation is genuinely complete, "
    "call todo_write with merge=true to update the existing open items before the final debrief."
)

COURSE_CORRECT_PREFIX = (
    "[User course-correction. SAME continuous agent job, not a new task.]\n"
    "Incorporate the constraint below. Call todo_write (merge=true) to cancel obsolete steps "
    "and adjust the plan, then continue from where you left off. "
    "Do not re-read huge docs you already loaded unless the user asks. "
    "Do not restart from scratch.\n\n"
)


def tool_activity_line(name: str, arguments: dict | None = None, result: str | None = None) -> str:
    """Short line for the thinking dropdown, not a full tool dump."""
    args = arguments or {}
    hint = ""
    if name == "run_shell":
        hint = str(args.get("command") or "")[:80]
    elif name in ("read_file", "write_file", "append_file", "list_dir", "search_files"):
        hint = str(args.get("path") or args.get("query") or "")[:80]
    elif name == "delegate":
        hint = str(args.get("task") or "")[:80]
    elif name == "todo_write":
        todos = args.get("todos") or []
        hint = ", ".join(
            f"{t.get('id')}={t.get('status')}" for t in todos[:4] if isinstance(t, dict)
        )
    else:
        hint = str(list(args.keys())[:4])
    line = f"→ {name}" + (f" ({hint})" if hint else "")
    if result is not None:
        err = False
        try:
            parsed = json.loads(result) if isinstance(result, str) and result.strip().startswith("{") else None
            if isinstance(parsed, dict) and parsed.get("error"):
                err = True
                line += f" ✗ {str(parsed.get('error'))[:60]}"
        except Exception:
            pass
        if not err:
            line += " ✓"
    return line
