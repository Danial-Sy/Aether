# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Ollama lifecycle + streaming chat helpers."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from config import MODELS, OLLAMA_EXE, OLLAMA_HOST, OLLAMA_MODELS

_loaded_model: str | None = None


# Applied to `ollama serve` (and the systemd unit on Linux).
# Windows WDDM overcommits VRAM→RAM by default; Linux CUDA does not unless
# llama.cpp unified memory is on. KV q8_0 is what makes 64k–256k viable on 24 GB.
OLLAMA_SERVE_ENV = {
    "OLLAMA_KV_CACHE_TYPE": "q8_0",
    "OLLAMA_FLASH_ATTENTION": "1",
    "OLLAMA_NUM_PARALLEL": "1",
    "GGML_CUDA_ENABLE_UNIFIED_MEMORY": "1",
}


def ollama_env() -> dict:
    env = dict(os.environ)
    if OLLAMA_MODELS:
        env["OLLAMA_MODELS"] = str(OLLAMA_MODELS)
    env["OLLAMA_HOST"] = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")
    for key, default in OLLAMA_SERVE_ENV.items():
        env[key] = env.get(key, default)
    extra = []
    parent = Path(OLLAMA_EXE).parent
    if parent.exists():
        extra.append(str(parent))
        lib = parent / "lib" / "ollama"
        if lib.exists():
            extra.append(str(lib))
    if extra:
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env


def linux_ollama_unit_has_spillover() -> bool:
    """True when the systemd ollama unit already has unified-memory spillover."""
    if sys.platform == "win32":
        return True
    try:
        out = subprocess.check_output(
            ["systemctl", "show", "ollama", "-p", "Environment", "--no-pager"],
            text=True,
            timeout=5,
        )
    except Exception:
        return False
    return "GGML_CUDA_ENABLE_UNIFIED_MEMORY=1" in out


def ensure_linux_ollama_spillover() -> bool:
    """Install a systemd drop-in so VRAM overflow pages to RAM (Windows-like)."""
    if sys.platform == "win32" or linux_ollama_unit_has_spillover():
        return True
    installer = Path(__file__).resolve().parent / "install-ollama-env.sh"
    if not installer.is_file():
        return False
    helpers = []
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        helpers.append(["pkexec", str(installer)])
    helpers.append(["sudo", "-n", str(installer)])
    for cmd in helpers:
        try:
            r = subprocess.run(cmd, timeout=120)
            if r.returncode == 0 and linux_ollama_unit_has_spillover():
                return True
        except Exception:
            continue
    return linux_ollama_unit_has_spillover()


async def wait_up(timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.time() < deadline:
            try:
                r = await client.get(f"{OLLAMA_HOST}/api/tags")
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
    return False


def ensure_ollama_sync() -> None:
    ensure_linux_ollama_spillover()
    try:
        import urllib.request
        urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=2)
        return
    except Exception:
        pass
    kwargs: dict[str, Any] = {"env": ollama_env()}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([str(OLLAMA_EXE), "serve"], **kwargs)
    for _ in range(40):
        try:
            import urllib.request
            urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=2)
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("Ollama failed to start")


async def list_models() -> list[dict]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{OLLAMA_HOST}/api/tags")
        r.raise_for_status()
        return r.json().get("models", [])


async def list_running_models() -> list[dict]:
    """Models currently loaded in Ollama (/api/ps)."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{OLLAMA_HOST}/api/ps")
            r.raise_for_status()
            return r.json().get("models", []) or []
    except Exception:
        return []


async def is_model_resident(model_id: str) -> bool:
    running = await list_running_models()
    for m in running:
        name = m.get("name") or m.get("model") or ""
        if name == model_id or name.startswith(model_id) or model_id.startswith(name.split(":")[0]):
            return True
    return False


async def model_ready(model_id: str) -> bool:
    models = await list_models()
    names = {m.get("name") for m in models}
    return any(model_id == n or n.startswith(model_id) for n in names)


async def unload_model(model_id: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            await client.post(
                f"{OLLAMA_HOST}/api/generate",
                json={"model": model_id, "keep_alive": 0, "prompt": ""},
            )
    except Exception:
        pass


async def _wait_unloaded(timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not await list_running_models():
            return True
        await asyncio.sleep(0.15)
    return not await list_running_models()


def _wait_unloaded_sync(client: httpx.Client, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = client.get(f"{OLLAMA_HOST}/api/ps")
            running = (r.json().get("models") or []) if r.status_code == 200 else []
        except Exception:
            running = []
        if not running:
            return True
        time.sleep(0.15)
    return False


async def unload_all_models() -> list[str]:
    """Drop every resident Ollama model from VRAM and wait until /api/ps is empty."""
    global _loaded_model
    freed: list[str] = []
    running = await list_running_models()
    names = []
    for m in running:
        name = m.get("name") or m.get("model")
        if name:
            names.append(name)
    # Also clear anything we tracked locally
    if _loaded_model and _loaded_model not in names:
        names.append(_loaded_model)
    for name in names:
        await unload_model(name)
        freed.append(name)
    await _wait_unloaded()
    _loaded_model = None
    return freed


def unload_all_models_sync() -> list[str]:
    """Sync VRAM unload for desktop shutdown. Blocks until Ollama reports empty."""
    global _loaded_model
    freed: list[str] = []
    try:
        with httpx.Client(timeout=30.0) as client:
            try:
                r = client.get(f"{OLLAMA_HOST}/api/ps")
                r.raise_for_status()
                running = r.json().get("models", []) or []
            except Exception:
                running = []
            names = []
            for m in running:
                name = m.get("name") or m.get("model")
                if name:
                    names.append(name)
            if _loaded_model and _loaded_model not in names:
                names.append(_loaded_model)
            for name in names:
                try:
                    client.post(
                        f"{OLLAMA_HOST}/api/generate",
                        json={"model": name, "keep_alive": 0, "prompt": ""},
                    )
                    freed.append(name)
                except Exception:
                    pass
            _wait_unloaded_sync(client)
    except Exception:
        pass
    _loaded_model = None
    return freed


async def ensure_model_loaded(key: str) -> dict:
    global _loaded_model
    meta = MODELS[key]
    mid = meta["id"]
    if not await model_ready(mid):
        raise RuntimeError(f"Model not installed: {mid}")
    if _loaded_model and _loaded_model != mid:
        await unload_model(_loaded_model)
        await asyncio.sleep(1.0)
    _loaded_model = mid
    return meta


async def force_load_model(key: str, *, num_ctx: int = 2048, keep_alive: str | None = None) -> dict:
    """Actually pull weights into VRAM with a 1-token generate (ensure_model_loaded alone does not)."""
    meta = await ensure_model_loaded(key)
    mid = meta["id"]
    ka = keep_alive or meta.get("keep_alive") or "30m"
    payload = {
        "model": mid,
        "prompt": ".",
        "stream": False,
        "keep_alive": ka,
        "options": {"num_ctx": int(num_ctx), "num_predict": 1, "temperature": 0},
    }
    async with httpx.AsyncClient(timeout=600.0) as client:
        r = await client.post(f"{OLLAMA_HOST}/api/generate", json=payload)
        r.raise_for_status()
    return meta


# Chars per token. Prose sits near 4.0, but code and tool-result JSON tokenize
# far denser because punctuation and indentation each tend to cost a token. A
# flat 4.0 underestimated real transcripts by 2.3-2.8x, and underestimating is
# what makes compaction fire late and num_ctx come out too small. The prompt
# then overflows and Ollama truncates it from the front, dropping the system
# prompt and the plan, which is what made long runs forget and restart.
_CPT_PROSE = 4.0
_CPT_DENSE = 1.7
_DENSITY_SATURATION = 0.25
# Scanning a whole 100k-char prompt every step is wasted work; the symbol
# density of a large body is stable well before that.
_DENSITY_SAMPLE = 20_000


def symbol_density(text: str) -> float:
    """Share of non-alphanumeric, non-space characters: how code-like the text is."""
    sample = text[:_DENSITY_SAMPLE]
    if not sample:
        return 0.0
    symbols = sum(1 for ch in sample if not ch.isalnum() and not ch.isspace())
    return symbols / len(sample)


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    density = min(symbol_density(text), _DENSITY_SATURATION)
    ratio = density / _DENSITY_SATURATION
    cpt = _CPT_PROSE - (_CPT_PROSE - _CPT_DENSE) * ratio
    return max(1, int(len(text) / cpt))


def estimate_image_tokens(image_b64: str | None = None) -> int:
    """Desktop screenshots are ~3–5k tokens; 800 was far too low and caused 4096 OOMs."""
    if not image_b64:
        return 4800
    # Rough: longer base64 ≈ higher-res / more patches. Cap so one shot can't force 128k alone.
    nbytes = len(image_b64)
    if nbytes < 80_000:
        return 1600
    if nbytes < 400_000:
        return 3200
    if nbytes < 1_500_000:
        return 4800
    return 6400


def estimate_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        total += estimate_tokens(m.get("content") or "")
        total += 8
        if m.get("thinking"):
            total += estimate_tokens(m["thinking"])
        for img in m.get("images") or []:
            total += estimate_image_tokens(img if isinstance(img, str) else None)
        for att in m.get("attachments") or []:
            total += estimate_tokens(att.get("text") or "") + 20
            if att.get("kind") in ("image", "screenshot") or att.get("has_image"):
                total += estimate_image_tokens(att.get("image_b64"))
        # tool_calls are part of the prompt too. write_file carries an entire file
        # in its arguments, so skipping these under-sized num_ctx badly enough that
        # Ollama truncated the user message away and failed with a 500.
        total += estimate_tokens(m.get("tool_name") or m.get("name") or "")
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            fn = fn if isinstance(fn, dict) else (tc if isinstance(tc, dict) else {})
            total += estimate_tokens(str(fn.get("name") or ""))
            args = fn.get("arguments")
            if not isinstance(args, str):
                try:
                    args = json.dumps(args or {}, ensure_ascii=False)
                except Exception:
                    args = str(args or "")
            total += estimate_tokens(args) + 8
    return total


def effective_num_ctx(
    estimated_prompt_tokens: int,
    ctx_limit: int,
    reply_headroom: int = 4096,
    *,
    floor: int = 2048,
) -> int:
    """Use only as much KV as needed. Full 128K/256K alloc is what makes short chats feel slow."""
    floor = max(2048, int(floor))
    # The prompt size is an estimate (chars/4), so size against a padded figure.
    # Undersizing makes Ollama truncate the front of the conversation and fail
    # with "no user query found in messages"; overshooting only costs some KV.
    padded = int(int(estimated_prompt_tokens) * 1.15) + 256
    need = max(floor, padded + int(reply_headroom))
    buckets = (2048, 4096, 8192, 12288, 16384, 24576, 32768, 49152, 65536, 98304, 131072, 196608, 262144)
    limit = max(floor, int(ctx_limit))
    for b in buckets:
        if need <= b:
            return min(max(b, floor), limit)
    return limit


def model_options(meta: dict, num_ctx: int | None = None) -> dict:
    opts = {
        "temperature": meta.get("temperature", 0.7),
        "top_p": meta.get("top_p", 0.9),
        "top_k": meta.get("top_k", 40),
        "repeat_penalty": meta.get("repeat_penalty", 1.05),
        "num_ctx": int(num_ctx or meta.get("context") or 8192),
    }
    return opts


def _grow_num_ctx(options: dict) -> dict | None:
    """Next KV bucket up, or None when already at the ceiling."""
    buckets = (2048, 4096, 8192, 12288, 16384, 24576, 32768, 49152, 65536, 98304, 131072, 196608, 262144)
    cur = int((options or {}).get("num_ctx") or 0)
    for b in buckets:
        if b > cur:
            out = dict(options or {})
            out["num_ctx"] = b
            return out
    return None


def normalize_chat_messages(messages: list[dict]) -> list[dict[str, Any]]:
    """Convert stored messages to Ollama's native /api/chat schema."""
    clean_msgs = []
    for m in messages:
        entry: dict[str, Any] = {"role": m["role"], "content": m.get("content") or ""}
        # Ollama requires the complete assistant message, thinking included, on
        # the next request. Dropping it changes the conversation state for
        # thinking models like Qwen.
        if m.get("thinking"):
            entry["thinking"] = m["thinking"]
        if m.get("images"):
            entry["images"] = m["images"]
        if m.get("tool_calls"):
            entry["tool_calls"] = m["tool_calls"]
        if m.get("role") == "tool":
            # The /api/chat schema calls this field ``tool_name``.  ``name`` is
            # an OpenAI-chat convention and is ignored by Ollama, so previous
            # Aether runs returned anonymous tool results to the model.
            tool_name = m.get("tool_name") or m.get("name")
            if tool_name:
                entry["tool_name"] = tool_name
        clean_msgs.append(entry)
    return clean_msgs


async def stream_chat(
    model_id: str,
    messages: list[dict],
    *,
    keep_alive: str = "5m",
    options: dict | None = None,
    tools: list | None = None,
    think: bool | None = None,
) -> AsyncIterator[dict[str, Any]]:
    clean_msgs = normalize_chat_messages(messages)
    payload: dict[str, Any] = {
        "model": model_id,
        "messages": clean_msgs,
        "stream": True,
        "keep_alive": keep_alive,
        "options": options or {},
    }
    if tools:
        payload["tools"] = tools
    if think is not None:
        payload["think"] = bool(think)
    # If the KV window was still sized too small, Ollama truncates the prompt and
    # 500s. Nothing has been yielded at that point, so growing num_ctx and
    # retrying once is safe and turns a dead run into a slower but working one.
    for attempt in range(2):
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", f"{OLLAMA_HOST}/api/chat", json=payload) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", "replace")[:400]
                    grew = _grow_num_ctx(payload.get("options") or {})
                    if attempt == 0 and grew:
                        payload["options"] = grew
                        continue
                    raise RuntimeError(
                        f"Ollama rejected the request ({resp.status_code}). {detail.strip()}"
                    )
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    yield chunk
        return


async def chat_once(
    model_id: str,
    messages: list[dict],
    *,
    options: dict | None = None,
    keep_alive: str = "2m",
    think: bool | None = None,
) -> str:
    out = []
    async for chunk in stream_chat(model_id, messages, options=options, keep_alive=keep_alive, think=think):
        msg = chunk.get("message") or {}
        if msg.get("content"):
            out.append(msg["content"])
        if chunk.get("done"):
            break
    return "".join(out)
