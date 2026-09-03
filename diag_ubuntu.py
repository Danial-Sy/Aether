#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Full Ubuntu diagnostic. Runs against a live server on :7878."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:7878"
OLLAMA = "http://127.0.0.1:11434"
TIMEOUT = 600

results: list[tuple[str, str, str]] = []  # section, status, detail


def log(section: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    results.append((section, status, detail[:500]))
    mark = "✓" if ok else "✗"
    print(f"{mark} [{section}] {detail[:200]}", flush=True)


def api(method: str, path: str, body: dict | None = None, timeout: float = 30) -> tuple[int, dict | str]:
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def ollama_ps() -> list[dict]:
    with urllib.request.urlopen(f"{OLLAMA}/api/ps", timeout=5) as r:
        return json.loads(r.read()).get("models") or []


def gpu_mib() -> tuple[int, int]:
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
        text=True,
    ).strip()
    used, total = [int(x.strip()) for x in out.split(",")]
    return used, total


def mem_avail_gib() -> float:
    with open("/proc/meminfo") as f:
        info = {}
        for line in f:
            k, v = line.split(":", 1)
            info[k.strip()] = int(v.split()[0])
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    return avail / (1024**2)


def stream_send(cid: str, content: str, **kwargs) -> dict:
    """POST /send and collect SSE until done."""
    payload = {"content": content, **kwargs}
    url = f"{BASE}/api/chats/{cid}/send"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    out = {
        "events": [],
        "content": "",
        "thinking": "",
        "tool_calls": [],
        "tool_results": [],
        "memory_events": [],
        "errors": [],
        "done": False,
        "elapsed": 0.0,
        "tokens_approx": 0,
    }
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        buf = ""
        for chunk in iter(lambda: r.read(4096), b""):
            buf += chunk.decode(errors="replace")
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                for line in block.splitlines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    out["events"].append(ev)
                    t = ev.get("type")
                    if t == "delta":
                        out["content"] = ev.get("content") or out["content"]
                        out["thinking"] = ev.get("thinking") or out["thinking"]
                    elif t == "message":
                        m = ev.get("message") or {}
                        out["content"] = m.get("content") or out["content"]
                        out["thinking"] = m.get("thinking") or out["thinking"]
                    elif t == "tool_call":
                        out["tool_calls"].append(ev)
                    elif t == "tool_result":
                        out["tool_results"].append(ev)
                    elif t in ("memory_saved", "memory_recall"):
                        out["memory_events"].append(ev)
                    elif t == "error":
                        out["errors"].append(ev.get("message"))
                    elif t == "done":
                        out["done"] = True
                        ctx = ev.get("context") or {}
                        out["tokens_approx"] = ctx.get("used", 0)
    out["elapsed"] = time.time() - t0
    return out


def stream_compact(cid: str) -> dict:
    url = f"{BASE}/api/chats/{cid}/compact"
    req = urllib.request.Request(url, method="POST")
    out = {"summary": "", "messages_after": 0, "compacted": False}
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        buf = ""
        for chunk in iter(lambda: r.read(4096), b""):
            buf += chunk.decode(errors="replace")
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                for line in block.splitlines():
                    if not line.startswith("data:"):
                        continue
                    ev = json.loads(line[5:].strip())
                    if ev.get("type") == "compacted":
                        out["summary"] = ev.get("summary") or ""
                        out["messages_after"] = len(ev.get("messages") or [])
                        out["compacted"] = True
    return out


def wait_server(deadline: float = 120) -> bool:
    t0 = time.time()
    while time.time() - t0 < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/api/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def wait_warm(deadline: float = 900) -> dict:
    t0 = time.time()
    while time.time() - t0 < deadline:
        try:
            with urllib.request.urlopen(f"{BASE}/api/warmup", timeout=30) as r:
                data = json.loads(r.read())
                if data.get("ok") or data.get("status") == "ready":
                    return data
        except Exception:
            pass
        time.sleep(2)
    return {"ok": False, "status": "timeout"}


def main() -> int:
    print("=" * 60)
    print("AETHER UBUNTU FULL DIAGNOSTIC")
    print("=" * 60)

    # --- 0. Prerequisites ---
    try:
        subprocess.check_output(["nvidia-smi"], stderr=subprocess.STDOUT)
        log("prereq.gpu", True, "NVIDIA GPU visible")
    except Exception as e:
        log("prereq.gpu", False, str(e))

    spill = subprocess.run(
        ["systemctl", "show", "ollama", "-p", "Environment", "--no-pager"],
        capture_output=True,
        text=True,
    ).stdout
    log(
        "prereq.spillover",
        "GGML_CUDA_ENABLE_UNIFIED_MEMORY=1" in spill,
        "systemd unified memory" if "GGML_CUDA_ENABLE_UNIFIED_MEMORY=1" in spill else spill[:120],
    )

    if not wait_server(5):
        log("server", False, "Aether not on :7878. Start ./aether.sh first")
        return 1
    log("server", True, "API reachable")

    warm = wait_warm(900)
    log("warmup", warm.get("ok") or warm.get("status") == "ready", json.dumps(warm)[:180])
    ps = ollama_ps()
    log("warmup.model_loaded", bool(ps), f"resident: {[m.get('name') for m in ps]}")

    # --- 1. Memory router (unit) ---
    sys.path.insert(0, str(ROOT))
    import memory_router as mr
    import search_router as sr

    i = mr.classify("remember this: my Ubuntu diag color is teal")
    log("memory.router.store", i.store_memory and not i.recall_memory, str(i))
    i2 = mr.classify("do you remember my Ubuntu diag color?")
    log("memory.router.recall", i2.recall_memory and not i2.store_memory, str(i2))
    i3 = mr.classify("what did we discuss in our other chat about spillover?")
    log("memory.router.cross", i3.cross_chat, str(i3))

    d = sr.route_search("what's the weather in Seattle")
    log("search.router", d.search and "weather" in (d.query or "").lower(), f"{d.reason} → {d.query}")

    # --- 2. Memory store + recall via API ---
    _, chat_a = api("POST", "/api/chats", {"mode": "chat"})
    cid_a = chat_a["id"]
    r = stream_send(cid_a, "Please remember this: my diagnostic passphrase is NEBULA-42.")
    saved = any(e.get("type") == "memory_saved" for e in r["memory_events"])
    log("memory.store.api", saved, f"events={r['memory_events']}")

    _, mem = api("GET", "/api/memory/global")
    notes = [n.get("text", "") for n in (mem.get("notes") or [])]
    log("memory.persist", any("NEBULA-42" in n for n in notes), f"{len(notes)} global notes")

    r2 = stream_send(cid_a, "Do you remember my diagnostic passphrase?")
    recall_ev = any(e.get("type") == "memory_recall" for e in r2["memory_events"])
    mentions = "NEBULA" in (r2["content"] or "").upper()
    log("memory.recall.api", recall_ev and mentions, f"recall_ev={recall_ev} answer_has_pass={mentions}")

    # --- 3. Cross-chat reference ---
    _, chat_b = api("POST", "/api/chats", {"mode": "chat", "title": "Spillover notes chat"})
    cid_b = chat_b["id"]
    stream_send(cid_b, "In this chat we decided the spillover keyword is OVERFLOW-RAM for testing.")
    time.sleep(1)
    _, chat_c = api("POST", "/api/chats", {"mode": "chat"})
    cid_c = chat_c["id"]
    r3 = stream_send(cid_c, "What did we discuss in our other chat about spillover?")
    cross = "OVERFLOW-RAM" in (r3["content"] or "") or "spillover" in (r3["content"] or "").lower()
    log("cross_chat", cross, (r3["content"] or "")[:160])

    # --- 4. Compaction ---
    _, chat_d = api("POST", "/api/chats", {"mode": "chat"})
    cid_d = chat_d["id"]
    for i in range(8):
        stream_send(cid_d, f"Turn {i+1}: list three prime numbers and one fact about Ubuntu {i+1}.")
    _, before = api("GET", f"/api/chats/{cid_d}")
    n_before = len(before.get("messages") or [])
    c = stream_compact(cid_d)
    _, after = api("GET", f"/api/chats/{cid_d}")
    n_after = len(after.get("messages") or [])
    has_summary = bool((after.get("summary") or "").strip())
    log(
        "compact.manual",
        c["compacted"] and has_summary and n_after < n_before,
        f"msgs {n_before}→{n_after} summary_len={len(after.get('summary') or '')}",
    )

    # --- 5. Think ON vs OFF (general / qwen3.8) ---
    _, chat_think = api("POST", "/api/chats", {"mode": "chat"})
    cid_th = chat_think["id"]
    r_on = stream_send(
        cid_th,
        "What is 17 * 23? Reply with just the number.",
        model_key="general",
        think=True,
    )
    r_off = stream_send(
        cid_th,
        "What is 19 * 21? Reply with just the number.",
        model_key="general",
        think=False,
    )
    think_on_len = len(r_on.get("thinking") or "")
    think_off_len = len(r_off.get("thinking") or "")
    ans_on = "391" in (r_on["content"] or "")
    ans_off = "399" in (r_off["content"] or "")
    log(
        "think.on",
        ans_on and (think_on_len > 20 or think_on_len >= think_off_len),
        f"thinking_chars={think_on_len} ans_ok={ans_on} elapsed={r_on['elapsed']:.1f}s",
    )
    log(
        "think.off",
        ans_off,
        f"thinking_chars={think_off_len} ans_ok={ans_off} elapsed={r_off['elapsed']:.1f}s",
    )
    log(
        "think.delta",
        think_on_len > think_off_len + 10 or (think_on_len > 0 and think_off_len == 0),
        f"on={think_on_len} off={think_off_len}",
    )

    # --- 6. Reasoning model (qwen3.8) ---
    _, chat_r = api("POST", "/api/chats", {"mode": "chat"})
    cid_r = chat_r["id"]
    t0 = time.time()
    r_r = stream_send(
        cid_r,
        "Is 997 a prime number? Answer yes or no only.",
        model_key="reasoning",
    )
    r_ok = re.search(r"\byes\b", (r_r["content"] or ""), re.I) is not None
    log(
        "model.reasoning",
        r_ok and r_r["done"],
        f"elapsed={r_r['elapsed']:.1f}s thinking={len(r_r.get('thinking') or '')} chars",
    )

    # --- 7. VRAM spillover stress ---
    gpu_before = gpu_mib()[0]
    ram_before = mem_avail_gib()
    print(f"  [spillover] GPU before={gpu_before} MiB RAM avail={ram_before:.1f} GiB", flush=True)
    try:
        payload = {
            "model": "qwen3.8:27b",
            "prompt": "Say hello in one word.",
            "stream": False,
            "keep_alive": "2m",
            "options": {"num_ctx": 98304, "num_predict": 8, "temperature": 0},
        }
        t0 = time.time()
        req = urllib.request.Request(
            f"{OLLAMA}/api/generate",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            gen = json.loads(resp.read())
        elapsed = time.time() - t0
        gpu_after = gpu_mib()[0]
        ram_after = mem_avail_gib()
        spill_ok = gen.get("done") and not gen.get("error")
        heavy = gpu_after > 18000 or ram_after < ram_before - 2
        log(
            "vram.spillover",
            spill_ok,
            f"num_ctx=98304 gpu {gpu_before}→{gpu_after} MiB ram_avail {ram_before:.1f}→{ram_after:.1f} GiB elapsed={elapsed:.1f}s",
        )
        log("vram.spillover.pressure", heavy or gpu_after > 12000, f"gpu_used={gpu_after}")
        # unload
        urllib.request.urlopen(
            urllib.request.Request(
                f"{OLLAMA}/api/generate",
                data=json.dumps({"model": "qwen3.8:27b", "keep_alive": 0, "prompt": ""}).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            ),
            timeout=60,
        )
    except Exception as e:
        log("vram.spillover", False, str(e))

    # --- 8. Agentic coder ---
    diag_dir = ROOT / "data" / "diag_agent"
    diag_dir.mkdir(parents=True, exist_ok=True)
    test_file = diag_dir / "hello_diag.py"
    if test_file.exists():
        test_file.unlink()
    _, proj = api("POST", "/api/projects", {
        "name": "Diag Agent",
        "root": str(diag_dir),
        "description": "Ubuntu diagnostic sandbox",
    })
    pid = proj["id"]
    _, chat_agent = api("POST", "/api/chats", {"mode": "agentic", "project_id": pid})
    cid_agent = chat_agent["id"]
    r_agent = stream_send(
        cid_agent,
        f"Create a file hello_diag.py in the project root that prints exactly: AETHER_DIAG_OK\nThen run it with python3 and confirm the output.",
        model_key="coder",
    )
    wrote = test_file.exists() and "AETHER_DIAG_OK" in test_file.read_text()
    tool_used = len(r_agent["tool_calls"]) >= 1 or len(r_agent["tool_results"]) >= 1
    run_ok = "AETHER_DIAG_OK" in json.dumps(r_agent["tool_results"]) or "AETHER_DIAG_OK" in (r_agent["content"] or "")
    log("agentic.tools", tool_used, f"tools={len(r_agent['tool_calls'])} results={len(r_agent['tool_results'])}")
    log("agentic.write_run", wrote and (run_ok or wrote), f"file_exists={wrote} elapsed={r_agent['elapsed']:.1f}s")

    # --- 9. Computer overlay + screenshot + VL model ---
    _, ov_on = api("POST", "/api/computer-overlay", {"enabled": True})
    time.sleep(0.8)
    _, ov_st = api("GET", "/api/computer-overlay")
    overlay_ok = ov_on.get("enabled") and ov_st.get("enabled")
    log("computer.overlay", overlay_ok, str(ov_st))

    shot_ok = False
    shot_b64 = None
    try:
        with urllib.request.urlopen(f"{BASE}/api/screenshot", timeout=30, method="POST", data=b"") as r:
            shot = json.loads(r.read())
        shot_ok = bool(shot.get("image_b64")) and shot.get("size", 0) > 1000
        shot_b64 = shot.get("image_b64")
        log("computer.screenshot", shot_ok, f"size={shot.get('size')} path={shot.get('path')}")
    except Exception as e:
        log("computer.screenshot", False, str(e))

    api("POST", "/api/computer-overlay", {"enabled": False})

    if shot_b64:
        _, chat_cu = api("POST", "/api/chats", {"mode": "agentic"})
        cid_cu = chat_cu["id"]
        # send with screenshot as attachment via ingest path
        shot_path = ROOT / "data" / "uploads"
        shots = sorted(shot_path.glob("shot_*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        attach = []
        if shots:
            attach = [{"name": shots[0].name, "kind": "screenshot", "path": str(shots[0])}]
        r_cu = stream_send(
            cid_cu,
            "This is a screenshot of my desktop. In one short sentence, describe whether you see a taskbar or panel and what color theme the desktop appears to have.",
            model_key="computer",
            computer_use=True,
            attachments=attach,
        )
        api("POST", "/api/computer-overlay", {"enabled": False})
        vl_ok = r_cu["done"] and len(r_cu["content"] or "") > 20
        log("computer.vl_model", vl_ok, (r_cu["content"] or "")[:180])
    else:
        log("computer.vl_model", False, "skipped, no screenshot")

    # --- 10. Shutdown unload ---
    gpu_pre = gpu_mib()[0]
    api("POST", "/api/shutdown")
    time.sleep(2)
    ps_end = ollama_ps()
    gpu_post = gpu_mib()[0]
    log(
        "shutdown.unload",
        len(ps_end) == 0 or gpu_post < max(800, gpu_pre - 4000),
        f"ps={ps_end} gpu {gpu_pre}→{gpu_post} MiB",
    )

    # --- Summary ---
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    passed = sum(1 for _, s, _ in results if s == "PASS")
    failed = sum(1 for _, s, _ in results if s == "FAIL")
    for section, status, detail in results:
        print(f"  {status:4}  {section:28}  {detail[:70]}")
    print(f"\nTotal: {passed} passed, {failed} failed / {len(results)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
