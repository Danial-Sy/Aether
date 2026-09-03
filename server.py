# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Aether: local Claude-style desktop companion."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import itertools
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

import agent
import attachment_router as ar
import ollama_client as oc
import search_router as sr
import search_web
import storage as st
from config import COMPACT_PROMPT, MODELS, ROOT, SYSTEM_PROMPTS, TITLE_PROMPT, TITLE_MODEL_ID, UPLOADS, VERSION
from config import effort_for as cfg_effort_for
import computer_overlay
import memory_router

app = FastAPI(title="Aether", version=VERSION)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
STATIC = ROOT / "static"
UPLOADS.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOADS)), name="uploads")
_gen_lock = asyncio.Lock()
# A generation that hangs or dies badly used to hold _gen_lock forever, leaving
# every later message with a silent empty stream and a spinner that never stops.
GEN_LOCK_TIMEOUT = 20.0
# chat id -> {"future": Future, "event": dict}. The event is kept so the UI can
# re-read a prompt it never rendered instead of the run stalling until timeout.
_pending_questions: dict[str, dict] = {}


def _park_prompt(key: str, event: dict) -> asyncio.Future:
    """Register an open prompt and return the future its answer resolves."""
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending_questions[key] = {"future": fut, "event": event}
    return fut


def _approval_key(chat: dict) -> str:
    """Which chat id the UI will answer under.

    A sub-agent runs on a synthetic child id the client has never seen, so its
    prompts have to be parked under the parent's id or the answer POST lands on
    nothing and the sub-run waits out the full timeout.
    """
    return str(chat.get("approval_key") or chat.get("id") or "")
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


@app.middleware("http")
async def no_stale_ui(request: Request, call_next):
    """Never let the webview serve a cached app.js/styles.css.

    The desktop shell keeps its cache across restarts, so without this an update
    to the UI can stay invisible after relaunching and look like a missing feature.
    """
    response = await call_next(request)
    if request.url.path.startswith("/static") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


@app.middleware("http")
async def local_origin_only(request: Request, call_next):
    """Reject browser writes originating outside Aether's local UI."""
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and origin not in {
            "http://127.0.0.1:7878",
            "http://localhost:7878",
        }:
            return JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)
    return await call_next(request)


class ChatCreate(BaseModel):
    mode: str = "chat"  # chat | agentic (legacy: code | computer)
    title: str | None = None
    project_id: str | None = None
    # Records the picker's choice at creation, so a new chat cannot reset it.
    agent_model: str | None = None


class ChatPatch(BaseModel):
    title: str | None = None
    reasoning: bool | None = None
    computer_use: bool | None = None
    mode: str | None = None
    project_id: str | None = None
    pinned: bool | None = None
    # Lets the model picker retarget an open chat instead of forcing a new one.
    agent_model: str | None = None


class AttachmentIn(BaseModel):
    name: str
    kind: str = "file"  # file | folder | image | screenshot
    path: str | None = None
    text: str | None = None
    image_b64: str | None = None


class SendMessage(BaseModel):
    content: str
    reasoning: bool | None = None
    model_key: str | None = None
    # "agent" | "coder". Both are real model keys; one runs the whole job.
    agent_model: str | None = None
    effort: str | None = None
    computer_use: bool = False
    think: bool | None = None
    project_id: str | None = None
    web_search: bool = False
    attachments: list[AttachmentIn] = Field(default_factory=list)


class MemoryNote(BaseModel):
    text: str
    tags: list[str] = Field(default_factory=list)


class ProjectCreate(BaseModel):
    name: str
    root: str
    description: str = ""


class SettingsPatch(BaseModel):
    data: dict


_warm_event = asyncio.Event()
_warm_state: dict = {
    "status": "idle",
    "error": None,
    "model": None,
    "label": None,
    "progress": 0,
    "total": 0,
}
_warm_task: asyncio.Task | None = None
_active_gens: dict[str, asyncio.Task] = {}
WARM_ORDER = ("general", "reasoning", "agent", "coder")
# Keys that drive the tool loop. "coder" is faster but blind, so computer use
# always routes to "agent".
AGENTIC_KEYS = ("agent", "coder")
VISION_AGENT_KEY = "agent"


async def _warm_one(key: str, *, keep: bool = True) -> None:
    meta = MODELS[key]
    _warm_state["label"] = meta.get("label") or key
    _warm_state["model"] = meta.get("id")
    await oc.force_load_model(key, num_ctx=2048, keep_alive=(meta.get("keep_alive") or "30m") if keep else "0")


async def _warm_all_models() -> None:
    """Warm the default chat model into VRAM and verify it is resident before opening the UI."""
    if _warm_event.is_set() and _warm_state.get("status") == "ready":
        # Re-verify: a stale "ready" from a previous process must not skip the load
        mid = MODELS["general"]["id"]
        if await oc.is_model_resident(mid):
            return
        _warm_event.clear()
    _warm_state["status"] = "warming"
    _warm_state["error"] = None
    _warm_state["total"] = 1
    _warm_state["progress"] = 1
    try:
        await _warm_one("general", keep=True)
        mid = MODELS["general"]["id"]
        # Poll until Ollama reports the model resident (or timeout)
        for _ in range(40):
            if await oc.is_model_resident(mid):
                break
            await asyncio.sleep(0.25)
        if not await oc.is_model_resident(mid):
            # One more force load attempt
            await _warm_one("general", keep=True)
        if await oc.is_model_resident(mid):
            _warm_state["status"] = "ready"
            _warm_state["label"] = MODELS["general"]["label"]
            _warm_state["model"] = mid
        else:
            _warm_state["status"] = "error"
            _warm_state["error"] = f"{mid} did not stay loaded in VRAM"
    except Exception as e:
        _warm_state["status"] = "error"
        _warm_state["error"] = str(e)
    finally:
        _warm_event.set()


def _ensure_warm_task() -> None:
    global _warm_task
    if _warm_event.is_set() and _warm_state.get("status") == "ready":
        return
    if _warm_task is None or _warm_task.done():
        if _warm_event.is_set() and _warm_state.get("status") != "ready":
            _warm_event.clear()
        _warm_task = asyncio.create_task(_warm_all_models())


@app.on_event("startup")
async def startup() -> None:
    oc.ensure_ollama_sync()
    UPLOADS.mkdir(parents=True, exist_ok=True)
    st.load_settings()
    if not st.list_projects():
        try:
            from config import AI_ROOT
            st.new_project("AI Workspace", str(AI_ROOT), "Forge / models / tools")
        except Exception:
            pass
    _ensure_warm_task()


async def _stop_warm_and_unload() -> list[str]:
    global _warm_task
    task = _warm_task
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    return await oc.unload_all_models()


@app.on_event("shutdown")
async def shutdown() -> None:
    try:
        computer_overlay.set_overlay(False)
    except Exception:
        pass
    try:
        await _stop_warm_and_unload()
    except Exception:
        pass


@app.post("/api/shutdown")
async def api_shutdown():
    """Explicit VRAM unload, called by the desktop shell before the process dies."""
    try:
        computer_overlay.set_overlay(False)
    except Exception:
        pass
    freed = await _stop_warm_and_unload()
    return {"ok": True, "unloaded": freed}


@app.get("/api/warmup")
async def warmup():
    """Block until startup warm finishes. Splash screen awaits this."""
    _ensure_warm_task()
    try:
        await asyncio.wait_for(_warm_event.wait(), timeout=900.0)
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "status": "timeout",
            "error": "Warmup timed out",
            "model": _warm_state.get("model"),
            "label": _warm_state.get("label"),
            "progress": _warm_state.get("progress"),
            "total": _warm_state.get("total"),
        }
    return {
        "ok": _warm_state.get("status") == "ready",
        "status": _warm_state.get("status"),
        "error": _warm_state.get("error"),
        "model": _warm_state.get("model"),
        "label": _warm_state.get("label"),
        "progress": _warm_state.get("progress"),
        "total": _warm_state.get("total"),
    }


@app.get("/api/warmup/status")
async def warmup_status():
    # What Ollama actually holds. The UI cannot infer this after a reload.
    resident: list[str] = []
    try:
        running = await oc.list_running_models()
        names = {(m.get("name") or m.get("model") or "") for m in running}
        for key, meta in MODELS.items():
            mid = meta.get("id") or ""
            if any(n == mid or n.startswith(mid) for n in names):
                resident.append(key)
    except Exception:
        resident = []
    return {
        "resident": resident,
        "ok": _warm_state.get("status") == "ready",
        "status": _warm_state.get("status"),
        "error": _warm_state.get("error"),
        "model": _warm_state.get("model"),
        "label": _warm_state.get("label"),
        "progress": _warm_state.get("progress"),
        "total": _warm_state.get("total"),
        "ready": _warm_event.is_set() and _warm_state.get("status") == "ready",
    }


@app.post("/api/warmup/model/{key}")
async def warmup_model(key: str):
    """Warm a single model (used when switching). Leaves that model loaded."""
    if key not in MODELS:
        raise HTTPException(404, "Unknown model")
    try:
        await _warm_one(key, keep=True)
        return {"ok": True, "key": key, "model": MODELS[key]["id"], "label": MODELS[key]["label"]}
    except Exception as e:
        raise HTTPException(500, str(e))


class AnswerIn(BaseModel):
    answers: dict[str, Any] = Field(default_factory=dict)
    cancelled: bool = False
    # Shell approvals only: "command" or "binary" persists the approval so the
    # same prompt is not shown again.
    remember: str | None = None


@app.post("/api/chats/{cid}/answer")
async def answer_question(cid: str, body: AnswerIn):
    """Resolve an open ask_user prompt so the parked agent loop can continue."""
    entry = _pending_questions.get(cid)
    fut = (entry or {}).get("future")
    if not fut or fut.done():
        return {"ok": False, "reason": "no open question"}
    fut.set_result({
        "answers": body.answers,
        "cancelled": body.cancelled,
        "remember": body.remember,
    })
    return {"ok": True}


@app.get("/api/chats/{cid}/pending")
async def pending_question(cid: str):
    """The prompt this chat is currently parked on, if any.

    The UI polls this while a run streams. An approval card that never rendered
    (a dropped frame, a reload mid-run) is otherwise invisible until the timeout
    expires, which is the failure this endpoint exists to end.
    """
    entry = _pending_questions.get(cid)
    fut = (entry or {}).get("future")
    if not entry or not fut or fut.done():
        return {"pending": None}
    return {"pending": entry.get("event")}


@app.get("/api/canon")
async def api_get_canon(project_id: str | None = None):
    return {
        "global": st.load_canon(None).get("entries") or [],
        "project": (st.load_canon(project_id).get("entries") or []) if project_id else [],
    }


@app.delete("/api/canon/{entry_id}")
async def api_delete_canon(entry_id: str, project_id: str | None = None):
    return st.delete_canon_entry(project_id, entry_id)


@app.post("/api/chats/{cid}/stop")
async def stop_generation(cid: str):
    task = _active_gens.get(cid)
    if task and not task.done():
        task.cancel()
        return {"ok": True, "stopped": True}
    return {"ok": True, "stopped": False}


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
async def health():
    try:
        installed = await oc.list_models()
        names = {m["name"] for m in installed}
    except Exception as e:
        return {"ok": False, "error": str(e), "models": []}
    models = []
    for key, meta in MODELS.items():
        models.append({
            **meta,
            "key": key,
            "installed": any(meta["id"] == n or n.startswith(meta["id"]) for n in names),
        })
    from config import EFFORT, EFFORT_ORDER
    # Ordered low→high so the UI can index the slider straight off this list.
    levels = [{"key": k, **EFFORT[k]} for k in EFFORT_ORDER if k in EFFORT]
    return {
        "ok": True,
        "models": models,
        "settings": st.load_settings(),
        "effort_levels": levels,
    }


@app.get("/api/settings")
async def get_settings():
    return st.load_settings()


@app.patch("/api/settings")
async def patch_settings(body: SettingsPatch):
    return st.save_settings(body.data)


@app.get("/api/chats")
async def api_list_chats(mode: str | None = None):
    return st.list_chats(mode)


@app.post("/api/chats")
async def api_new_chat(body: ChatCreate):
    mode = body.mode
    if mode in ("code", "computer", "agentic"):
        mode = "agentic"
    titles = {"agentic": "New agentic session"}
    title = body.title or titles.get(mode, "New chat")
    chat = st.new_chat(mode=mode, title=title, project_id=body.project_id)
    if mode == "agentic":
        chat["agent_model"] = _agent_model_choice(body.agent_model, DEFAULT_AGENT_KEY)
        chat = st.save_chat(chat)
    return chat


@app.get("/api/chats/{cid}")
async def api_get_chat(cid: str):
    chat = st.get_chat(cid)
    if not chat:
        raise HTTPException(404)
    return chat


@app.patch("/api/chats/{cid}")
async def api_patch_chat(cid: str, body: ChatPatch):
    chat = st.get_chat(cid)
    if not chat:
        raise HTTPException(404)
    patch = body.model_dump(exclude_none=True)
    if "agent_model" in patch:
        patch["agent_model"] = _agent_model_choice(
            patch["agent_model"], chat.get("agent_model") or DEFAULT_AGENT_KEY
        )
    chat.update(patch)
    chat = st.save_chat(chat)
    if body.computer_use is not None:
        try:
            computer_overlay.set_overlay(bool(chat.get("computer_use")))
        except Exception:
            pass
    return chat


@app.delete("/api/chats/{cid}")
async def api_delete_chat(cid: str):
    if not st.delete_chat(cid):
        raise HTTPException(404)
    return {"ok": True}


# One model runs the whole job. "hybrid" swapped models mid-run, which costs a
# full unload and reload on a 24GB card. Legacy chats storing it are migrated.
AGENT_MODEL_CHOICES = AGENTIC_KEYS
# The MoE coder activates ~3B parameters per token, so it emits tool calls far
# faster than the dense 27B. The 27B is selected explicitly when vision is needed.
DEFAULT_AGENT_KEY = "coder"


def _agent_model_choice(requested: str | None, fallback: str) -> str:
    """Clamp the UI's agentic selection to a real model key."""
    if requested == "hybrid":
        return DEFAULT_AGENT_KEY
    return requested if requested in AGENT_MODEL_CHOICES else fallback


def _model_key(chat: dict, reasoning: bool | None = None, model_key: str | None = None) -> str:
    if model_key and model_key in MODELS:
        return model_key
    mode = chat.get("mode") or "chat"
    if mode in ("code", "agentic", "computer"):
        # Computer use needs eyes; coder has none, so it always falls back to agent.
        if chat.get("computer_use"):
            return VISION_AGENT_KEY
        sel = chat.get("agent_model")
        # Chats saved before hybrid was removed still carry it; run them on the
        # executor rather than silently promoting them to the slower dense model.
        return sel if sel in AGENTIC_KEYS else DEFAULT_AGENT_KEY
    use_r = chat.get("reasoning", False) if reasoning is None else reasoning
    return "reasoning" if use_r else "general"


def _resolve_effort(requested: str | None, model_key: str) -> str:
    """Clamp a UI-supplied effort tier, falling back to the model's own default."""
    from config import EFFORT
    tier = (requested or "").strip().lower()
    if tier in EFFORT:
        return tier
    return cfg_effort_for(model_key)


def _apply_effort(meta: dict, opts: dict, effort: str | None = None) -> dict:
    from config import DEFAULT_EFFORT, EFFORT
    # Honour the requested tier; fall back to the model's own default, then global.
    tier = effort or meta.get("effort") or DEFAULT_EFFORT
    e = EFFORT.get(tier) or EFFORT[DEFAULT_EFFORT]
    out = dict(opts)
    base_temp = float(meta.get("temperature", 0.7))
    out["temperature"] = max(0.05, min(1.5, base_temp * float(e["temp_scale"])))
    out["num_predict"] = int(e["num_predict"])
    return out, e


def _chat_has_images(chat: dict) -> bool:
    for m in chat.get("messages") or []:
        if m.get("images"):
            return True
        for att in m.get("attachments") or []:
            if att.get("kind") in ("image", "screenshot") or att.get("has_image") or att.get("image_b64"):
                return True
    return False


def _num_ctx_for(chat: dict, model_key: str, msgs: list[dict] | None = None) -> int:
    limit = _ctx_limit(model_key)
    # Estimate what we will send this turn. Callers inside the agent loop pass the
    # prompt they already rendered rather than paying for a second full render.
    try:
        used = _estimate_chat_tokens(chat, model_key, msgs=msgs)
    except Exception:
        used = 2048
    has_images = _chat_has_images(chat)
    # Short prompts: smaller KV alloc (big win on first-token latency). Grow as chat grows.
    # Vision turns must never land on 4096: one screenshot is ~4.4k tokens alone.
    if has_images or chat.get("computer_use"):
        headroom = 2048 if used < 6000 else (3072 if used < 12000 else 4096)
        floor = 12288 if (has_images and chat.get("computer_use")) else 8192
    else:
        headroom = 1536 if used < 2000 else (3072 if used < 8000 else 4096)
        floor = 2048
    # Headroom must cover the full reply budget. Reserving less than num_predict
    # cuts generation off inside the thinking block, yielding no tool call.
    from config import EFFORT, DEFAULT_EFFORT
    tier = chat.get("effort") or (MODELS.get(model_key) or {}).get("effort") or DEFAULT_EFFORT
    reply_budget = int((EFFORT.get(tier) or EFFORT[DEFAULT_EFFORT]).get("num_predict") or 0)
    headroom = max(headroom, reply_budget + 512)
    want = oc.effective_num_ctx(used, limit, reply_headroom=headroom, floor=floor)
    # Ollama reloads the model whenever num_ctx changes, so keep this monotonic
    # per chat+model: grow when needed, never shrink.
    seen = chat.get("num_ctx_used")
    if not isinstance(seen, dict):
        seen = {}
    # Honor a user lowering the limit on a chat that grew larger previously.
    stored_prev = int(seen.get(model_key) or 0)
    prev = min(stored_prev, limit)
    val = max(want, prev)
    if val != stored_prev:
        seen[model_key] = val
        chat["num_ctx_used"] = seen
    return val


def _ctx_limit(key: str) -> int:
    settings = st.load_settings()
    override = {
        "general": settings.get("chat_context"),
        "reasoning": settings.get("reasoning_context"),
        "agent": settings.get("agent_context"),
        "coder": settings.get("coder_context"),
    }.get(key)
    meta = MODELS[key]
    val = int(override or meta["context"])
    return min(val, int(meta.get("context_max") or val))


# Replaying every tool result verbatim overruns num_ctx, and Ollama truncates the
# FRONT of the prompt when that happens. Keep the newest few in full and compress
# the rest to a receipt. The 8K cap is sized so a whole source slice survives:
# truncating one just sends the model back to read the missing tail.
TOOL_RESULT_MAX_CHARS = 8000
# Compact well below the threshold. Stopping just under it means the next tool
# result trips it again and the prompt prefix is rewritten every step.
COMPACT_LOW_WATER = 0.55
# Marks where earlier turns were folded into a summary. Messages before the
# newest boundary are not rendered, but nothing is ever deleted.
COMPACT_BOUNDARY_KIND = "compact_boundary"
# The cheap reduction tier below summarization: drop old tool calls and results
# from the render, keep the narrative. No model call, so it can fire often.
# Reduction is progressive on purpose. Summarization alone lets the prompt run
# roughly twice as long before anything shrinks it.
DETAIL_HORIZON_KIND = "detail_horizon"
DETAIL_HORIZON_LOW_WATER = 0.55
# Never evict below this many detail turns: the model needs its own last action
# and that action's result to continue at all.
DETAIL_HORIZON_MIN_TURNS = 4


def _boundary_index(msgs: list[dict]) -> int:
    """Index of the newest compaction boundary, or -1 if there has never been one."""
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("kind") == COMPACT_BOUNDARY_KIND:
            return i
    return -1


def _read_identity(name: str, content: str) -> tuple[str, int, int] | None:
    """Identify a read_file result as (path, start, end), or None."""
    if name != "read_file":
        return None
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    path = payload.get("path")
    start, end = payload.get("start"), payload.get("end")
    if not path or not isinstance(start, int) or not isinstance(end, int):
        return None
    return str(path), start, end


def _superseded_reads(window: list[dict]) -> set[int]:
    """Indices (within `window`) of reads a later read has entirely replaced.

    Scoped to the rendered window rather than the whole transcript: the cost is
    bounded by what is actually being sent, and an append-only transcript makes
    the old whole-history scan quadratic for no benefit.

    This is not merely about size. The model re-sweeps a file with nudged bounds
    (1-200, 140-280, then 1-240 …), so five passes over one file arrive as five
    near-identical full copies. Repeating the same content back at the model is
    what teaches it to keep exploring instead of converging.
    """
    superseded: set[int] = set()
    newer: dict[str, list[tuple[int, int]]] = {}
    for idx in range(len(window) - 1, -1, -1):
        m = window[idx]
        if m.get("role") != "tool":
            continue
        name = m.get("name") or ""
        # A write invalidates earlier reads of that file: the content genuinely
        # differs either side of an edit, so stop collapsing across it.
        if name in {"write_file", "append_file", "edit_file"}:
            newer.clear()
            continue
        key = _read_identity(name, m.get("content") or "")
        if key is None:
            continue
        path, start, end = key
        covered = newer.get(path) or []
        if agent.intersection_size(start, end, covered) >= end - start + 1:
            superseded.add(idx)
        else:
            newer[path] = agent.merge_intervals([*covered, (start, end)])
    return superseded


def _horizon_index(msgs: list[dict], after: int = -1) -> int:
    """Index of the newest detail horizon lying after `after`, or -1."""
    for i in range(len(msgs) - 1, after, -1):
        if msgs[i].get("kind") == DETAIL_HORIZON_KIND:
            return i
    return -1


def _advance_detail_horizon(chat: dict, model_key: str, msgs: list[dict] | None = None) -> bool:
    """Drop older tool detail from the render when the prompt gets heavy.

    Anchored to an inserted marker rather than a stored offset, so it keeps the
    correctness of the boundary model (nothing deleted, no index can go stale) while
    restoring the cheap tier that kept prompts in the fast zone. Between two
    insertions the render is append-only, so the KV cache still holds.

    Costs one pass over per-message estimates; the old replay floor re-rendered
    the entire prompt up to 200 times to make the same decision.
    """
    limit = _ctx_limit(model_key)
    threshold = float(st.load_settings().get("auto_compact_at", 0.85))
    high = int(limit * threshold * 0.9)
    used = _estimate_chat_tokens(chat, model_key, msgs=msgs)
    if used <= high:
        return False
    all_msgs = chat.get("messages") or []
    boundary = _boundary_index(all_msgs)
    start = max(boundary, _horizon_index(all_msgs, boundary)) + 1
    detail = [
        i for i in range(start, len(all_msgs))
        if all_msgs[i].get("role") == "tool"
        or (all_msgs[i].get("role") == "assistant" and all_msgs[i].get("tool_calls"))
    ]
    if len(detail) <= DETAIL_HORIZON_MIN_TURNS:
        return False
    cost = {i: oc.estimate_messages_tokens([all_msgs[i]]) for i in detail}
    # Everything that is not evictable detail still has to fit.
    overhead = max(0, used - sum(cost.values()))
    budget = max(0, int(high * DETAIL_HORIZON_LOW_WATER) - overhead)
    kept = 0
    cut = start
    for i in reversed(detail):
        if kept + cost[i] > budget:
            cut = i + 1
            break
        kept += cost[i]
    # Never orphan a tool result from the assistant turn that requested it.
    while cut < len(all_msgs) and all_msgs[cut].get("role") == "tool":
        cut += 1
    if cut <= start or cut >= len(all_msgs):
        return False
    all_msgs.insert(cut, {
        "role": "system",
        "kind": DETAIL_HORIZON_KIND,
        "ts": time.time(),
        "quiet": True,
    })
    return True


def _active_summary(chat: dict) -> str:
    """The summary that currently stands in for everything before the boundary."""
    msgs = chat.get("messages") or []
    idx = _boundary_index(msgs)
    if idx >= 0:
        return (msgs[idx].get("summary") or "").strip()
    # Chats compacted before Phase 2 kept their summary on the chat itself.
    return (chat.get("summary") or "").strip()


def _contains_replay_elision(value: Any) -> bool:
    if isinstance(value, str):
        return agent.REPLAY_ELISION_PREFIX in value
    if isinstance(value, dict):
        return any(_contains_replay_elision(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_replay_elision(item) for item in value)
    return False


def _rewrite_stale_tool_result(name: str, content: str) -> str:
    """Mark historical capability errors as obsolete before replaying a chat."""
    if name != "edit_file":
        return content
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return content
    if (
        isinstance(payload, dict)
        and payload.get("error") == "Too many edit ranges in one batch"
        and int(payload.get("maximum") or 0) < agent.MAX_EDIT_RANGES
    ):
        return json.dumps({
            "tool": "edit_file",
            "obsolete_error": True,
            "previous_maximum": payload.get("maximum"),
            "current_maximum": agent.MAX_EDIT_RANGES,
            "note": (
                "This result came from an older Aether runtime. The limit is now higher; "
                "do not ask the user to split or choose work because of this old error."
            ),
        })
    return content


def _is_plan_deferral(content: str) -> bool:
    """Recognize legacy assistant replies that handed an ordered plan back to the user."""
    text = " ".join((content or "").lower().split())
    if not text:
        return False
    return any(marker in text for marker in (
        "which one?",
        "which one should",
        "pick one",
        "what to do next",
        "tell me which step",
        "tell me what to do next",
        "try narrowing the request to one concrete next step",
        "i'm stopping instead of retrying",
        "i am stopping instead of retrying",
        "the model returned no assistant message or tool call",
    ))


def _is_simple_resume(content: str) -> bool:
    """Return whether a user message only asks an existing job to keep going."""
    return bool(re.fullmatch(
        r"\s*(?:continue|resume|go on|keep going|carry on|proceed|finish it)\s*[.!]*\s*",
        content or "",
        re.IGNORECASE,
    ))


def _tool_succeeded(name: str, result: str) -> bool:
    """Return whether a tool produced a usable new observation or mutation."""
    try:
        payload = json.loads(result) if isinstance(result, str) else result
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or payload.get("error"):
        return False
    if name == "run_shell":
        return payload.get("exit_code") == 0
    validation = payload.get("validation")
    if isinstance(validation, dict) and validation.get("ok") is False:
        return False
    return payload.get("ok") is not False




def _start_new_job(chat: dict) -> None:
    """Drop per-job state when a message starts fresh work."""
    chat["asked_this_job"] = False
    # Written by the implementation_plan tool, which no longer exists. Popped so
    # a chat saved before Phase 1 stops feeding a dead scaffold to the prompt.
    chat.pop("agent_spec", None)
    # Replay anchors to a boundary message, not a stored offset. Dropped from
    # chats that still carry one.
    chat.pop("replay_floor", None)


def _rejection_nudge(rejections: dict[str, dict]) -> str:
    """Restate the constraints the model kept violating, as one directive.

    A rejected call is recoverable information: the tool said exactly what was
    wrong. Repeating that verbatim beats the generic "you are looping" message,
    which told the model to stop without telling it what to do instead.
    """
    lines = []
    for payload in list(rejections.values())[-3:]:
        detail = payload.get("hint") or payload.get("error") or ""
        if not detail:
            continue
        limits = ", ".join(
            f"{k}={payload[k]}" for k in ("requested", "maximum", "maximum_chars")
            if payload.get(k) is not None
        )
        lines.append(f"- {detail}" + (f" ({limits})" if limits else ""))
    if not lines:
        return ""
    return (
        "[agent-loop] A tool rejected your last calls and you resent them unchanged. "
        "Do not send those arguments again. Satisfy these constraints instead:\n"
        + "\n".join(lines)
        + "\nSplit the operation into smaller valid calls yourself and continue the job. "
        "Do not hand this choice back to the user."
    )


def _call_signature(call: dict, project_root: Path | None) -> str:
    """Identity for the repeat guard, including the target file's revision.

    Past the repeat limit the guard replays a cached rejection and never runs
    the tool again. Keyed on arguments alone that is permanent: an anchored edit
    rejected while the file said one thing stayed rejected after the file was
    changed to say exactly what the anchor wanted. Observed directly: the call
    refused ten times as "Anchor text not found" applies cleanly when run.
    Folding the revision in means a changed file is a new call and gets a real
    attempt, while retrying against an unchanged file still counts as a repeat.
    """
    args = call.get("arguments") or {}
    payload: dict[str, Any] = {"n": call.get("name"), "a": args}
    target = args.get("path")
    if isinstance(target, str) and target:
        try:
            resolved, err = agent.resolve_existing(target, project_root, want_dir=False)
            if resolved is not None and not err:
                payload["rev"] = agent._file_revision(resolved)
        except (OSError, ValueError, PermissionError):
            pass
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _current_run_messages(chat: dict) -> list[dict]:
    """Messages belonging to the job the model is working on right now.

    Read coverage must not leak across user turns: content read for a previous
    request is long gone from the prompt, so blocking a fresh read of it would
    starve the new job. Everything from the latest real user message onward is
    this run; loop nudges are internal and do not open a new one.
    """
    msgs = chat.get("messages") or []
    start = 0
    for idx in range(len(msgs) - 1, -1, -1):
        m = msgs[idx]
        if m.get("role") == "user" and not m.get("agent_nudge"):
            start = idx
            break
    return msgs[start:]


def _build_messages(
    chat: dict,
    model_key: str,
    clarify_first: bool = False,
    tool_replay_limit: int | None = None,
) -> list[dict]:
    sys = SYSTEM_PROMPTS[model_key]
    mem = st.memory_as_prompt("global", chat.get("project_id"))
    if mem:
        sys += "\n\n# Memory\n" + mem
    active_summary = _active_summary(chat)
    if active_summary:
        sys += "\n\n# Compacted earlier context\n" + active_summary
    hist = st.history_index(exclude_id=chat.get("id"))
    if hist:
        sys += "\n\n" + hist
    lib_block = ar.library_prompt_block(chat.get("file_library"))
    if lib_block:
        sys += "\n\n" + lib_block
    if model_key in AGENTIC_KEYS:
        sys += agent.tools_system_addon()
        proj = st.get_project(chat["project_id"]) if chat.get("project_id") else None
        if proj:
            sys += f"\n\nActive project: {proj['name']}\nRoot: {proj['root']}\n{proj.get('description') or ''}"
        canon = st.canon_as_prompt(chat.get("project_id"))
        if canon:
            sys += "\n\n" + canon
        if clarify_first:
            sys += (
                "\n\n# Clarify-first is ON\n"
                "Before doing real work on this task, call ask_user once with the questions "
                "whose answers would most change what you build. Skip anything already settled "
                "in the Established canon, and skip anything you could answer yourself with a "
                "tool. If the task is genuinely unambiguous, say so in one line and continue."
            )
        plan = agent.todos_prompt_block(chat)
        if plan:
            sys += "\n\n" + plan
    if chat.get("computer_use"):
        sys += (
            "\n\n# Computer use is ON\n"
            "Screenshots of the desktop may be attached. Inspect them carefully, describe "
            "on-screen text and layout precisely, locate UI elements, and give concrete next "
            "actions (click targets, keys, menu paths). Be explicit that coordinates are "
            "approximate."
        )
    all_msgs = chat.get("messages") or []
    # Everything before the newest boundary is covered by its summary, already in
    # `sys`, so rendering starts just after it. The boundary is a real message,
    # not a stored index, so editing or deleting a turn cannot invalidate it.
    # Between two boundaries the prompt is append-only, which is what lets Ollama
    # reuse the KV cache.
    boundary = _boundary_index(all_msgs)
    render = all_msgs[boundary + 1:]
    msgs: list[dict] = [{"role": "system", "content": sys}]
    # A summary paraphrases, and the model checks its work against the literal
    # request, so re-emit the latest user turn verbatim once it falls behind.
    if boundary >= 0:
        latest_user = _latest_user_index(all_msgs)
        if 0 <= latest_user < boundary:
            msgs.append({
                "role": "user",
                "content": all_msgs[latest_user].get("content") or "",
            })
    # Detail-bearing turns before the newest horizon are dropped from the render.
    # The narrative (user turns, assistant prose, debriefs) is untouched.
    horizon_abs = _horizon_index(all_msgs, boundary)
    detail_from = (horizon_abs - (boundary + 1)) if horizon_abs >= 0 else 0
    superseded = _superseded_reads(render)
    keep_tool_turns: set[int] | None = None
    if tool_replay_limit is not None:
        # Explicit narrowing (the length-truncation recovery lane) deliberately
        # discards history, so a rolling window is what is wanted there.
        tool_turn_idxs = [
            i for i, mm in enumerate(render)
            if mm.get("role") == "assistant" and (mm.get("tool_calls") or [])
        ]
        keep_tool_turns = set(tool_turn_idxs[-max(1, int(tool_replay_limit)):])
    # A boundary can land between an assistant tool_call and its result, which
    # Ollama rejects. Drop the orphan rather than send an invalid sequence.
    pending_tool_calls = False
    for idx, m in enumerate(render):
        role = m.get("role")
        content = m.get("content") or ""
        tool_calls = m.get("tool_calls") or []
        # Older boundaries are pure bookkeeping; only the newest one contributes
        # a summary, and it sits before `render` by construction.
        if m.get("kind") in (COMPACT_BOUNDARY_KIND, DETAIL_HORIZON_KIND):
            continue
        # Older detail: drop the assistant tool-call turn, and the orphan check
        # below then drops its results too.
        if idx < detail_from and (role == "tool" or (role == "assistant" and tool_calls)):
            if role == "assistant":
                pending_tool_calls = False
            continue
        if role == "tool":
            if not pending_tool_calls:
                continue
            content = _rewrite_stale_tool_result(m.get("name") or "", content)
            # Tool role, so the model sees a continuous turn rather than a new
            # request. Everything after the boundary replays in full.
            if idx in superseded:
                body = json.dumps({
                    "note": "Superseded: you read this exact range again later; "
                            "the current content is in the newer result below.",
                })
            else:
                body = content[:TOOL_RESULT_MAX_CHARS]
                if len(content) > TOOL_RESULT_MAX_CHARS:
                    body += "\n…[truncated, re-read a narrower slice if you need more]"
            entry: dict[str, Any] = {"role": "tool", "content": body}
            if m.get("name"):
                entry["tool_name"] = m["name"]
            msgs.append(entry)
            continue
        if role == "assistant":
            # Loop-control placeholders are internal state, never conversation.
            # Dropping them here also heals older transcripts.
            if agent.is_internal_continuation_placeholder(content):
                content = ""
            if tool_calls and keep_tool_turns is not None and idx not in keep_tool_turns:
                pending_tool_calls = False
                continue
            # Interim turns without tool calls hold only UI notes. Replaying one
            # teaches the model to echo it as a final answer.
            if m.get("interim") and not tool_calls:
                continue
            # Heal legacy chats that returned a todo plan as a question. The plan
            # is the authority; replaying the deferral teaches it to ask again.
            if not tool_calls and agent.todos_incomplete(chat) and _is_plan_deferral(content):
                continue
            # A mutation call that copied Aether's replay placeholder never
            # changed the file. Drop the poisoned call/result pair.
            if tool_calls and _contains_replay_elision(tool_calls):
                pending_tool_calls = False
                continue
            # Must keep empty-content assistants that emitted tool_calls; dropping
            # those breaks the assistant→tool sequence required by Ollama.
            if not content and not m.get("images") and not tool_calls:
                continue
            entry = {"role": "assistant", "content": content}
            if m.get("images"):
                entry["images"] = m["images"]
            if tool_calls:
                # Ollama needs thinking replayed with the calls it produced.
                # That round's native thinking only, not the UI work log.
                if m.get("native_thinking"):
                    entry["thinking"] = m["native_thinking"]
                # Keep full arguments in storage for the audit trail, but do not
                # resend entire file bodies on every later agent iteration.
                entry["tool_calls"] = agent.to_ollama_tool_calls(
                    tool_calls, elide_large_inputs=True
                )
            pending_tool_calls = bool(tool_calls)
            msgs.append(entry)
            continue
        if role not in ("user", "system") or not content and not m.get("images"):
            continue
        # Loop nudges must not look like fresh user turns.
        if m.get("agent_nudge"):
            # Skipping this must preserve pending_tool_calls: older transcripts
            # sometimes stored a nudge between the assistant call and tool result.
            continue
        # Chat and reasoning have no file tools, so expand attached files into
        # prompt only when the file router marks this turn (or legacy same-message attach).
        send_content = content
        if m.get("resume_job") or (
            m.get("course_correct") and _is_simple_resume(send_content)
        ):
            send_content = agent.CONTINUE_NUDGE + "\n\n" + send_content
        elif m.get("course_correct"):
            send_content = agent.COURSE_CORRECT_PREFIX + send_content
        if role == "user" and model_key not in AGENTIC_KEYS:
            load_paths = m.get("load_files") or None
            if load_paths:
                extra = _expand_attachments_for_chat_model(
                    m, only_paths=load_paths, library=chat.get("file_library")
                )
                if extra:
                    send_content = (send_content + "\n\n" + extra).strip()
        entry = {"role": role, "content": send_content}
        if m.get("images"):
            entry["images"] = m["images"]
        pending_tool_calls = False
        msgs.append(entry)
    return msgs


# Ratio of Ollama's real prompt_eval_count to our estimate, learned per chat.
# Clamped at 0.75 so a bad ratio cannot claim much extra window, and at 1.00 so
# one saying we underestimate is honoured in full. Underestimating is the
# dangerous direction: Ollama truncates the front of an overflowing prompt.
_ESTIMATE_BIAS_FLOOR = 0.75
_ESTIMATE_BIAS_MIN_SAMPLES = 3


def record_estimate_accuracy(chat: dict, model_key: str, estimated: int, actual: int) -> None:
    """Learn how far our estimate sits from what Ollama actually counted.

    The estimator is deliberately conservative, and measured against real agent
    prompts it ran ~1.33x over. That is not free headroom: it is 42% of the
    allocated context that nothing is ever allowed to use, which is what pushed
    the replay floor into evicting the model's working set. Ollama reports the
    true count on every step, so there is no reason to keep guessing.
    """
    if not isinstance(chat, dict) or estimated <= 0 or not actual or actual <= 0:
        return
    seen = chat.get("estimate_bias")
    if not isinstance(seen, dict):
        seen = {}
    entry = seen.get(model_key) or {}
    ratio = float(entry.get("ratio") or 0.0)
    count = int(entry.get("n") or 0)
    observed = actual / estimated
    # Plain EMA once seeded, so a single odd step cannot move the window much.
    ratio = observed if count == 0 else ratio + (observed - ratio) * 0.25
    seen[model_key] = {"ratio": ratio, "n": count + 1}
    chat["estimate_bias"] = seen


def _estimate_bias(chat: dict, model_key: str) -> float:
    entry = ((chat or {}).get("estimate_bias") or {}).get(model_key) or {}
    if int(entry.get("n") or 0) < _ESTIMATE_BIAS_MIN_SAMPLES:
        return 1.0
    # Keep a small margin over the learned ratio; being slightly over costs
    # headroom, being under costs the run.
    return max(_ESTIMATE_BIAS_FLOOR, min(1.0, float(entry.get("ratio") or 1.0) * 1.06))


def _estimate_chat_tokens(chat: dict, model_key: str, msgs: list[dict] | None = None) -> int:
    """Estimate tokens that will actually be sent (system + memory + summary + turns).

    `msgs` lets a caller that has already rendered the prompt reuse it. Rebuilding
    it here is not free: _build_messages walks the whole transcript, and this used
    to be called 4-200 times per agent step (once per _num_ctx_for, up to 8 across
    compaction passes, and once per iteration of the replay floor's 200-round
    eviction loop). That was pure CPU burned between model calls.
    """
    if msgs is None:
        msgs = _build_messages(chat, model_key)
    used = oc.estimate_messages_tokens(msgs)
    # The schema is sent beside messages and consumes the same KV context.
    if model_key in AGENTIC_KEYS:
        used += oc.estimate_tokens(json.dumps(agent.tool_defs_for(chat), ensure_ascii=False)) + 128
    return int(used * _estimate_bias(chat, model_key))


async def _compact_chat(chat: dict, model_key: str, upto: int) -> str:
    """Summarize messages[:upto] into a structured continuity brief.

    Summarizing with the same model that is running the job matters: the brief is
    the only thing that survives the boundary, and a weaker model produced a prose
    blob that dropped file paths and decisions the run still needed.
    """
    key = model_key if model_key in MODELS else "general"
    meta = await oc.ensure_model_loaded(key)
    ctx = min(_ctx_limit(key), 32768)
    transcript = []
    for m in (chat.get("messages") or [])[:upto]:
        if m.get("kind") == COMPACT_BOUNDARY_KIND:
            # An earlier brief is the best record of what came before it.
            transcript.append(f"[earlier summary]\n{m.get('summary') or ''}")
            continue
        role = (m.get("role") or "?").upper()
        content = (m.get("content") or "")[:6000]
        if m.get("role") == "tool":
            content = f"[{m.get('name') or 'tool'}] {content}"
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            if isinstance(fn, dict):
                content += f"\n[called {fn.get('name')}]"
        if content.strip():
            transcript.append(f"{role}: {content}")
    text = "\n\n".join(transcript)[-100000:]
    summary = await oc.chat_once(
        meta["id"],
        [
            {"role": "system", "content": COMPACT_PROMPT},
            {"role": "user", "content": text},
        ],
        options=oc.model_options(meta, ctx),
        keep_alive="30m",
    )
    body, _ = _split_thinking(summary)
    return (body or summary).strip()


def _latest_user_index(msgs: list[dict]) -> int:
    """Index of the newest real user turn (loop nudges are internal), or -1."""
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if m.get("role") == "user" and not m.get("agent_nudge"):
            return i
    return -1


def _compact_split_point(msgs: list[dict], keep: int) -> int:
    """Where to place a boundary: keep the newest `keep` messages, never splitting
    an assistant tool_call from its results.

    The split deliberately may pass the latest user turn. In a long agent run that
    turn sits at index 0 with nothing but tool churn after it, so clamping the
    split to it meant `upto` was always 0 and compaction could never fire,
    a run could only grow. The request itself is not lost: _build_messages re-emits
    it verbatim after the summary, which is the shape Codex uses (initial context
    + recent user messages + summary).
    """
    upto = max(0, len(msgs) - max(1, keep))
    # Walk back off a tool result so the boundary never orphans one.
    while upto > 0 and msgs[upto].get("role") == "tool":
        upto -= 1
    return max(0, upto)


async def _maybe_auto_compact(chat: dict, model_key: str, emit=None, msgs=None):
    """Fold older turns into a summary when the projected prompt nears the limit.

    Nothing is deleted. A boundary message is inserted into the transcript and
    rendering resumes after it, so the full history stays on disk for resume and
    audit, and no stored offset can go stale. The previous implementation sliced
    chat["messages"] in place and saved, which destroyed the only copy of the run
    and silently invalidated every index into it.
    """
    settings = st.load_settings()
    threshold = float(settings.get("auto_compact_at", 0.85))
    keep = int(settings.get("compact_keep_recent", 10))
    limit = _ctx_limit(model_key)
    did = False

    for _pass in range(4):
        used = _estimate_chat_tokens(chat, model_key, msgs=msgs)
        msgs = None  # only the caller's first render is still valid
        pct = used / max(limit, 1)
        all_msgs = chat.get("messages") or []
        if pct < threshold or len(all_msgs) < 3:
            break
        # Hysteresis: summarize well past the trigger so the following steps can
        # append without immediately compacting again.
        keep_now = max(2, keep - _pass * 4)
        upto = _compact_split_point(all_msgs, keep_now)
        if upto <= 0:
            break
        # Never re-summarize ground already covered by the newest boundary.
        if upto <= _boundary_index(all_msgs):
            break

        if emit and not did:
            await emit({
                "type": "compacting",
                "pct": round(100 * pct, 1),
                "used": used,
                "limit": limit,
            })

        summary = await _compact_chat(chat, model_key, upto)
        if not summary:
            break
        all_msgs.insert(upto, {
            "role": "system",
            "kind": COMPACT_BOUNDARY_KIND,
            "summary": summary,
            "ts": time.time(),
            # The UI hides quiet messages, so the marker never renders as a turn.
            "quiet": True,
        })
        # Mirrored for the context panel and for chats read by older code paths.
        chat["summary"] = summary
        st.save_chat(chat)
        did = True

        # Far enough under the trigger that several append-only steps fit before
        # the next boundary. Otherwise loop with a smaller tail.
        if _estimate_chat_tokens(chat, model_key) <= limit * threshold * COMPACT_LOW_WATER:
            break

    if did and emit:
        used_final = _estimate_chat_tokens(chat, model_key)
        await emit({
            "type": "compacted",
            "summary": (chat.get("summary") or "")[:800],
            "messages": chat.get("messages") or [],
            "context": {
                "used": used_final,
                "limit": limit,
                "pct": round(100 * used_final / max(limit, 1), 1),
            },
        })
    return chat, did


def _split_thinking(content: str) -> tuple[str, str]:
    think = ""
    body = content
    m = re.search(r"<think>(.*?)</think>", content, re.DOTALL | re.IGNORECASE)
    if m:
        think = m.group(1).strip()
        body = (content[: m.start()] + content[m.end() :]).strip()
    else:
        m2 = re.match(r"<think>(.*)$", content, re.DOTALL | re.IGNORECASE)
        if m2 and "</think>" not in content.lower():
            think = m2.group(1).strip()
            body = ""
    return body, think


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _read_attachment_text(path: Path, limit: int = 120_000) -> str:
    if path.is_dir():
        entries = []
        for p in sorted(path.iterdir())[:200]:
            entries.append(("📁 " if p.is_dir() else "📄 ") + p.name)
        return f"[folder {path}]\n" + "\n".join(entries)
    if not path.is_file():
        return f"[missing {path}]"
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
        return f"[image file {path}]"
    try:
        if path.stat().st_size > 2_000_000:
            return f"[file too large: {path}, {path.stat().st_size} bytes]"
        raw = path.read_bytes()[: min(limit + 4096, path.stat().st_size)]
        # Treat as text if mostly decodable UTF-8 / no NUL
        if b"\x00" in raw[:4096]:
            return f"[binary file {path.name}, {path.stat().st_size} bytes; open/inspect with tools if needed]"
        text = raw.decode("utf-8", errors="replace")[:limit]
        return text
    except Exception as e:
        return f"[unreadable {path}: {e}]"


_TEXT_SUFFIXES = {
    ".md", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".csv", ".tsv", ".html", ".htm", ".css", ".scss", ".xml",
    ".svg", ".sh", ".bash", ".zsh", ".ps1", ".bat", ".c", ".h", ".cpp", ".hpp", ".rs",
    ".go", ".java", ".kt", ".swift", ".rb", ".php", ".sql", ".r", ".m", ".mm", ".lua",
    ".vim", ".dockerfile", ".gitignore", ".env", ".log", ".rst", ".tex", ".bib",
}


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _attachment_pointer(name: str, kind: str, path: Path | None) -> str:
    """Short reference for the visible user message. Never dump file bodies here."""
    label = name or (path.name if path else "attachment")
    if path and path.exists():
        try:
            if path.is_dir():
                return f"[Attached folder: {label}]\npath: {path}"
            size = _fmt_size(path.stat().st_size)
            return (
                f"[Attached file: {label}]\n"
                f"path: {path}\n"
                f"size: {size} · kind: {kind or 'file'}\n"
                f"Read this path with tools (or ask to open it); contents are not inlined."
            )
        except Exception:
            pass
    return f"[Attached {kind or 'file'}: {label}]" + (f"\npath: {path}" if path else "")


def _expand_attachments_for_chat_model(
    message: dict,
    *,
    limit: int = 80_000,
    only_paths: list[str] | None = None,
    library: list[dict] | None = None,
) -> str:
    """For non-agentic models (no read_file), inject file text at prompt-build time only."""
    blocks: list[str] = []
    budget = limit
    want = set(only_paths) if only_paths else None
    # Prefer explicit load list; else message attachments; else full library
    sources: list[dict] = []
    if want and library:
        by_path = {str(i.get("path")): i for i in (library or []) if i.get("path")}
        for p in only_paths or []:
            if p in by_path:
                sources.append(by_path[p])
            else:
                sources.append({"name": Path(p).name, "path": p, "kind": "file"})
    else:
        sources = list(message.get("attachments") or [])
    for att in sources:
        if budget <= 0:
            blocks.append("[additional attachments omitted, budget reached]")
            break
        kind = att.get("kind") or "file"
        if kind in ("image", "screenshot") or att.get("has_image"):
            continue
        path_s = att.get("path")
        if not path_s:
            continue
        if want is not None and path_s not in want:
            continue
        p = Path(path_s)
        name = att.get("name") or p.name
        if p.is_dir():
            listing = _read_attachment_text(p, limit=min(8000, budget))
            blocks.append(f"--- folder: {name} ({p}) ---\n{listing}")
            budget -= len(listing)
            continue
        if not p.is_file():
            blocks.append(f"--- missing attachment: {name} ({p}) ---")
            continue
        suffix = p.suffix.lower()
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
            continue
        body = _read_attachment_text(p, limit=min(budget, 60_000))
        blocks.append(f"--- file: {name} ({p}) ---\n{body}")
        budget -= len(body)
    if not blocks:
        return ""
    return "\n\n# Attached files (loaded for this model turn)\n" + "\n\n".join(blocks)


def _sync_file_library(chat: dict, incoming: list[dict] | None = None) -> list[dict]:
    """Persist chat-scoped file library from new attaches + historical message attaches."""
    harvested: list[dict] = []
    for m in chat.get("messages") or []:
        for att in m.get("attachments") or []:
            harvested.append(att)
    lib = ar.merge_library(chat.get("file_library"), harvested)
    if incoming:
        lib = ar.merge_library(lib, incoming)
    chat["file_library"] = lib
    return lib


def _related_chats_block(query: str, exclude_id: str | None, *, force: bool = False) -> str:
    """Attach past-chat excerpts only on explicit cross-chat intent or exact title mention."""
    q = (query or "").strip()
    if not q:
        return ""
    intents = memory_router.classify(q)
    title_hits = []
    ql = q.lower()
    for meta in st.list_chats():
        if exclude_id and meta["id"] == exclude_id:
            continue
        title = (meta.get("title") or "").strip()
        if len(title) >= 5 and title.lower() in ql and not st.is_placeholder_title(title):
            title_hits.append(meta)
    if not force and not intents.cross_chat and not title_hits:
        return ""
    hits = st.search_chats(q, exclude_id=exclude_id, limit=4) if (intents.cross_chat or force) else []
    seen = set()
    blocks = []
    for meta in title_hits + hits:
        cid = meta.get("id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        ex = st.chat_excerpt(cid, max_chars=4500)
        if not ex:
            continue
        blocks.append(f"--- past chat: {ex['title']} [{ex['id']}] ---\n{ex['excerpt']}")
        if len(blocks) >= 3:
            break
    if not blocks:
        if intents.cross_chat:
            return (
                "\n\n# Retrieved past chats\n"
                "No matching other chat was found. Say you do not have that conversation on file."
            )
        return ""
    return "\n\n# Retrieved past chats\n" + "\n\n".join(blocks)


async def _post_reply_jobs(cid: str, model_key: str) -> None:
    """Title and deferred compact, isolated from the chat stream and GPU lock."""
    try:
        final = st.get_chat(cid)
        if not final:
            return
        await _maybe_auto_title(final)
        used = _estimate_chat_tokens(final, model_key)
        limit = _ctx_limit(model_key)
        over = used / max(limit, 1) >= float(st.load_settings().get("auto_compact_at", 0.85))
        if over and len(final.get("messages") or []) >= 3:
            await _maybe_auto_compact(final, model_key, None)
    except Exception:
        pass


async def _heuristic_title(user_text: str) -> str:
    line = (user_text or "").strip().splitlines()[0].strip()
    line = re.sub(r"\s+", " ", line)
    # drop attachment noise
    line = re.sub(r"\[System:.*?\]", "", line).strip()
    words = line.split()
    title = " ".join(words[:6]).strip(" \"'.,:;!?")
    if len(title) > 48:
        title = title[:45].rstrip() + "…"
    return title or "New chat"


async def _maybe_auto_title(chat: dict) -> str | None:
    """Title via tiny CPU model (qwen2.5:0.5b) so the 27B GPU model is never interrupted."""
    if chat.get("title_auto") and not st.is_placeholder_title(chat.get("title")):
        return None
    msgs = chat.get("messages") or []
    user = next((m for m in msgs if m.get("role") == "user" and m.get("content")), None)
    asst = next((m for m in reversed(msgs) if m.get("role") == "assistant" and m.get("content")), None)
    if not user or not asst:
        return None
    title = None
    try:
        snippet = f"USER: {(user.get('content') or '')[:400]}\nASSISTANT: {(asst.get('content') or '')[:400]}"
        # num_gpu=0 means CPU only, so the resident chat model stays in VRAM
        raw = await oc.chat_once(
            TITLE_MODEL_ID,
            [
                {"role": "system", "content": TITLE_PROMPT},
                {"role": "user", "content": snippet},
            ],
            options={"num_ctx": 1024, "num_predict": 24, "temperature": 0.2, "num_gpu": 0},
            keep_alive="0",
        )
        body, _ = _split_thinking(raw)
        title = (body or raw).strip().splitlines()[0].strip().strip("\"'")
        title = re.sub(r"^[Tt]itle:\s*", "", title).strip()
        if len(title) > 60:
            title = title[:57] + "…"
        if not title or len(title) < 2:
            title = None
    except Exception:
        title = None
    if not title:
        title = await _heuristic_title(user.get("content") or "")
    if title and len(title) >= 2:
        chat["title"] = title
        chat["title_auto"] = True
        st.save_chat(chat)
        return title
    return None
    msgs = chat.get("messages") or []
    user = next((m for m in msgs if m.get("role") == "user" and m.get("content")), None)
    asst = next((m for m in reversed(msgs) if m.get("role") == "assistant" and m.get("content")), None)
    if not user or not asst:
        return None
    try:
        meta = await oc.ensure_model_loaded("general")
        snippet = f"USER: {(user.get('content') or '')[:800]}\n\nASSISTANT: {(asst.get('content') or '')[:800]}"
        raw = await oc.chat_once(
            meta["id"],
            [
                {"role": "system", "content": TITLE_PROMPT},
                {"role": "user", "content": snippet},
            ],
            options=oc.model_options(meta, 4096),
            keep_alive="2m",
        )
        body, _ = _split_thinking(raw)
        title = (body or raw).strip().splitlines()[0].strip().strip("\"'")
        title = re.sub(r"^[Tt]itle:\s*", "", title).strip()
        if len(title) > 60:
            title = title[:57] + "..."
        if title and len(title) >= 2:
            chat["title"] = title
            chat["title_auto"] = True
            st.save_chat(chat)
            return title
    except Exception:
        return None
    return None


def _uploads_url(path: Path) -> str | None:
    try:
        path = path.resolve()
        root = UPLOADS.resolve()
        if path.is_file() and (path == root or root in path.parents):
            return f"/uploads/{path.name}"
    except Exception:
        return None
    return None


def _ingest_to_uploads(src: Path) -> Path:
    """Copy an arbitrary local file into UPLOADS so the UI can serve it."""
    UPLOADS.mkdir(parents=True, exist_ok=True)
    src = Path(src)
    if src.stat().st_size > MAX_UPLOAD_BYTES:
        raise ValueError("File is larger than Aether's 25 MB attachment limit")
    dest = UPLOADS / f"{uuid.uuid4().hex[:10]}_{src.name}"
    dest.write_bytes(src.read_bytes())
    return dest


def _prepare_user_message(body: SendMessage, chat: dict | None = None) -> dict:
    parts = [body.content.strip()]
    images: list[str] = []
    attachments_meta = []
    for att in body.attachments:
        meta = {"name": att.name, "kind": att.kind, "path": att.path}
        if att.image_b64:
            raw = att.image_b64
            if "," in raw and raw.startswith("data:"):
                raw = raw.split(",", 1)[1]
            images.append(raw)
            meta["has_image"] = True
            # Persist under uploads if we only have b64 (rare) or attach URL for existing path
            if att.path:
                p = Path(att.path)
                url = _uploads_url(p)
                if not url and p.is_file():
                    try:
                        p = _ingest_to_uploads(p)
                        meta["path"] = str(p)
                        url = _uploads_url(p)
                    except Exception:
                        url = None
                if url:
                    meta["url"] = url
            else:
                try:
                    UPLOADS.mkdir(parents=True, exist_ok=True)
                    dest = UPLOADS / f"paste_{uuid.uuid4().hex[:10]}.png"
                    dest.write_bytes(base64.b64decode(raw))
                    meta["path"] = str(dest)
                    meta["url"] = _uploads_url(dest)
                except Exception:
                    pass
            parts.append(f"\n\n[Attached image: {att.name or 'image'}]")
        elif att.path:
            p = Path(att.path)
            # Keep UI/chat transcript clean: path pointer only (no full file dump).
            if att.kind in ("image", "screenshot") or p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
                try:
                    if not _uploads_url(p) and p.is_file():
                        p = _ingest_to_uploads(p)
                        meta["path"] = str(p)
                    images.append(base64.b64encode(p.read_bytes()).decode("ascii"))
                    meta["has_image"] = True
                    meta["kind"] = att.kind if att.kind in ("image", "screenshot") else "image"
                    url = _uploads_url(p)
                    if url:
                        meta["url"] = url
                    parts.append(f"\n\n[Attached image: {att.name or p.name}]")
                except Exception as e:
                    parts.append(f"\n\n[Attached image failed: {att.name}: {e}]")
            else:
                if p.is_file():
                    meta["size"] = p.stat().st_size
                    meta["kind"] = att.kind or "file"
                elif p.is_dir():
                    meta["kind"] = "folder"
                parts.append("\n\n" + _attachment_pointer(att.name, meta.get("kind") or att.kind, p))
        elif att.text:
            # Explicit pasted snippets only. Keep them short in the visible message.
            snippet = att.text[:500]
            parts.append(f"\n\n--- {att.name} (excerpt) ---\n{snippet}" + ("…" if len(att.text) > 500 else ""))
            meta["text_chars"] = len(att.text)
        attachments_meta.append(meta)

    raw_q = body.content.strip()
    decision = None
    if raw_q:
        decision = sr.route_search(
            raw_q,
            messages=(chat or {}).get("messages") or [],
            last_search_query=(chat or {}).get("last_search_query") or "",
            force_on=bool(body.web_search),
            agentic=_model_key(chat or {}, model_key=body.model_key) in AGENTIC_KEYS,
        )
    if decision and decision.search and decision.query:
        try:
            pack = search_web.run_web_research(decision.query, max_results=6)
            pack["router_reason"] = decision.reason
            parts.append("\n\n" + search_web.format_search_context(pack))
            if chat is not None:
                chat["last_search_query"] = decision.query
        except Exception as e:
            parts.append(f"\n\n[web search failed: {e}]")

    msg = {
        "role": "user",
        "content": "\n".join(parts).strip(),
        "ts": time.time(),
        "attachments": attachments_meta,
    }
    if images:
        msg["images"] = images
    return msg


@app.post("/api/chats/{cid}/send")
async def api_send(cid: str, body: SendMessage):
    chat = st.get_chat(cid)
    if not chat:
        raise HTTPException(404)

    # Model picker / computer-use toggle
    if body.model_key in MODELS:
        mk = body.model_key
        if mk in AGENTIC_KEYS:
            chat["mode"] = "agentic"
            chat["agent_model"] = _agent_model_choice(body.agent_model, mk)
            chat["computer_use"] = bool(body.computer_use)
            chat["reasoning"] = False
        else:
            chat["mode"] = "chat"
            chat["computer_use"] = False
            chat["reasoning"] = mk == "reasoning"
    else:
        if body.reasoning is not None and (chat.get("mode") or "chat") == "chat":
            chat["reasoning"] = body.reasoning
        if chat.get("mode") in ("agentic", "code", "computer"):
            chat["mode"] = "agentic"
            chat["computer_use"] = bool(body.computer_use)

    if body.project_id is not None:
        chat["project_id"] = body.project_id

    if body.effort:
        chat["effort"] = body.effort
    if body.think is not None:
        chat["think"] = bool(body.think)

    # Resume after stop / open plan: treat the next user message as a course-correction
    # on the SAME job (do not restart; do not re-dump huge attachments).
    continuing_job = bool(chat.get("interrupted")) or agent.todos_incomplete(chat)
    if chat.get("interrupted"):
        chat["interrupted"] = False

    user_msg = _prepare_user_message(body, chat)
    # Ensure message ids for later edit/delete
    if not user_msg.get("id"):
        user_msg["id"] = uuid.uuid4().hex[:10]

    # Persist attaches for the whole chat + route whether to read them this turn
    new_atts = list(user_msg.get("attachments") or [])
    lib = _sync_file_library(chat, new_atts)
    decision = ar.route_attachment_read(
        body.content,
        lib,
        new_attachments=new_atts,
    )
    # Mid-job corrections often say "if you read the doc", which must not force a re-read.
    if continuing_job and not new_atts and decision.read and decision.reason != "new_attach_vague":
        decision = ar.AttachmentDecision(False, reason="skip_reread_on_continue")
    model_key_early = _model_key(chat, model_key=body.model_key)
    if decision.read:
        user_msg["load_files"] = decision.paths
        nudge = ar.format_read_nudge(decision, agentic=(model_key_early in AGENTIC_KEYS))
        if nudge:
            user_msg["content"] = (user_msg.get("content") or "") + nudge

    if continuing_job:
        if _is_simple_resume(body.content):
            user_msg["resume_job"] = True
        else:
            user_msg["course_correct"] = True

    intents = memory_router.classify(body.content)
    memory_events: list[dict] = []
    if decision.read:
        memory_events.append({
            "type": "file_router",
            "reason": decision.reason,
            "paths": decision.paths,
            "names": decision.names,
        })

    # Explicit lasting-memory STORE only (remember / memorize / store this…)
    if intents.store_memory:
        note = memory_router.extract_store_text(body.content)
        if note:
            scope = "global"
            if chat.get("project_id") and re.search(r"\bproject\b", body.content, re.I):
                scope = f"project_{chat['project_id']}"
            st.add_memory_note(scope, note)
            memory_events.append({"type": "memory_saved", "scope": scope, "text": note})
            user_msg["content"] = (
                (user_msg.get("content") or "")
                + f"\n\n[System: saved to lasting memory ({scope}): {note}]"
            )

    # Explicit memory RECALL
    if intents.recall_memory:
        hits = memory_router.search_memory_notes(body.content, chat.get("project_id"))
        user_msg["content"] = (user_msg.get("content") or "") + "\n\n" + memory_router.format_memory_recall(hits)
        memory_events.append({"type": "memory_recall", "hits": len(hits)})

    # Cross-chat excerpts only when asked (or title named)
    related = _related_chats_block(body.content, exclude_id=cid)
    if related:
        user_msg["content"] = (user_msg.get("content") or "") + related

    chat.setdefault("messages", []).append(user_msg)
    st.ensure_message_ids(chat)
    st.save_chat(chat)
    model_key = _model_key(chat, model_key=body.model_key)
    effort = _resolve_effort(body.effort or chat.get("effort"), model_key)
    chat["effort"] = effort
    _start_new_job(chat)
    st.save_chat(chat)
    try:
        computer_overlay.set_overlay(bool(chat.get("computer_use")))
    except Exception:
        pass

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        async def emit(ev: dict):
            await queue.put(ev)

        async def run():
            try:
                await asyncio.wait_for(_gen_lock.acquire(), timeout=GEN_LOCK_TIMEOUT)
            except asyncio.TimeoutError:
                await emit({
                    "type": "error",
                    "message": (
                        "Aether is still busy with a previous response and did not free up. "
                        "Press Stop, or restart Aether if this keeps happening."
                    ),
                })
                await queue.put(None)
                return
            try:
                try:
                    await emit({"type": "user_message", "message": user_msg})
                    for mev in memory_events:
                        await emit(mev)
                    chat_local = st.get_chat(cid)
                    chat_local, _ = await _maybe_auto_compact(chat_local, model_key, emit)
                    meta = await oc.ensure_model_loaded(model_key)
                    await emit({"type": "status", "model": meta, "key": model_key, "effort": effort})
                    if model_key in AGENTIC_KEYS:
                        async for ev in _agent_loop(
                            chat_local, meta, model_key, effort, think=chat_local.get("think")
                        ):
                            await emit(ev)
                    else:
                        async for ev in _plain_stream(chat_local, meta, model_key, effort, think=chat_local.get("think")):
                            await emit(ev)
                    final = st.get_chat(cid)
                    used = _estimate_chat_tokens(final, model_key)
                    limit = _ctx_limit(model_key)
                    # Unlock the client now: title and compact must not block sending
                    await emit({
                        "type": "done",
                        "chat_id": cid,
                        "context": {
                            "used": used,
                            "limit": limit,
                            "pct": round(100 * used / max(limit, 1), 1),
                        },
                    })
                    # Fire-and-forget: never blocks the next user message / GPU model
                    asyncio.create_task(_post_reply_jobs(cid, model_key))
                except asyncio.CancelledError:
                    try:
                        stopped = st.get_chat(cid) or chat_local
                        stopped["interrupted"] = True
                        st.save_chat(stopped)
                        await emit({
                            "type": "stopped",
                            "interrupted": True,
                            "todos": agent.todos_progress(stopped.get("agent_todos")),
                        })
                    except Exception:
                        await emit({"type": "stopped"})
                    raise
                except Exception as e:
                    await emit({"type": "error", "message": str(e)})
                finally:
                    await queue.put(None)
            finally:
                _gen_lock.release()

        task = asyncio.create_task(run())
        _active_gens[cid] = task
        finished_ok = False
        try:
            while True:
                ev = await queue.get()
                if ev is None:
                    finished_ok = True
                    break
                yield _sse(ev)
        finally:
            _active_gens.pop(cid, None)
            # Only cancel if the client disconnected mid-stream
            if not finished_ok and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    return StreamingResponse(event_stream(), media_type="text/event-stream")


async def _plain_stream(chat: dict, meta: dict, model_key: str, effort: str | None = None, think: bool | None = None):
    msgs = _build_messages(chat, model_key)
    opts = oc.model_options(meta, _num_ctx_for(chat, model_key))
    resolved_effort = _resolve_effort(effort, model_key)
    opts, effort_meta = _apply_effort(meta, opts, resolved_effort)
    if model_key == "reasoning":
        msgs = list(msgs)
        msgs[0] = {
            **msgs[0],
            "content": msgs[0]["content"] + "\nUse thorough step-by-step reasoning before the final answer.",
        }
    assistant = {
        "role": "assistant",
        "content": "",
        "thinking": "",
        "model": meta["id"],
        "effort": effort,
        "ts": time.time(),
    }
    chat["messages"].append(assistant)
    st.save_chat(chat)
    full = ""
    think_flag = None
    if model_key == "general":
        think_flag = True if think is None else bool(think)
    elif model_key == "reasoning":
        # Respect UI Thinking toggle (default on when unset).
        think_flag = True if think is None else bool(think)
    elif chat.get("computer_use"):
        # VL answers belong in content for UI guidance; thinking burns tokens on screenshots
        think_flag = False if think is None else bool(think)
    # The effort tier is a ceiling: dragging the slider to "low" means no thinking
    # pass at all, whatever the per-model default was.
    if not effort_meta.get("think", True):
        think_flag = False
    async for chunk in oc.stream_chat(meta["id"], msgs, keep_alive=meta["keep_alive"], options=opts, think=think_flag):
        msg = chunk.get("message") or {}
        native = msg.get("thinking") or ""
        delta = msg.get("content") or ""
        if native:
            assistant["thinking"] = (assistant.get("thinking") or "") + native
        if delta:
            full += delta
            body, parsed = _split_thinking(full)
            assistant["content"] = body
            if parsed:
                assistant["thinking"] = parsed
            yield {"type": "delta", "content": body, "thinking": assistant.get("thinking") or ""}
        elif native:
            yield {"type": "delta", "content": assistant.get("content") or "", "thinking": assistant.get("thinking") or ""}
        if chunk.get("done"):
            break
    body, parsed = _split_thinking(full)
    assistant["content"] = body or full
    if parsed:
        assistant["thinking"] = parsed
    st.save_chat(chat)
    yield {"type": "message", "message": assistant}


# Park generously but not forever: the generation lock is held while we wait.
# An unanswered question is cheap to expire because the tool result tells the
# model to assume a default. A denied command derails a run, so shell approvals
# keep a much longer window.
ASK_USER_TIMEOUT = 90.0
# A denied command derails a run, so an approval the user simply has not
# noticed yet must not expire quickly. The dock keeps it visible meanwhile.
SHELL_APPROVAL_TIMEOUT = 300.0

# Set AETHER_TIMING_LOG to a path to append one JSON line of per-step timings.
_TIMING_LOG = os.environ.get("AETHER_TIMING_LOG", "")


def _allowlist_for(settings: dict, project_id: str | None) -> dict:
    """The remembered approvals for one project (plus the global ones)."""
    store = settings.get("shell_allowlist")
    if not isinstance(store, dict):
        return {"commands": [], "binaries": []}
    scope = store.get(project_id or "global") or {}
    shared = store.get("global") or {} if project_id else {}
    return {
        "commands": list(scope.get("commands") or []) + list(shared.get("commands") or []),
        "binaries": list(scope.get("binaries") or []) + list(shared.get("binaries") or []),
    }


def remember_shell_approval(project_id: str | None, command: str, scope: str) -> None:
    """Persist an 'always allow' answer so the same prompt never returns.

    Approving a command once and being asked again is the main reason approval
    prompts feel like noise rather than a safety control.
    """
    if scope not in {"command", "binary"}:
        return
    settings = st.load_settings()
    store = dict(settings.get("shell_allowlist") or {})
    key = project_id or "global"
    entry = dict(store.get(key) or {})
    if scope == "command":
        values = list(entry.get("commands") or [])
        item = (command or "").strip()
    else:
        values = list(entry.get("binaries") or [])
        item = agent.shell_primary_binary(command) or ""
    if item and item not in values:
        values.append(item)
        values = values[-200:]
    entry["commands" if scope == "command" else "binaries"] = values
    store[key] = entry
    st.save_settings({"shell_allowlist": store})


def _shell_gate(settings: dict, command: str, project_id: str | None = None) -> tuple[bool, str]:
    """Decide whether a shell command needs the user's approval.

    Modes: always_ask, safe_auto (auto-run inspection/build/test/install), never_ask.
    Falls back to the legacy confirm_shell_commands flag when no mode is stored.
    Commands the user chose to remember are auto-approved in every mode but
    always_ask, which by definition means "ask me anyway".
    """
    mode = settings.get("shell_approval_mode")
    if mode not in {"always_ask", "safe_auto", "never_ask"}:
        mode = "always_ask" if settings.get("confirm_shell_commands", True) else "never_ask"
    if mode == "never_ask":
        return False, "auto-approve is on for every command"
    if mode == "safe_auto":
        verdict, why = agent.classify_shell(command)
        if verdict == "safe":
            return False, why
        allowed = _allowlist_for(settings, project_id)
        if (command or "").strip() in allowed["commands"]:
            return False, "you approved this exact command before"
        binary = agent.shell_primary_binary(command)
        if binary and binary in allowed["binaries"]:
            return False, f"you always allow {binary}"
        return True, why
    return True, "Aether confirms every command"


def _apply_user_answers(chat: dict, questions: list[dict], reply: dict) -> str:
    """Turn a UI answer payload into a tool result, and record it as canon."""
    answers = reply.get("answers") or {}
    if reply.get("cancelled"):
        return json.dumps({
            "cancelled": True,
            "timed_out": bool(reply.get("timed_out")),
            "note": (
                "The user did not answer. Do not ask again. Make the most reasonable "
                "assumption, state it plainly in your final debrief, and continue."
            ),
        })
    pid = chat.get("project_id")
    out = []
    for q in questions:
        qid = q.get("id") or ""
        raw = answers.get(qid, answers.get(q.get("question") or "", ""))
        if isinstance(raw, list):
            text = ", ".join(str(x) for x in raw if str(x).strip())
        else:
            text = str(raw or "").strip()
        if not text:
            continue
        out.append({"question": q.get("question"), "answer": text})
        try:
            st.record_canon(pid, q.get("question") or "", text, header=q.get("header") or "")
        except Exception:
            pass
    if not out:
        return json.dumps({
            "cancelled": True,
            "note": "No answers were provided. Assume sensible defaults and continue.",
        })
    return json.dumps({
        "answers": out,
        "note": "Recorded as canon. These decisions are settled; do not ask them again.",
    }, indent=2)


# The summary is the whole point: a sub-task that reads 40k tokens must not hand
# 40k tokens back, or delegation costs more than doing the work inline.
SUBAGENT_REPORT_MAX_CHARS = 6000

# Events that park the run on the user. They must reach the client from a
# sub-run too, or the sub-run waits on an answer nobody was asked for.
_PROMPT_EVENTS = frozenset({
    "ask_user", "ask_user_done", "shell_approval", "shell_approval_done",
})


def _allowed_tool_names(chat: dict) -> set[str]:
    """Tools this run may actually execute, not merely the ones it was offered."""
    return {t["function"]["name"] for t in agent.tool_defs_for(chat)}


async def _run_subagent(
    parent: dict,
    args: dict,
    meta: dict,
    model_key: str,
    effort: str | None = None,
):
    """Run one delegated sub-task over a fresh transcript.

    Yields the same event shapes the parent stream uses, plus a final
    `subagent_result` carrying the payload to hand back as the tool result. The
    sub-run's own tool results never reach the parent's messages, which is the
    entire benefit, and the reason the return value is capped.
    """
    task = str(args.get("task") or "").strip()
    wanted = args.get("tools")
    child = {
        "id": f"{parent.get('id') or 'chat'}-sub-{uuid.uuid4().hex[:6]}",
        "mode": parent.get("mode") or "agentic",
        "project_id": parent.get("project_id"),
        "agent_model": parent.get("agent_model"),
        "title": f"Sub-task: {task[:48]}",
        "subagent": {
            "parent": parent.get("id"),
            "task": task,
            "tools": list(wanted) if isinstance(wanted, list) else None,
        },
        # Park sub-run prompts under the parent id. The child id is synthetic and
        # an answer posted to it would resolve nothing.
        "approval_key": parent.get("id"),
        "messages": [{"role": "user", "content": task}],
    }
    yield {"type": "status", "model": {"label": "Delegating a sub-task…"}, "key": model_key}

    # Attempted, not executed: tool_start fires before the allowlist guard.
    report, tried_tools, rounds = "", [], 0
    try:
        # think=False: sub-tasks inspect and report rather than design, and the
        # thinking budget is the same allowance the tool call has to fit in.
        async for event in _agent_loop(child, meta, model_key, effort=effort, think=False):
            kind = event.get("type")
            if kind in _PROMPT_EVENTS:
                # A sub-run blocks on approvals like the parent does. Dropping
                # these leaves it waiting on a card that was never shown.
                yield event
            elif kind == "tool_start":
                tried_tools.append(event.get("name"))
                yield {
                    "type": "status",
                    "model": {"label": f"Sub-task: {event.get('name')}…"},
                    "key": model_key,
                }
            elif kind == "message":
                content = (event.get("message") or {}).get("content") or ""
                if content:
                    report = content
                    rounds = len([m for m in child["messages"] if m.get("role") == "assistant"])
    except Exception as exc:  # a failed sub-task must not kill the parent run
        yield {
            "type": "subagent_result",
            "result": json.dumps({
                "error": f"Sub-task failed: {exc}",
                "hint": "Do this work directly with your own tools.",
            }),
        }
        return

    truncated = len(report) > SUBAGENT_REPORT_MAX_CHARS
    payload = {
        "delegated": True,
        "task": task[:400],
        "report": report[:SUBAGENT_REPORT_MAX_CHARS] or
                  "The sub-task produced no findings.",
        "tools_attempted": tried_tools[:20],
        "rounds": rounds,
        "transcript": child["id"],
    }
    if truncated:
        payload["note"] = (
            "The sub-task's report was longer than the delegation budget and was "
            "cut. Delegate a narrower question if you need the rest."
        )
    yield {"type": "subagent_result", "result": json.dumps(payload)}


async def _agent_loop(chat: dict, meta: dict, model_key: str, effort: str | None = None, think: bool | None = None):
    """Run the native Ollama tool loop until the model returns an assistant message.

    This follows the same mechanical contract used by Ollama's documented agent
    loop and OpenAI Codex: tool calls mean execute-and-follow-up; an assistant
    message with no tool calls means the turn is complete.  There is deliberately
    no global step ceiling.  Deterministic duplicate-call guards stop actual
    cycles without killing long runs that are still making progress.
    """
    settings = st.load_settings()
    repeat_limit = max(2, int(settings.get("agent_repeat_limit", 3)))
    # Identical tool calls repeated across steps mean the model is spinning, not
    # working. The circuit breaker is independent of productive step count.
    call_counts: dict[str, int] = {}
    # Structured rejections keyed by call signature, so a repeat of a call the
    # tool refused is answered with the constraint it violated.
    call_rejections: dict[str, dict] = {}
    repeat_stalls = 0
    no_action_stalls = 0
    # Whether the previous failed sample ran out of clock (as opposed to
    # returning nothing). The two need opposite recovery budgets.
    last_stall_timed_out = False
    # Whether this run has written anything yet. ask_user is allowed before the
    # first mutation and refused after it.
    mutated_this_run = False
    plan_prose_stalls = 0
    plan_repeat_recoveries = 0
    force_action_next = False
    rejection_nudge = ""
    plan_checkpoint_due = False
    # Resume an interrupted plan on the first sample, without waiting for the
    # model to defer once more first.
    plan_continuation_due = agent.todos_incomplete(chat)
    latest_user = next(
        (
            message
            for message in reversed(chat.get("messages") or [])
            if message.get("role") == "user"
        ),
        {},
    )
    # Do not resume with the same oversized replay that just exhausted context.
    # Six turns hold a typical set of source slices and still leave room.
    run_tool_replay_limit: int | None = (
        6
        if agent.todos_incomplete(chat)
        and (
            latest_user.get("course_correct")
            or latest_user.get("resume_job")
            or _is_simple_resume(latest_user.get("content") or "")
        )
        else None
    )
    # No inspection gate and no plan-completion enforcement. Both counted the
    # model's moves and then refused them. Reading "too much" means the working
    # set keeps evaporating, and a task list is a tool, never a gate. See
    # ARCHITECTURE.md for what they cost when they were in place.
    step_timings: list[dict] = []
    work_log: list[str] = []
    proj = st.get_project(chat["project_id"]) if chat.get("project_id") else None
    project_root = Path(proj["root"]) if proj else None
    # Only worth offering where surveying is expensive. Decided once per run,
    # since tool_defs_for cannot reach the project root itself.
    chat["delegate_available"] = agent.project_is_large(project_root)
    # One model per run. Swapping a planner for an executor mid-run costs a full
    # unload and reload, because two 17-19GB models cannot share a 24GB card.
    active_key, active_meta = model_key, meta
    # num_ctx is deliberately not computed here. Sizing the KV cache once from
    # the opening prompt caps long runs and sends them re-exploring.
    agent_effort = _resolve_effort(effort, model_key)
    # Clarify-first: nudge via the system prompt (not a fake user turn) until the
    # model actually calls ask_user, then stop.
    clarify_first = bool(settings.get("clarify_first_turn")) and not chat.get("asked_this_job")
    from config import EFFORT as _EFFORT
    think_flag = bool(think)
    if not (_EFFORT.get(agent_effort) or {}).get("think", True):
        think_flag = False

    def _log(line: str) -> str:
        line = (line or "").strip()
        if not line:
            return "\n".join(work_log)
        work_log.append(line)
        # Cap growth so the dropdown stays usable
        if len(work_log) > 200:
            del work_log[:-180]
        return "\n".join(work_log)

    for step in itertools.count():
        # A budget, not a gate: it never refuses a move, it ends the sub-run and
        # hands the parent whatever was learned.
        if chat.get("subagent") and step >= agent.SUBAGENT_MAX_STEPS:
            # A fresh message: `assistant` from the previous iteration is already
            # in chat["messages"], so reusing it would append a duplicate.
            capped = {
                "role": "assistant",
                "content": (
                    f"Sub-task budget reached after {agent.SUBAGENT_MAX_STEPS} rounds "
                    "without a conclusion. Findings so far are in the tool results above."
                ),
                "thinking": "\n".join(work_log),
                "model": active_meta["id"],
                "ts": time.time(),
                "interim": False,
            }
            chat["messages"].append(capped)
            st.save_chat(chat)
            yield {"type": "message", "message": capped}
            return
        if step == 0 and (chat.get("agent_todos") or []):
            yield {"type": "todos", **agent.todos_progress(chat.get("agent_todos"))}
        action_only = force_action_next
        force_action_next = False
        # A length-truncated sample means there was no room left to serialize a
        # tool call. Narrow replay to the newest four turns, then two.
        if action_only:
            run_tool_replay_limit = max(2, 6 - 2 * no_action_stalls)
        # Render once per step and reuse it for the budget checks. Between two
        # boundaries this render is append-only, so the KV cache holds.
        msgs = _build_messages(
            chat,
            active_key,
            clarify_first=clarify_first,
            tool_replay_limit=run_tool_replay_limit,
        )
        def _rerender():
            return _build_messages(
                chat,
                active_key,
                clarify_first=clarify_first,
                tool_replay_limit=run_tool_replay_limit,
            )

        # Progressive reduction, cheapest first: drop older tool detail, and only
        # summarize if that still leaves the prompt over the limit.
        if _advance_detail_horizon(chat, active_key, msgs=msgs):
            _log("[context] dropped older tool detail to keep the prompt small")
            msgs = _rerender()
        _, compacted = await _maybe_auto_compact(chat, active_key, None, msgs=msgs)
        if compacted:
            _log("[context] compacted earlier turns into a summary")
            msgs = _rerender()
        # Report the window every step, so the meter fills as the run goes
        # instead of jumping at the end. Taken from the prompt just rendered.
        step_used = _estimate_chat_tokens(chat, active_key, msgs=msgs)
        step_limit = _ctx_limit(active_key)
        yield {
            "type": "context",
            "used": step_used,
            "limit": step_limit,
            "pct": round(100 * step_used / max(step_limit, 1), 1),
            "key": active_key,
        }
        # Directives never go in msgs[0]. Editing the system prompt changes the
        # prefix every step and forces the whole conversation to be reprocessed.
        directives: list[str] = []
        if plan_checkpoint_due:
            directives.append(
                "[agent-loop] A project mutation just succeeded. If the current plan milestone "
                "is now complete, call todo_write before starting the next phase. Complete only that "
                "one milestone; Aether promotes the next pending item automatically. If the same "
                "milestone genuinely needs more work, continue it."
            )
            plan_checkpoint_due = False
        if plan_continuation_due:
            active = agent.todos_progress(chat.get("agent_todos")).get("active") or {}
            directives.append(
                "[agent-loop] OPEN PLAN, CONTINUE AUTONOMOUSLY. Do not ask the user which "
                "todo to do; the plan already determines the order. Call the next required tool "
                f"for the active milestone now: {active.get('id')}: {active.get('content')}. "
                "If implementation is complete, "
                "update only the active todo, then continue with the automatically promoted item. "
                "If a tool call was rejected repeatedly, change strategy or split that operation "
                "into valid smaller calls yourself; do not return that choice to the user."
            )
            plan_continuation_due = False
        if rejection_nudge:
            directives.append(rejection_nudge)
            rejection_nudge = ""
        if action_only:
            # A large native tool call is invisible until its JSON is complete.
            # Tell the model to stop narrating and emit the action.
            directives.append(agent.ACTION_ONLY_NUDGE)
        if directives:
            # Steering must not be a system message. Qwen's template hoists every
            # system message to the front, so a "trailing" directive lands in the
            # prefix and voids the cache: 33.8s for seven tokens against 3.5s for
            # 3,101 appended ones. Riding on the newest tool result keeps the
            # prompt append-only.
            text = "\n\n".join(directives)
            if msgs and msgs[-1].get("role") == "tool":
                tail = dict(msgs[-1])
                tail["content"] = (tail.get("content") or "") + "\n\n" + text
                msgs[-1] = tail
            else:
                msgs.append({"role": "user", "content": text})
        # Re-size the KV cache for what we are actually about to send this step.
        opts = oc.model_options(active_meta, _num_ctx_for(chat, active_key, msgs=msgs))
        opts, _ = _apply_effort(active_meta, opts, agent_effort)
        if action_only:
            # Shrinking a retry that already hit the token limit guarantees
            # another truncation. Retry in the full configured context.
            recovery_ctx = max(int(opts.get("num_ctx") or 0), _ctx_limit(active_key))
            opts["num_ctx"] = recovery_ctx
            # Size against the reduced recovery prompt, not the full ten-turn
            # replay that was intentionally discarded above.
            prompt_tokens = oc.estimate_messages_tokens(msgs)
            prompt_tokens += oc.estimate_tokens(
                json.dumps(agent.tool_defs_for(chat), ensure_ascii=False)
            ) + 384
            available = max(2048, recovery_ctx - prompt_tokens - 1024)
            current_predict = int(opts.get("num_predict") or 8192)
            recovery_multiplier = 2 ** max(1, min(no_action_stalls, 2))
            target_predict = min(
                24576,
                max(current_predict, current_predict * recovery_multiplier),
            )
            opts["num_predict"] = min(target_predict, available)
            opts["temperature"] = min(float(opts.get("temperature") or 0.2), 0.1)
            seen = chat.setdefault("num_ctx_used", {})
            if isinstance(seen, dict):
                seen[active_key] = recovery_ctx
        assistant = {
            "role": "assistant",
            "content": "",
            "thinking": "\n".join(work_log),
            "model": active_meta["id"],
            "tool_calls": [],
            "ts": time.time(),
            "step": step,
            "effort": effort,
            "interim": True,
        }
        chat["messages"].append(assistant)
        full = ""
        native_think = ""
        native_calls: list = []
        final_chunk: dict[str, Any] = {}
        base_sample_timeout = max(30, int(settings.get("agent_sample_timeout", 150)))
        # Shrinking the budget is right for a model that returned nothing and
        # wrong for one that ran out of clock: that needs more time, not less.
        # At 82.6 tok/s an 8192-token reply needs ~99s of generation alone.
        reply_budget = int((_EFFORT.get(agent_effort) or {}).get("num_predict") or 2048)
        # Conservative floor: half the measured rate, plus room for prompt eval.
        generation_floor = int(reply_budget / 40) + 60
        if last_stall_timed_out:
            sample_timeout = max(base_sample_timeout, generation_floor)
        else:
            sample_timeout = max(60, base_sample_timeout - no_action_stalls * 30)
        # Splitting time-to-first-token from generation says whether a slow run
        # is paying for reprocessed context or for tokens.
        step_started = time.monotonic()
        try:
            async with asyncio.timeout(sample_timeout):
                async for chunk in oc.stream_chat(
                    active_meta["id"],
                    msgs,
                    keep_alive=active_meta["keep_alive"],
                    options=opts,
                    tools=agent.tool_defs_for(chat),
                    # Only the opening sample thinks. Thinking on every round
                    # eats the whole output budget before a call is emitted.
                    think=bool(think_flag and not action_only and step == 0),
                ):
                    final_chunk = chunk
                    msg = chunk.get("message") or {}
                    if msg.get("tool_calls"):
                        # Calls can arrive across chunks. Replacing this list
                        # discards all but the last.
                        native_calls.extend(msg["tool_calls"])
                    native = msg.get("thinking") or ""
                    if native:
                        native_think += native
                    delta = msg.get("content") or ""
                    if delta:
                        full += delta
                    if native or delta:
                        body, parsed = _split_thinking(full)
                        think_view = "\n".join(work_log)
                        extra = (native_think or parsed or "").strip()
                        if extra:
                            think_view = (think_view + "\n" + extra).strip() if think_view else extra
                        assistant["thinking"] = think_view
                        assistant["content"] = body  # held privately until final
                        yield {
                            "type": "delta",
                            "content": "",
                            "thinking": think_view,
                            "step": step,
                            "quiet": True,
                        }
                    if chunk.get("done"):
                        break
        except asyncio.TimeoutError:
            final_chunk = {
                "done": True,
                "done_reason": f"sample timeout after {sample_timeout}s",
            }

        step_elapsed = time.monotonic() - step_started

        def _secs(key: str) -> float:
            raw = final_chunk.get(key)
            return round(raw / 1e9, 2) if isinstance(raw, (int, float)) else 0.0

        # Ollama's counters, not wall clock: tool calls arrive as one chunk at
        # the end, so measured time-to-first-token includes the generation.
        prompt_evaluated = final_chunk.get("prompt_eval_count")
        step_stats = {
            "step": step,
            "model": active_meta["id"],
            # Tools are sent beside the messages and occupy the same window, so
            # a count that omits them is not what the model receives.
            "prompt_tokens_sent": (
                oc.estimate_messages_tokens(msgs)
                + oc.estimate_tokens(json.dumps(agent.tool_defs_for(chat), ensure_ascii=False))
                + 128
            ),
            # prompt_eval_count is the full prompt length whether or not the
            # cache was reused, so it says nothing about reuse. The same request
            # twice reports an identical count while wall time drops 19.3s to
            # 5.5s. prompt_eval_s is the only signal that tracks it.
            "prompt_eval_count": prompt_evaluated,
            "prompt_eval_s": _secs("prompt_eval_duration"),
            "eval_count": final_chunk.get("eval_count"),
            "eval_s": _secs("eval_duration"),
            "load_s": _secs("load_duration"),
            "total_s": round(step_elapsed, 2),
            "num_ctx": opts.get("num_ctx"),
        }
        step_timings.append(step_stats)
        record_estimate_accuracy(
            chat, active_key, step_stats["prompt_tokens_sent"], prompt_evaluated
        )
        _log(
            f"[timing] step {step}: sent~{step_stats['prompt_tokens_sent']}tok "
            f"prompt={prompt_evaluated}tok ({step_stats['prompt_eval_s']}s"
            f"{'' if step_stats['prompt_eval_s'] < 5 else ' CACHE MISS'}) "
            f"gen={step_stats['eval_count']} ({step_stats['eval_s']}s) "
            f"total={step_stats['total_s']}s"
        )
        if _TIMING_LOG:
            try:
                with open(_TIMING_LOG, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(step_stats) + "\n")
            except OSError:
                pass

        body, parsed = _split_thinking(full)
        content = (body or full or "").strip()
        if agent.is_internal_continuation_placeholder(content):
            content = ""
            _log("[loop guard] discarded an internal continuation placeholder")
        if native_think:
            _log(native_think.strip()[:6000])
        if parsed:
            _log(parsed[:4000])
        assistant["native_thinking"] = (native_think or parsed or "").strip()
        assistant["thinking"] = "\n".join(work_log)
        calls = agent.extract_tool_calls({"content": content, "tool_calls": native_calls})
        calls = agent.dedupe_tool_calls(calls, limit=8)

        assistant["tool_calls"] = [
            {
                "id": c.get("id"),
                "index": c.get("index"),
                "name": c["name"],
                "arguments": c["arguments"],
            }
            for c in calls
        ]

        if calls:
            # A native call is an action, so an earlier empty-generation retry
            # no longer counts against this turn.
            no_action_stalls = 0
            plan_prose_stalls = 0
            # Tool step: fold any narration into thinking; keep content empty for UI
            if content:
                assistant["thinking"] = _log(f"[note] {content[:600]}")
            assistant["content"] = ""
            assistant["interim"] = True
            assistant["thinking"] = "\n".join(work_log)
            st.save_chat(chat)
            yield {"type": "message", "message": {**assistant, "content": "", "thinking": assistant["thinking"]}}
            for call in calls:
                sig = _call_signature(call, project_root)
                call_counts[sig] = call_counts.get(sig, 0) + 1
                if call_counts[sig] > 2:
                    # Hand back a correction instead of the same result again.
                    repeat_stalls += 1
                    # A rejected call is a different failure from a repeated
                    # one. The model needs the constraint it violated, not a
                    # generic "stop repeating".
                    rejection = call_rejections.get(sig)
                    if rejection:
                        correction = {
                            **rejection,
                            "repeated": call_counts[sig],
                            "error": (
                                f"Rejected again. This is attempt {call_counts[sig]} with "
                                "identical arguments, so the constraint below still applies. "
                                "Do not resend this call. Change the arguments to satisfy it: "
                                f"{rejection.get('hint') or rejection.get('error') or 'see fields above'}"
                            ),
                        }
                    else:
                        correction = {
                            "error": (
                                f"You already called {call['name']} with these exact arguments "
                                f"{call_counts[sig]} times and the result has not changed. Stop "
                                "repeating it. Take the next concrete action now: write the file, "
                                "run the test, or give your final answer."
                            )
                        }
                    chat["messages"].append({
                        "role": "tool",
                        "name": call["name"],
                        "content": json.dumps(correction),
                        "ts": time.time(),
                        "quiet": True,
                    })
                    yield {
                        "type": "delta",
                        "content": "",
                        "thinking": _log(f"[loop guard] skipped repeated {call['name']} call"),
                        "quiet": True,
                    }
                    continue
                # tool_start already drives the status line. Logging the activity
                # line here as well printed every tool twice in the thinking log.
                yield {"type": "tool_start", "name": call["name"], "arguments": call["arguments"]}
                # Withholding a schema is advice, not enforcement: run_tool
                # dispatches on name, so a withheld tool still executes.
                if call["name"] not in _allowed_tool_names(chat):
                    result = json.dumps({
                        "error": f"{call['name']} is not available in this sub-task.",
                        "available": sorted(_allowed_tool_names(chat)),
                        "hint": "Use one of the available tools, or report what you have found.",
                    })
                elif call["name"] == "delegate":
                    # Same loop over a fresh transcript, so the sub-task's
                    # reading costs the parent only its summary. The benefit is
                    # context isolation, not parallelism. Price is one cold
                    # prompt evaluation when control returns, 3.0s median.
                    task = str((call.get("arguments") or {}).get("task") or "").strip()
                    if chat.get("subagent"):
                        result = json.dumps({
                            "error": "A sub-agent cannot delegate further.",
                            "hint": "Do this work directly with your own tools.",
                        })
                    elif not task:
                        result = json.dumps({
                            "error": "delegate needs a self-contained `task`.",
                        })
                    else:
                        result = ""
                        async for sub_event in _run_subagent(
                            chat, call.get("arguments") or {}, active_meta, active_key,
                            effort=effort,
                        ):
                            if sub_event.get("type") == "subagent_result":
                                result = sub_event["result"]
                            else:
                                yield sub_event
                        yield {"type": "status", "model": {"label": "Working…"}, "key": active_key}
                elif call["name"] == "ask_user" and mutated_this_run:
                    # Questions belong at the start or not at all. Asking mid-run
                    # strands a half-built change behind a prompt.
                    result = json.dumps({
                        "error": "Too late to ask; you have already started writing files.",
                        "hint": (
                            "Make the most reasonable assumption, finish the job, and state "
                            "what you assumed in your final debrief."
                        ),
                    })
                elif call["name"] == "ask_user":
                    # The only blocking tool: park the loop, hand the questions to the
                    # UI, and wait for a real answer before doing anything else.
                    questions = agent.normalize_questions(call["arguments"])
                    if not questions:
                        result = json.dumps({
                            "error": "ask_user needs at least one question with 2+ options.",
                        })
                    else:
                        answer_key = _approval_key(chat)
                        ask_event = {
                            "type": "ask_user",
                            "questions": questions,
                            "chat_id": answer_key,
                        }
                        fut = _park_prompt(answer_key, ask_event)
                        yield dict(ask_event)
                        yield {"type": "status", "model": {"label": "Waiting for you…"}, "key": active_key}
                        try:
                            reply = await asyncio.wait_for(fut, timeout=ASK_USER_TIMEOUT)
                        except asyncio.TimeoutError:
                            reply = {"answers": {}, "cancelled": True, "timed_out": True}
                        finally:
                            _pending_questions.pop(answer_key, None)
                        result = _apply_user_answers(chat, questions, reply)
                        clarify_first = False
                        chat["asked_this_job"] = True
                        yield {"type": "ask_user_done", "cancelled": bool(reply.get("cancelled"))}
                        yield {"type": "status", "model": {"label": "Working…"}, "key": active_key}
                elif call["name"] == "run_shell" and _shell_gate(
                    settings,
                    str(call.get("arguments", {}).get("command") or ""),
                    chat.get("project_id"),
                )[0]:
                    command = str(call.get("arguments", {}).get("command") or "").strip()
                    why = _shell_gate(settings, command, chat.get("project_id"))[1]
                    answer_key = _approval_key(chat)
                    # A dedicated event, not an ask_user card: an approval is a
                    # yes/no on one command.
                    approval_event = {
                        "type": "shell_approval",
                        "chat_id": answer_key,
                        "command": command[:2000],
                        "binary": agent.shell_primary_binary(command),
                        "why": why,
                    }
                    fut = _park_prompt(answer_key, approval_event)
                    yield dict(approval_event)
                    yield {"type": "status", "model": {"label": "Waiting for approval…"}, "key": active_key}
                    try:
                        reply = await asyncio.wait_for(fut, timeout=SHELL_APPROVAL_TIMEOUT)
                    except asyncio.TimeoutError:
                        reply = {"answers": {}, "cancelled": True, "timed_out": True}
                    finally:
                        _pending_questions.pop(answer_key, None)
                    approved = (
                        not reply.get("cancelled")
                        and reply.get("answers", {}).get("shell_approval") == "Run command"
                    )
                    if approved:
                        remember_shell_approval(
                            chat.get("project_id"), command, str(reply.get("remember") or "")
                        )
                        settings = st.load_settings()
                        result = agent.run_tool(
                            call["name"],
                            call["arguments"],
                            project_root,
                            chat.get("project_id"),
                            chat=chat,
                        )
                    elif reply.get("timed_out"):
                        # Reporting an unanswered prompt as a refusal sent the
                        # model looking for a reason the user never gave.
                        result = json.dumps({
                            "error": "Approval request expired without an answer",
                            "command": command[:400],
                            "hint": (
                                "Nobody answered the prompt; this is not a refusal. If another "
                                "tool can do the same job, use it. Otherwise continue with the "
                                "rest of the plan and say in your debrief that this command is "
                                "still waiting on approval."
                            ),
                        })
                    else:
                        result = json.dumps({
                            "error": "Command denied by the user",
                            "hint": (
                                "The user refused this command. Do not resend it. Achieve the "
                                "goal another way or continue with the rest of the plan."
                            ),
                        })
                    yield {"type": "shell_approval_done", "cancelled": not approved}
                    yield {"type": "status", "model": {"label": "Working…"}, "key": active_key}
                else:
                    result = agent.run_tool(
                        call["name"],
                        call["arguments"],
                        project_root,
                        chat.get("project_id"),
                        chat=chat,
                    )
                # Success is judged on the real result, before any offloading
                # rewrites it into a pointer.
                tool_succeeded = _tool_succeeded(call["name"], result)
                # Oversized output goes to disk and rides in the prompt as
                # head + tail + path. Truncating instead cuts the tail the model
                # needs next and sends it re-reading.
                stored = agent.offload_tool_output(
                    call["name"], result, chat.get("id") or "", len(chat["messages"])
                ) or result
                chat["messages"].append({
                    "role": "tool",
                    "name": call["name"],
                    "content": stored,
                    "ts": time.time(),
                    "quiet": True,
                })
                if not tool_succeeded:
                    try:
                        payload = json.loads(result)
                    except (TypeError, json.JSONDecodeError):
                        payload = None
                    if isinstance(payload, dict) and payload.get("error"):
                        call_rejections[sig] = {
                            k: v for k, v in payload.items()
                            if k in {"error", "hint", "maximum", "requested",
                                     "maximum_chars", "path", "validation", "suggestions"}
                        }
                else:
                    call_rejections.pop(sig, None)
                if tool_succeeded:
                    no_action_stalls = 0
                    plan_repeat_recoveries = 0
                    # A redundant read earlier in a long job is no longer a
                    # consecutive stall once an edit or check actually succeeds.
                    repeat_stalls = 0
                    if call["name"] in {"write_file", "append_file", "edit_file"}:
                        plan_checkpoint_due = bool(chat.get("agent_todos"))
                        # Building has started; questions are closed from here on.
                        mutated_this_run = True
                st.save_chat(chat)
                activity = agent.tool_activity_line(call["name"], call.get("arguments"), result)
                yield {
                    "type": "delta",
                    "content": "",
                    "thinking": _log(activity),
                    "quiet": True,
                }
                # No tool card dumps: the activity is already in the thinking log
                yield {
                    "type": "tool_result",
                    "name": call["name"],
                    "result": "",
                    "quiet": True,
                    "summary": activity,
                }
                if (
                    tool_succeeded
                    and project_root is not None
                    and chat.get("project_id")
                    and call["name"] in {"write_file", "append_file", "edit_file", "run_shell"}
                ):
                    try:
                        tree_result = json.loads(agent.run_tool(
                            "list_dir",
                            {"path": "."},
                            project_root,
                            chat.get("project_id"),
                        ))
                    except Exception:
                        tree_result = {}
                    yield {
                        "type": "project_tree",
                        "project_id": chat.get("project_id"),
                        "entries": tree_result.get("entries") or [],
                    }
                if call["name"] == "todo_write":
                    prog = agent.todos_progress(chat.get("agent_todos"))
                    yield {"type": "todos", **prog}
                    if tool_succeeded:
                        plan_checkpoint_due = False
            if repeat_stalls >= repeat_limit:
                # An open plan is authority to recover on its own. Bounded
                # retries beat asking the user to re-order work already planned.
                if agent.todos_incomplete(chat):
                    plan_repeat_recoveries += 1
                    repeat_stalls = 0
                    if plan_repeat_recoveries <= repeat_limit:
                        plan_continuation_due = True
                        # The plan directive says "change strategy"; this says
                        # which constraint to change it to.
                        rejection_nudge = _rejection_nudge(call_rejections)
                        yield {
                            "type": "status",
                            "model": {"label": "Changing strategy for open plan…"},
                            "key": active_key,
                        }
                        continue
                    assistant["content"] = (
                        "The model repeatedly retried a rejected tool operation without changing "
                        "strategy. Aether stopped this run without changing the open plan."
                    )
                    assistant["interim"] = False
                    assistant["thinking"] = "\n".join(work_log)
                    chat["interrupted"] = True
                    st.save_chat(chat)
                    yield {"type": "delta", "content": assistant["content"], "thinking": assistant["thinking"]}
                    yield {"type": "message", "message": assistant}
                    return
                # No open plan is not a reason to abandon a job. A rejected call
                # carries a constraint, so restate it before giving up.
                if call_rejections:
                    plan_repeat_recoveries += 1
                    repeat_stalls = 0
                    if plan_repeat_recoveries <= repeat_limit:
                        rejection_nudge = _rejection_nudge(call_rejections)
                        yield {
                            "type": "status",
                            "model": {"label": "Changing strategy…"},
                            "key": active_key,
                        }
                        continue
                # With no open plan and nothing actionable left, stop rather than
                # burning every remaining step in silence.
                assistant["content"] = (
                    "I got stuck repeating the same tool call without making progress, so I "
                    "stopped instead of looping. Earlier successful tool changes remain in place. "
                    "Try narrowing the request to one concrete next step."
                )
                assistant["interim"] = False
                assistant["thinking"] = "\n".join(work_log)
                chat["interrupted"] = True
                st.save_chat(chat)
                yield {"type": "delta", "content": assistant["content"], "thinking": assistant["thinking"]}
                yield {"type": "message", "message": assistant}
                return
            continue

        # A normal assistant response ends the turn. The plan does not get a
        # vote: refusing a debrief while milestones are open deadlocks a model
        # that has finished and cannot say so.
        # One exception, about the shape of the reply rather than the plan. Qwen
        # sometimes narrates its next action instead of taking it. That is a
        # failed action, bounded by the same stall budget as an empty response.
        if content and no_action_stalls < 3 and agent.looks_incomplete(content):
            no_action_stalls += 1
            force_action_next = True
            _log(f"[loop] narration instead of an action ({no_action_stalls}/3)")
            if chat.get("messages") and chat["messages"][-1] is assistant:
                chat["messages"].pop()
            st.save_chat(chat)
            yield {
                "type": "status",
                "model": {"label": f"Turning narration into action ({no_action_stalls}/3)…"},
                "key": active_key,
            }
            continue
        # An open plan earns a couple of nudges, then gets out of the way. No
        # nudge at all and a weak model writes a confident wrap-up far too early.
        # The old gate's flaw was aborting the run afterwards; here the budget
        # expires into acceptance and the debrief goes through.
        if content and agent.todos_incomplete(chat) and plan_prose_stalls < 2:
            plan_prose_stalls += 1
            plan_continuation_due = True
            _log(f"[plan] nudged to continue an open plan ({plan_prose_stalls}/2)")
            if chat.get("messages") and chat["messages"][-1] is assistant:
                chat["messages"].pop()
            st.save_chat(chat)
            yield {
                "type": "status",
                "model": {"label": f"Continuing open plan ({plan_prose_stalls}/2)…"},
                "key": active_key,
            }
            continue
        # Past the budget the turn ends on the model's own words and open
        # milestones stay open. Bulk-closing them rewrites the record.
        if content:
            assistant["content"] = content
            assistant["interim"] = False
            assistant["thinking"] = "\n".join(work_log)
            st.save_chat(chat)
            yield {"type": "delta", "content": content, "thinking": assistant["thinking"]}
            yield {"type": "message", "message": assistant}
            return

        # A truncated thinking-only response is a failed sample, not a turn.
        # Retry through progressively larger deterministic action lanes.
        no_action_stalls += 1
        reason = str(final_chunk.get("done_reason") or "empty response")
        # A timeout means mid-generation, not idle, so the retry needs more time
        # rather than less.
        last_stall_timed_out = reason.startswith("sample timeout")
        eval_count = final_chunk.get("eval_count")
        metric = f", {eval_count} generated tokens" if isinstance(eval_count, int) else ""
        assistant["content"] = ""
        assistant["interim"] = True
        assistant["thinking"] = _log(f"[model retry] {reason}{metric}; retrying without thinking")
        yield {"type": "message", "message": {**assistant, "content": ""}}
        if no_action_stalls <= 3:
            # Failed sample, not a turn. Keep its thinking in the work log but
            # never persist a phantom assistant message before the retry.
            if chat.get("messages") and chat["messages"][-1] is assistant:
                chat["messages"].pop()
            st.save_chat(chat)
            force_action_next = True
            yield {
                "type": "status",
                "model": {
                    "label": f"Retrying model action ({no_action_stalls}/3)…"
                },
                "key": active_key,
            }
            continue

        assistant["content"] = (
            "The model returned no assistant message or tool call after four progressively "
            f"smaller recovery prompts ({reason}{metric}), so Aether stopped this turn. "
            "Successful tool changes remain in place."
        )
        assistant["interim"] = False
        assistant["thinking"] = "\n".join(work_log)
        chat["interrupted"] = True
        st.save_chat(chat)
        yield {"type": "delta", "content": assistant["content"], "thinking": assistant["thinking"]}
        yield {"type": "message", "message": assistant}
        return


@app.post("/api/chats/{cid}/compact")
async def api_compact(cid: str):
    """Force-compact a chat (used by tests / recovery). Auto-compact runs on send."""
    chat = st.get_chat(cid)
    if not chat:
        raise HTTPException(404)
    model_key = _model_key(chat)

    async def stream():
        async with _gen_lock:
            yield _sse({"type": "compacting"})
            msgs = chat.get("messages") or []
            if len(msgs) < 2:
                yield _sse({"type": "compacted", "messages": msgs, "summary": chat.get("summary") or ""})
                return
            keep = int(st.load_settings().get("compact_keep_recent", 10))
            upto = _compact_split_point(msgs, keep)
            if upto <= 0:
                yield _sse({"type": "compacted", "messages": msgs, "summary": chat.get("summary") or ""})
                return
            summary = await _compact_chat(chat, model_key, upto)
            # Insert a boundary; never delete. The transcript stays whole on disk.
            msgs.insert(upto, {
                "role": "system",
                "kind": COMPACT_BOUNDARY_KIND,
                "summary": summary,
                "ts": time.time(),
                "quiet": True,
            })
            chat["summary"] = summary
            st.save_chat(chat)
            used = _estimate_chat_tokens(chat, model_key)
            limit = _ctx_limit(model_key)
            yield _sse({
                "type": "compacted",
                "messages": chat.get("messages") or [],
                "summary": (chat.get("summary") or "")[:800],
                "context": {
                    "used": used,
                    "limit": limit,
                    "pct": round(100 * used / max(limit, 1), 1),
                },
            })

    return StreamingResponse(stream(), media_type="text/event-stream")


class MemoryPatch(BaseModel):
    text: str


class ProjectPatch(BaseModel):
    name: str | None = None
    root: str | None = None
    description: str | None = None


class MessagePatch(BaseModel):
    content: str


@app.patch("/api/memory/{scope}/{note_id}")
async def api_patch_memory(scope: str, note_id: str, body: MemoryPatch):
    mem = st.update_memory_note(scope, note_id, body.text)
    if not mem:
        raise HTTPException(404)
    return mem


@app.patch("/api/projects/{pid}")
async def api_patch_project(pid: str, body: ProjectPatch):
    proj = st.update_project(pid, body.model_dump(exclude_none=True))
    if not proj:
        raise HTTPException(404)
    return proj


@app.patch("/api/chats/{cid}/messages/{mid}")
async def api_patch_message(cid: str, mid: str, body: MessagePatch):
    chat = st.update_message(cid, mid, body.content)
    if not chat:
        raise HTTPException(404)
    return chat


class ResubmitBody(BaseModel):
    content: str
    model_key: str | None = None
    agent_model: str | None = None
    think: bool | None = None
    effort: str | None = None


@app.post("/api/chats/{cid}/messages/{mid}/resubmit")
async def api_resubmit_message(cid: str, mid: str, body: ResubmitBody):
    """Edit a user message, drop later turns, and stream a fresh assistant reply."""
    chat = st.edit_and_truncate(cid, mid, body.content)
    if not chat:
        raise HTTPException(404, "Message not found or not a user message")

    if body.model_key in MODELS:
        mk = body.model_key
        if mk in AGENTIC_KEYS:
            chat["mode"] = "agentic"
            chat["agent_model"] = _agent_model_choice(body.agent_model, mk)
            chat["reasoning"] = False
        else:
            chat["mode"] = "chat"
            chat["computer_use"] = False
            chat["reasoning"] = mk == "reasoning"
        st.save_chat(chat)

    if body.think is not None:
        chat["think"] = bool(body.think)
        st.save_chat(chat)

    model_key = _model_key(chat, model_key=body.model_key)
    effort = _resolve_effort(body.effort or chat.get("effort"), model_key)
    # Editing a message restarts the work it asked for, so the scaffold written
    # for the previous attempt must not carry over.
    _start_new_job(chat)
    st.save_chat(chat)
    try:
        computer_overlay.set_overlay(bool(chat.get("computer_use")))
    except Exception:
        pass

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        async def emit(ev: dict):
            await queue.put(ev)

        async def run():
            async with _gen_lock:
                try:
                    chat_local = st.get_chat(cid)
                    chat_local, _ = await _maybe_auto_compact(chat_local, model_key, emit)
                    meta = await oc.ensure_model_loaded(model_key)
                    await emit({"type": "status", "model": meta, "key": model_key, "effort": effort})
                    if model_key in AGENTIC_KEYS:
                        async for ev in _agent_loop(
                            chat_local, meta, model_key, effort, think=chat_local.get("think")
                        ):
                            await emit(ev)
                    else:
                        async for ev in _plain_stream(chat_local, meta, model_key, effort, think=chat_local.get("think")):
                            await emit(ev)
                    final = st.get_chat(cid)
                    used = _estimate_chat_tokens(final, model_key)
                    limit = _ctx_limit(model_key)
                    await emit({
                        "type": "done",
                        "chat_id": cid,
                        "context": {
                            "used": used,
                            "limit": limit,
                            "pct": round(100 * used / max(limit, 1), 1),
                        },
                    })
                    asyncio.create_task(_post_reply_jobs(cid, model_key))
                except asyncio.CancelledError:
                    try:
                        stopped = st.get_chat(cid) or chat_local
                        stopped["interrupted"] = True
                        st.save_chat(stopped)
                        await emit({
                            "type": "stopped",
                            "interrupted": True,
                            "todos": agent.todos_progress(stopped.get("agent_todos")),
                        })
                    except Exception:
                        await emit({"type": "stopped"})
                    raise
                except Exception as e:
                    await emit({"type": "error", "message": str(e)})
                finally:
                    await queue.put(None)

        task = asyncio.create_task(run())
        _active_gens[cid] = task
        finished_ok = False
        try:
            while True:
                ev = await queue.get()
                if ev is None:
                    finished_ok = True
                    break
                yield _sse(ev)
        finally:
            _active_gens.pop(cid, None)
            if not finished_ok and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.delete("/api/chats/{cid}/messages/{mid}")
async def api_delete_message(cid: str, mid: str):
    chat = st.delete_message(cid, mid)
    if not chat:
        raise HTTPException(404)
    return {"ok": True, "chat": chat}


@app.get("/api/memory/{scope}")
async def api_get_memory(scope: str):
    return st.load_memory(scope)


@app.post("/api/memory/{scope}")
async def api_add_memory(scope: str, body: MemoryNote):
    return st.add_memory_note(scope, body.text, body.tags)


@app.delete("/api/memory/{scope}/{note_id}")
async def api_del_memory(scope: str, note_id: str):
    return st.delete_memory_note(scope, note_id)


@app.get("/api/projects")
async def api_list_projects():
    return st.list_projects()


@app.post("/api/projects")
async def api_new_project(body: ProjectCreate):
    root = Path(body.root)
    if not root.exists():
        raise HTTPException(400, f"Path does not exist: {body.root}")
    return st.new_project(body.name, str(root.resolve()), body.description)


@app.get("/api/projects/{pid}")
async def api_get_project(pid: str):
    p = st.get_project(pid)
    if not p:
        raise HTTPException(404)
    return p


@app.delete("/api/projects/{pid}")
async def api_del_project(pid: str):
    if not st.delete_project(pid):
        raise HTTPException(404)
    return {"ok": True}


@app.get("/api/projects/{pid}/tree")
async def api_project_tree(pid: str, path: str = "."):
    proj = st.get_project(pid)
    if not proj:
        raise HTTPException(404)
    listing = agent.run_tool("list_dir", {"path": path}, Path(proj["root"]), pid)
    return json.loads(listing)


@app.get("/api/context/{cid}")
async def api_context(cid: str):
    chat = st.get_chat(cid)
    if not chat:
        raise HTTPException(404)
    key = _model_key(chat)
    meta = MODELS[key]
    used = _estimate_chat_tokens(chat, key)
    limit = _ctx_limit(key)
    settings = st.load_settings()
    setting_key = {
        "general": "chat_context",
        "reasoning": "reasoning_context",
        "agent": "agent_context",
        "coder": "coder_context",
    }.get(key, "chat_context")
    return {
        "used": used,
        "limit": limit,
        "pct": round(100 * used / max(limit, 1), 1),
        "model": meta,
        "key": key,
        "setting_key": setting_key,
        "context_max": int(meta.get("context_max") or limit),
        "has_summary": bool(chat.get("summary")),
    }


@app.post("/api/computer-overlay")
async def api_computer_overlay(body: dict):
    enabled = bool(body.get("enabled"))
    try:
        return computer_overlay.set_overlay(enabled)
    except Exception as e:
        return {"ok": False, "enabled": False, "error": str(e)}


@app.get("/api/computer-overlay")
async def api_computer_overlay_get():
    return {"enabled": computer_overlay.is_active()}


@app.post("/api/pick-folder")
async def api_pick_folder():
    import asyncio

    import native_picker

    try:
        path = await asyncio.to_thread(native_picker.pick_folder, "Select project folder")
    except Exception as e:
        raise HTTPException(500, str(e)) from e
    if not path:
        return {"ok": False, "path": None}
    return {"ok": True, "path": str(path)}


@app.post("/api/pick-files")
async def api_pick_files():
    import asyncio

    import native_picker

    try:
        paths = await asyncio.to_thread(native_picker.pick_files, "Select files")
    except Exception as e:
        raise HTTPException(500, str(e)) from e
    return {"ok": True, "paths": [str(p) for p in (paths or [])]}

@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    UPLOADS.mkdir(parents=True, exist_ok=True)
    name = file.filename or f"upload-{uuid.uuid4().hex[:8]}"
    dest = UPLOADS / f"{uuid.uuid4().hex[:10]}_{Path(name).name}"
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File is larger than Aether's 25 MB attachment limit")
    dest.write_bytes(data)
    kind = "image" if Path(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"} else "file"
    out: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "path": str(dest),
        "size": len(data),
        "url": f"/uploads/{dest.name}",
    }
    if kind == "image":
        out["image_b64"] = base64.b64encode(data).decode("ascii")
    return out


class IngestPath(BaseModel):
    path: str


@app.post("/api/ingest-path")
async def api_ingest_path(body: IngestPath):
    src = Path(body.path)
    if not src.is_file():
        raise HTTPException(404, "File not found")
    dest = _ingest_to_uploads(src)
    kind = "image" if dest.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"} else "file"
    out: dict[str, Any] = {
        "name": src.name,
        "kind": kind,
        "path": str(dest),
        "size": dest.stat().st_size,
        "url": f"/uploads/{dest.name}",
    }
    if kind == "image":
        out["image_b64"] = base64.b64encode(dest.read_bytes()).decode("ascii")
    return out


@app.post("/api/screenshot")
async def api_screenshot():
    try:
        from PIL import ImageGrab
    except ImportError as e:
        raise HTTPException(500, "Pillow not installed") from e
    img = ImageGrab.grab()
    UPLOADS.mkdir(parents=True, exist_ok=True)
    dest = UPLOADS / f"shot_{uuid.uuid4().hex[:10]}.png"
    img.save(dest, format="PNG")
    raw = dest.read_bytes()
    return {
        "name": dest.name,
        "kind": "screenshot",
        "path": str(dest),
        "url": f"/uploads/{dest.name}",
        "image_b64": base64.b64encode(raw).decode("ascii"),
        "size": len(raw),
    }


@app.post("/api/search")
async def api_search(q: str = Form(...)):
    try:
        return search_web.run_web_research(q)
    except Exception as e:
        raise HTTPException(500, str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=7878, reload=False)
