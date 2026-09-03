# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Persistent chats, memory, projects, settings."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from config import CHATS, DEFAULT_SETTINGS, MEMORY, PROJECTS, SETTINGS_PATH, UPLOADS


def _ensure() -> None:
    for p in (CHATS, MEMORY, PROJECTS, UPLOADS, SETTINGS_PATH.parent):
        p.mkdir(parents=True, exist_ok=True)


def _read(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ── settings ──────────────────────────────────────────────────────────────

# Bumped when a stored setting has to be migrated rather than merely defaulted.
SETTINGS_VERSION = 2
# A stored value always wins over DEFAULT_SETTINGS, so raising the agentic
# working set needs a migration or existing installs keep the old window. Only
# the exact old default moves; a value the user picked is left alone.
_OLD_AGENT_CTX = 32768


def _migrate_settings(s: dict) -> tuple[dict, bool]:
    changed = False
    if int(s.get("settings_version") or 0) < 2:
        for key in ("agent_context", "coder_context"):
            new = DEFAULT_SETTINGS.get(key)
            if s.get(key) == _OLD_AGENT_CTX and new and new > _OLD_AGENT_CTX:
                s[key] = new
                changed = True
        s["settings_version"] = SETTINGS_VERSION
        changed = True
    return s, changed


def load_settings() -> dict:
    _ensure()
    s = dict(_read(SETTINGS_PATH, {}) or {})
    # Pre-1.3 installations persisted a hard agent step ceiling.  It is ignored
    # by the progress-based loop and must not leak back into API responses or a
    # later settings save, where it would look like an active safety control.
    s.pop("agent_max_steps", None)
    s, migrated = _migrate_settings(s)
    if migrated:
        _write(SETTINGS_PATH, s)
    out = dict(DEFAULT_SETTINGS)
    out.update(s)
    return out


def save_settings(patch: dict) -> dict:
    s = load_settings()
    s.update(patch)
    _write(SETTINGS_PATH, s)
    return s


# ── chats ─────────────────────────────────────────────────────────────────

def new_chat(mode: str = "chat", title: str = "New chat", project_id: str | None = None) -> dict:
    _ensure()
    if mode in ("code", "computer"):
        mode = "agentic"
    cid = uuid.uuid4().hex[:12]
    chat = {
        "id": cid,
        "title": title,
        "mode": mode,  # chat | agentic
        "reasoning": False,
        "computer_use": False,
        "project_id": project_id,
        "created": time.time(),
        "updated": time.time(),
        "messages": [],
        "summary": None,
        "pinned": False,
        "agent_todos": [],
        "file_library": [],
    }
    _write(CHATS / f"{cid}.json", chat)
    return chat


def _norm_mode(mode: str | None) -> str:
    if mode in ("code", "computer", "agentic"):
        return "agentic"
    return "chat"


def list_chats(mode: str | None = None) -> list[dict]:
    _ensure()
    items = []
    want = _norm_mode(mode) if mode else None
    for f in CHATS.glob("*.json"):
        c = _read(f, None)
        if not c:
            continue
        # Sub-agent transcripts stay on disk for audit, but they are sidechains
        # of a parent chat, not conversations of their own.
        if c.get("subagent"):
            continue
        cmode = _norm_mode(c.get("mode"))
        if want and cmode != want:
            continue
        items.append({
            "id": c["id"],
            "title": c.get("title", "Untitled"),
            "mode": cmode,
            "reasoning": c.get("reasoning", False),
            "computer_use": c.get("computer_use", False) or c.get("mode") == "computer",
            "project_id": c.get("project_id"),
            "updated": c.get("updated", 0),
            "pinned": c.get("pinned", False),
            "preview": _preview(c),
            "msg_count": len(c.get("messages") or []),
        })
    items.sort(key=lambda x: (not x["pinned"], -x["updated"]))
    return items


def _preview(c: dict) -> str:
    for m in reversed(c.get("messages") or []):
        if m.get("role") == "user" and m.get("content"):
            return (m["content"] or "").strip()[:120]
    return ""


def get_chat(cid: str) -> dict | None:
    return _read(CHATS / f"{cid}.json", None)


PLACEHOLDER_TITLES = {
    "",
    "New chat",
    "New code session",
    "New agentic session",
    "Computer use",
    "Untitled",
}


def is_placeholder_title(title: str | None) -> bool:
    return (title or "").strip() in PLACEHOLDER_TITLES


def save_chat(chat: dict) -> dict:
    chat["updated"] = time.time()
    for m in chat.get("messages") or []:
        if not m.get("id"):
            m["id"] = uuid.uuid4().hex[:10]
    # Provisional title from first user line until LLM auto-title lands
    if is_placeholder_title(chat.get("title")) and not chat.get("title_auto"):
        for m in chat.get("messages") or []:
            if m.get("role") == "user" and m.get("content"):
                raw = m["content"].strip().splitlines()[0]
                # Strip attachment dumps from provisional titles
                if raw.startswith("--- "):
                    continue
                t = raw[:48]
                chat["title"] = t + ("…" if len(raw) > 48 else "")
                break
    _write(CHATS / f"{chat['id']}.json", chat)
    return chat


def delete_chat(cid: str) -> bool:
    p = CHATS / f"{cid}.json"
    if p.exists():
        p.unlink()
        return True
    return False


def chat_excerpt(cid: str, max_chars: int = 6000) -> dict | None:
    c = get_chat(cid)
    if not c:
        return None
    lines = []
    if c.get("summary"):
        lines.append(f"[summary]\n{c['summary']}")
    for m in c.get("messages") or []:
        role = (m.get("role") or "?").upper()
        content = (m.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"{role}: {content[:2000]}")
    text = "\n\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 20] + "\n…[truncated]…"
    return {
        "id": c["id"],
        "title": c.get("title") or "Untitled",
        "mode": _norm_mode(c.get("mode")),
        "summary": c.get("summary") or "",
        "excerpt": text,
        "msg_count": len(c.get("messages") or []),
        "updated": c.get("updated", 0),
    }


def search_chats(query: str, exclude_id: str | None = None, limit: int = 8) -> list[dict]:
    """Keyword search across titles, previews, summaries, and recent message text."""
    q = (query or "").strip().lower()
    tokens = [t for t in re_split_tokens(q) if len(t) >= 2]
    items = []
    for meta in list_chats():
        if exclude_id and meta["id"] == exclude_id:
            continue
        c = get_chat(meta["id"])
        if not c:
            continue
        blob_parts = [
            c.get("title") or "",
            c.get("summary") or "",
            meta.get("preview") or "",
        ]
        for m in (c.get("messages") or [])[-12:]:
            blob_parts.append(m.get("content") or "")
        blob = "\n".join(blob_parts).lower()
        if tokens:
            score = sum(blob.count(t) * (3 if t in (c.get("title") or "").lower() else 1) for t in tokens)
            if score <= 0:
                # Title substring soft match
                title = (c.get("title") or "").lower()
                if q and q in title:
                    score = 5
                else:
                    continue
        else:
            score = meta.get("updated", 0) / 1e12
        items.append({
            "id": meta["id"],
            "title": meta["title"],
            "mode": meta["mode"],
            "preview": meta.get("preview") or "",
            "updated": meta.get("updated", 0),
            "score": score,
            "summary": (c.get("summary") or "")[:400],
        })
    items.sort(key=lambda x: (-x["score"], -x["updated"]))
    return items[:limit]


def re_split_tokens(q: str) -> list[str]:
    import re
    return re.findall(r"[a-z0-9_./\\-]+", q.lower())


def history_index(exclude_id: str | None = None, limit: int = 18) -> str:
    """Compact index of other chats for system prompt injection."""
    rows = []
    for meta in list_chats():
        if exclude_id and meta["id"] == exclude_id:
            continue
        title = (meta.get("title") or "Untitled").replace("\n", " ")
        prev = (meta.get("preview") or "").replace("\n", " ")[:90]
        rows.append(f"- [{meta['id']}] {title}")
        if len(rows) >= limit:
            break
    if not rows:
        return ""
    return (
        "# Chat history (other conversations)\n"
        "Titles only. Full excerpts are attached only when the user explicitly asks to recall another chat.\n"
        + "\n".join(rows)
    )


# ── memory (Claude-style lasting notes) ────────────────────────────────────

def load_memory(scope: str = "global") -> dict:
    _ensure()
    return _read(MEMORY / f"{scope}.json", {"scope": scope, "notes": [], "updated": 0})


def save_memory(scope: str, notes: list[dict]) -> dict:
    data = {"scope": scope, "notes": notes, "updated": time.time()}
    _write(MEMORY / f"{scope}.json", data)
    return data


def add_memory_note(scope: str, text: str, tags: list[str] | None = None) -> dict:
    mem = load_memory(scope)
    mem["notes"].insert(0, {
        "id": uuid.uuid4().hex[:8],
        "text": text.strip(),
        "tags": tags or [],
        "created": time.time(),
    })
    mem["updated"] = time.time()
    _write(MEMORY / f"{scope}.json", mem)
    return mem


def delete_memory_note(scope: str, note_id: str) -> dict:
    mem = load_memory(scope)
    mem["notes"] = [n for n in mem["notes"] if n.get("id") != note_id]
    mem["updated"] = time.time()
    _write(MEMORY / f"{scope}.json", mem)
    return mem


def memory_as_prompt(scope: str = "global", project_id: str | None = None) -> str:
    chunks = []
    g = load_memory("global")
    if g.get("notes"):
        chunks.append("## Global memory\n" + "\n".join(f"- {n['text']}" for n in g["notes"][:40]))
    if project_id:
        p = load_memory(f"project_{project_id}")
        if p.get("notes"):
            chunks.append("## Project memory\n" + "\n".join(f"- {n['text']}" for n in p["notes"][:40]))
    return "\n\n".join(chunks)



def update_memory_note(scope: str, note_id: str, text: str) -> dict | None:
    mem = load_memory(scope)
    found = False
    for n in mem.get("notes") or []:
        if n.get("id") == note_id:
            n["text"] = text.strip()
            n["updated"] = time.time()
            found = True
            break
    if not found:
        return None
    mem["updated"] = time.time()
    _write(MEMORY / f"{scope}.json", mem)
    return mem


def update_project(pid: str, patch: dict) -> dict | None:
    proj = get_project(pid)
    if not proj:
        return None
    for k in ("name", "root", "description"):
        if k in patch and patch[k] is not None:
            proj[k] = patch[k]
    if "root" in patch and patch["root"]:
        root_path = str(Path(patch["root"]).resolve())
        proj["root"] = root_path
        settings = load_settings()
        roots = list(settings.get("allowed_roots") or [])
        if root_path not in roots:
            roots.append(root_path)
            save_settings({"allowed_roots": roots})
    return save_project(proj)


def ensure_message_ids(chat: dict) -> dict:
    changed = False
    for m in chat.get("messages") or []:
        if not m.get("id"):
            m["id"] = uuid.uuid4().hex[:10]
            changed = True
    if changed:
        save_chat(chat)
    return chat


def update_message(cid: str, mid: str, content: str) -> dict | None:
    chat = get_chat(cid)
    if not chat:
        return None
    ensure_message_ids(chat)
    chat = get_chat(cid)
    for m in chat.get("messages") or []:
        if m.get("id") == mid:
            m["content"] = content
            m["edited"] = time.time()
            save_chat(chat)
            return chat
    return None


def edit_and_truncate(cid: str, mid: str, content: str) -> dict | None:
    """Update a user message and drop every message after it (for edit→resubmit)."""
    chat = get_chat(cid)
    if not chat:
        return None
    ensure_message_ids(chat)
    chat = get_chat(cid)
    msgs = list(chat.get("messages") or [])
    idx = next((i for i, m in enumerate(msgs) if m.get("id") == mid), -1)
    if idx < 0:
        return None
    if (msgs[idx].get("role") or "") != "user":
        return None
    msgs[idx]["content"] = content
    msgs[idx]["edited"] = time.time()
    chat["messages"] = msgs[: idx + 1]
    save_chat(chat)
    return chat


def delete_message(cid: str, mid: str) -> dict | None:
    """Delete a message. For user turns, also drop everything after it."""
    chat = get_chat(cid)
    if not chat:
        return None
    ensure_message_ids(chat)
    chat = get_chat(cid)
    msgs = list(chat.get("messages") or [])
    idx = next((i for i, m in enumerate(msgs) if m.get("id") == mid), -1)
    if idx < 0:
        return None
    if (msgs[idx].get("role") or "") == "user":
        chat["messages"] = msgs[:idx]
    else:
        chat["messages"] = [m for m in msgs if m.get("id") != mid]
    save_chat(chat)
    return chat

# ── projects ───────────────────────────────────────────────────────────────

def new_project(name: str, root: str, description: str = "") -> dict:
    _ensure()
    pid = uuid.uuid4().hex[:10]
    root_path = str(Path(root).resolve())
    proj = {
        "id": pid,
        "name": name,
        "root": root_path,
        "description": description,
        "created": time.time(),
        "updated": time.time(),
        "files_pinned": [],
    }
    _write(PROJECTS / f"{pid}.json", proj)
    # Allow agent access to any folder the user explicitly registers
    settings = load_settings()
    roots = list(settings.get("allowed_roots") or [])
    if root_path not in roots:
        roots.append(root_path)
        save_settings({"allowed_roots": roots})
    return proj


def list_projects() -> list[dict]:
    _ensure()
    items = [_read(f, None) for f in PROJECTS.glob("*.json")]
    items = [i for i in items if i]
    items.sort(key=lambda x: -x.get("updated", 0))
    return items


def get_project(pid: str) -> dict | None:
    return _read(PROJECTS / f"{pid}.json", None)


def save_project(proj: dict) -> dict:
    proj["updated"] = time.time()
    _write(PROJECTS / f"{proj['id']}.json", proj)
    return proj


def delete_project(pid: str) -> bool:
    p = PROJECTS / f"{pid}.json"
    if p.exists():
        p.unlink()
        return True
    return False


# Canon: answers the user has already given to clarifying questions. Every
# ask_user answer is recorded here and injected into the system prompt, so the
# same question is never asked twice.

def canon_scope(project_id: str | None) -> str:
    return f"canon_project_{project_id}" if project_id else "canon_global"


def load_canon(project_id: str | None = None) -> dict:
    _ensure()
    scope = canon_scope(project_id)
    return _read(MEMORY / f"{scope}.json", {"scope": scope, "entries": [], "updated": 0})


def record_canon(project_id: str | None, question: str, answer: str, *, header: str = "") -> dict:
    """Upsert one decision. Re-answering the same question overwrites it."""
    data = load_canon(project_id)
    entries = data.get("entries") or []
    q = (question or "").strip()
    a = (answer or "").strip()
    if not q or not a:
        return data
    key = q.lower()
    for e in entries:
        if (e.get("question") or "").strip().lower() == key:
            e["answer"] = a
            e["header"] = header or e.get("header") or ""
            e["updated"] = time.time()
            break
    else:
        entries.insert(0, {
            "id": uuid.uuid4().hex[:8],
            "question": q,
            "answer": a,
            "header": header or "",
            "created": time.time(),
            "updated": time.time(),
        })
    data["entries"] = entries[:200]
    data["updated"] = time.time()
    _write(MEMORY / f"{canon_scope(project_id)}.json", data)
    return data


def delete_canon_entry(project_id: str | None, entry_id: str) -> dict:
    data = load_canon(project_id)
    data["entries"] = [e for e in (data.get("entries") or []) if e.get("id") != entry_id]
    data["updated"] = time.time()
    _write(MEMORY / f"{canon_scope(project_id)}.json", data)
    return data


def canon_as_prompt(project_id: str | None = None, limit: int = 60) -> str:
    """Established decisions, newest first, injected into the agent system prompt."""
    lines: list[str] = []
    for label, pid in (("Global", None), ("This project", project_id)):
        if label == "This project" and not project_id:
            continue
        entries = (load_canon(pid).get("entries") or [])[:limit]
        if not entries:
            continue
        lines.append(f"## {label} decisions")
        for e in entries:
            head = f"[{e['header']}] " if e.get("header") else ""
            lines.append(f"- {head}{e['question']} → **{e['answer']}**")
    if not lines:
        return ""
    return (
        "# Established canon (already decided, do NOT ask these again)\n"
        + "\n".join(lines)
    )
