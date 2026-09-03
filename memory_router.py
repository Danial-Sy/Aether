# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Explicit memory and cross-chat intent router. Fires only on clear cues."""
from __future__ import annotations

import re
from dataclasses import dataclass

# Order matters: recall phrases checked before store so "remember when" never stores.
RECALL_MEMORY_RE = re.compile(
    r"\b("
    r"do you remember|did you remember|have you remembered|"
    r"what do you remember|what(?:'s| is) (?:in )?my memory|"
    r"remember when|remember that time|from (?:your |my )?memory|"
    r"check (?:your |my )?memory|in (?:your |my )?memories?"
    r")\b",
    re.I,
)

STORE_RE = re.compile(
    r"\b("
    r"remember (?:this|that|it)|memorize(?: this| that| it)?|"
    r"store (?:this|that|it)(?: in(?: lasting)? memory)?|"
    r"save (?:this|that)(?: to(?: lasting)? memory)?|"
    r"add (?:this|that) to(?: lasting)? memory|"
    r"don'?t forget(?: this| that)?|please remember|"
    r"keep (?:this|that) in mind(?: forever| permanently)?"
    r")\b",
    re.I,
)

CROSS_CHAT_RE = re.compile(
    r"\b("
    r"other chat|previous (?:chat|conversation|session)|"
    r"earlier (?:chat|conversation)|last (?:chat|conversation)|"
    r"that (?:other )?chat|past (?:chat|conversation)|"
    r"refer(?:ence)?(?: back)? to (?:that |the |our |a )?(?:chat|conversation)|"
    r"pull up (?:the |that |our )?(?:chat|conversation)|"
    r"look up (?:the |that |our )?(?:chat|conversation)|"
    r"from (?:our|the|that) (?:other )?(?:chat|conversation)|"
    r"what did we (?:discuss|talk|cover)(?: about)? (?:in|during) (?:the|that|our|another) (?:chat|conversation)"
    r")\b",
    re.I,
)

STORE_EXTRACT_RE = re.compile(
    r"^(?:please\s+)?(?:"
    r"remember(?: this| that| it)?|"
    r"memorize(?: this| that| it)?|"
    r"store(?: this| that| it)?(?: in(?: lasting)? memory)?|"
    r"save(?: this| that)?(?: to(?: lasting)? memory)?|"
    r"add(?: this| that)? to(?: lasting)? memory|"
    r"don'?t forget(?: this| that)?|"
    r"keep(?: this| that)? in mind(?: forever| permanently)?"
    r")\s*[:\-]?\s*",
    re.I,
)


@dataclass
class Intents:
    store_memory: bool = False
    recall_memory: bool = False
    cross_chat: bool = False


def classify(text: str) -> Intents:
    q = (text or "").strip()
    if not q:
        return Intents()
    recall = bool(RECALL_MEMORY_RE.search(q))
    # "remember when" and "do you remember" are recall, never a store
    store = bool(STORE_RE.search(q)) and not recall
    cross = bool(CROSS_CHAT_RE.search(q))
    return Intents(store_memory=store, recall_memory=recall, cross_chat=cross)


def extract_store_text(text: str) -> str:
    q = (text or "").strip()
    if not q:
        return ""
    cleaned = STORE_EXTRACT_RE.sub("", q, count=1).strip()
    # Drop trailing politeness
    cleaned = re.sub(r"^(that|this)\s+", "", cleaned, flags=re.I).strip()
    if len(cleaned) < 2:
        return ""
    return cleaned[:2000]


def search_memory_notes(query: str, project_id: str | None = None, limit: int = 8) -> list[dict]:
    from storage import load_memory

    q = (query or "").strip().lower()
    tokens = [t for t in re.findall(r"[a-z0-9_./\\-]{2,}", q) if t not in {
        "do", "you", "remember", "when", "what", "about", "the", "and", "from",
        "memory", "memories", "did", "have", "your", "my", "please", "that", "this",
    }]
    notes = []
    for scope in ["global"] + ([f"project_{project_id}"] if project_id else []):
        mem = load_memory(scope)
        for n in mem.get("notes") or []:
            text = (n.get("text") or "").strip()
            if not text:
                continue
            blob = text.lower()
            if tokens:
                score = sum(blob.count(t) for t in tokens)
                if score <= 0 and q and q not in blob:
                    # soft: any token prefix in note
                    if not any(t in blob for t in tokens):
                        continue
                score = score or 1
            else:
                score = 1
            notes.append({
                "id": n.get("id"),
                "scope": scope,
                "text": text,
                "score": score,
                "created": n.get("created", 0),
            })
    notes.sort(key=lambda x: (-x["score"], -x.get("created", 0)))
    return notes[:limit]


def format_memory_recall(hits: list[dict]) -> str:
    if not hits:
        return (
            "# Memory recall\n"
            "No lasting memory matched this request. "
            "Answer honestly that you do not have that saved in lasting memory "
            "(unless Chat history excerpts below cover it)."
        )
    lines = ["# Memory recall: matched lasting notes"]
    for h in hits:
        lines.append(f"- ({h['scope']}) {h['text']}")
    lines.append(
        "If any note is adjacent but not exact, say what you do remember from these notes. "
        "Do not invent memories that are not listed."
    )
    return "\n".join(lines)
