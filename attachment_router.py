# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Decide when the model should open files from the chat file library.

Keeps attached docs available for the whole chat, but only nudges a read when
the user asks, or is clearly stuck or referring to the docs. Not every turn.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


_NO_READ = re.compile(
    r"\b(don'?t|do not|no need to|without|skip)\s+(\w+\s+){0,4}"
    r"(read|open|check|look(\s+at)?)\b",
    re.I,
)
_EXPLICIT_READ = re.compile(
    r"\b(read|open|check|review|inspect|summarize|summarise|analyze|analyse|"
    r"go through|look at|look through|skim|parse)\b.{0,48}\b("
    r"file|files|doc|docs|document|documents|attachment|attachments|"
    r"upload|uploads|md|markdown|pdf|note|notes|report|hand.?off|analysis"
    r")\b"
    r"|\b(what does|what'?s in|according to|based on|from|in)\b.{0,40}\b("
    r"file|doc|document|attachment|upload|md|pdf|note|hand.?off|analysis"
    r")\b",
    re.I,
)
_CONFUSED = re.compile(
    r"\b("
    r"not sure|unsure|confused|stuck|lost|no idea|"
    r"what (should|do) i (do|start|try)|"
    r"where (do|should) i (start|begin|look)|"
    r"help me (figure|decide|prioritize|prioritise)|"
    r"don'?t know what to|"
    r"i'?m (lost|stuck|confused)|"
    r"what next|what now|how (do|should) i (proceed|continue)"
    r")\b",
    re.I,
)
_VAGUE_WITH_ATTACH = re.compile(
    r"^\s*("
    r"here|this|that|thoughts\??|take a look|look|check this|pls|please|"
    r"can you (look|check|review)|what do you think|wdyt|help"
    r")[\s.!?]*$",
    re.I,
)


@dataclass
class AttachmentDecision:
    read: bool
    paths: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    reason: str = ""


def _norm_lib(library: list[dict] | None) -> list[dict]:
    out = []
    seen = set()
    for item in library or []:
        if not isinstance(item, dict):
            continue
        path = (item.get("path") or "").strip()
        if not path or path in seen:
            continue
        kind = item.get("kind") or "file"
        if kind in ("image", "screenshot"):
            continue
        seen.add(path)
        out.append({
            "name": item.get("name") or path.rsplit("/", 1)[-1],
            "path": path,
            "kind": kind,
            "size": item.get("size"),
        })
    return out


def _mentioned_items(text: str, library: list[dict]) -> list[dict]:
    tl = (text or "").lower()
    hits = []
    for item in library:
        name = (item.get("name") or "").lower()
        stem = name.rsplit(".", 1)[0] if name else ""
        path = (item.get("path") or "").lower()
        if name and len(name) >= 3 and name in tl:
            hits.append(item)
            continue
        if stem and len(stem) >= 4 and stem in tl:
            hits.append(item)
            continue
        base = path.rsplit("/", 1)[-1]
        if base and len(base) >= 3 and base in tl:
            hits.append(item)
    return hits


def route_attachment_read(
    query: str,
    library: list[dict] | None,
    *,
    new_attachments: list[dict] | None = None,
) -> AttachmentDecision:
    """Return whether this turn should load/read library files."""
    lib = _norm_lib(library)
    if not lib:
        return AttachmentDecision(False, reason="no_library")

    q = (query or "").strip()
    if _NO_READ.search(q):
        return AttachmentDecision(False, reason="user_veto")

    mentioned = _mentioned_items(q, lib)
    if mentioned:
        return AttachmentDecision(
            True,
            paths=[m["path"] for m in mentioned],
            names=[m["name"] for m in mentioned],
            reason="filename_mention",
        )

    if _EXPLICIT_READ.search(q):
        return AttachmentDecision(
            True,
            paths=[m["path"] for m in lib],
            names=[m["name"] for m in lib],
            reason="explicit_read",
        )

    if _CONFUSED.search(q):
        return AttachmentDecision(
            True,
            paths=[m["path"] for m in lib],
            names=[m["name"] for m in lib],
            reason="stuck_or_unsure",
        )

    new_files = _norm_lib(new_attachments)
    if new_files and (not q or _VAGUE_WITH_ATTACH.search(q) or len(q) < 48):
        return AttachmentDecision(
            True,
            paths=[m["path"] for m in new_files],
            names=[m["name"] for m in new_files],
            reason="new_attach_vague",
        )

    return AttachmentDecision(False, reason="no_signal")


def library_prompt_block(library: list[dict] | None) -> str:
    items = _norm_lib(library)
    if not items:
        return ""
    lines = [
        "# Chat file library",
        "These files stay available for this chat. Paths are absolute.",
        "Do not assume you already have their full contents, open or read them when needed "
        "(user asks, refers to them, or you are unsure how to proceed).",
    ]
    for it in items:
        size = it.get("size")
        size_s = f" · {size} bytes" if isinstance(size, int) else ""
        lines.append(f"- {it['name']} [{it.get('kind') or 'file'}]{size_s}\n  path: {it['path']}")
    return "\n".join(lines)


def format_read_nudge(decision: AttachmentDecision, *, agentic: bool) -> str:
    if not decision.read or not decision.paths:
        return ""
    names = ", ".join(decision.names[:6]) or "attached files"
    paths = "\n".join(f"- {p}" for p in decision.paths[:8])
    if agentic:
        return (
            f"\n\n[System: file router → read now ({decision.reason}). "
            f"Call read_file (or list_dir for folders) on:\n{paths}\n"
            f"Targets: {names}]"
        )
    return (
        f"\n\n[System: file router → load now ({decision.reason}): {names}. "
        "Use the attached file contents below.]"
    )


def merge_library(existing: list[dict] | None, incoming: list[dict] | None) -> list[dict]:
    """Upsert by path; keep stable order (existing first)."""
    by_path: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for src in (existing or []), (incoming or []):
        for item in src:
            if not isinstance(item, dict):
                continue
            path = (item.get("path") or "").strip()
            if not path:
                continue
            kind = item.get("kind") or "file"
            if kind in ("image", "screenshot"):
                continue
            entry = {
                "name": item.get("name") or path.rsplit("/", 1)[-1],
                "path": path,
                "kind": kind,
            }
            if item.get("size") is not None:
                entry["size"] = item["size"]
            if path in by_path:
                by_path[path].update({k: v for k, v in entry.items() if v is not None})
            else:
                by_path[path] = entry
                order.append(path)
    return [by_path[p] for p in order]
