# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Capability probing, fit math and the model catalog."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import config
from config import DATA, OLLAMA_HOST, OLLAMA_MODELS, ROOT

CATALOG_PATH = ROOT / "catalog.json"
PROFILE_CACHE = DATA / "settings" / "models.json"

# Aether runs Ollama with OLLAMA_KV_CACHE_TYPE=q8_0, so one byte per element.
KV_BYTES_PER_ELEMENT = 1
# CUDA context and compute buffers, on top of weights and KV.
RUNTIME_OVERHEAD = 1024**3
# No model_info to read a native window from.
FALLBACK_CONTEXT_MAX = 32768

CTX_BUCKETS = (
    2048, 4096, 8192, 12288, 16384, 24576, 32768,
    49152, 65536, 98304, 131072, 196608, 262144,
)

_SAMPLING_KEYS = {
    "temperature": float,
    "top_p": float,
    "top_k": int,
    "repeat_penalty": float,
}


def parse_parameters(text: str | None) -> dict:
    """Read Ollama's Modelfile PARAMETER blob, which /api/show returns as text."""
    out: dict[str, Any] = {}
    for line in (text or "").splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        key, raw = parts[0].strip(), parts[1].strip().strip('"')
        cast = _SAMPLING_KEYS.get(key)
        if not cast:
            continue
        try:
            out[key] = cast(float(raw))
        except ValueError:
            continue
    return out


def kv_bytes_per_token(model_info: dict) -> int:
    """0 when the GGUF does not carry enough metadata to size the cache."""
    arch = model_info.get("general.architecture") or ""

    def field(suffix: str) -> int:
        try:
            return int(model_info.get(f"{arch}.{suffix}") or 0)
        except (TypeError, ValueError):
            return 0

    blocks = field("block_count")
    kv_heads = field("attention.head_count_kv")
    key_len = field("attention.key_length")
    val_len = field("attention.value_length")
    if not (key_len and val_len):
        # Most GGUFs omit both, and head_dim is embedding_length / head_count.
        heads = field("attention.head_count")
        head_dim = (field("embedding_length") // heads) if heads else 0
        key_len = key_len or head_dim
        val_len = val_len or head_dim
    if not (blocks and kv_heads and key_len and val_len):
        return 0
    return blocks * kv_heads * (key_len + val_len) * KV_BYTES_PER_ELEMENT


def default_label(model_id: str, model_info: dict, details: dict) -> str:
    base = model_info.get("general.basename") or ""
    size = model_info.get("general.size_label") or details.get("parameter_size") or ""
    return f"{base} {size}".strip() if base and size else model_id


def profile_from_show(
    model_id: str,
    show: dict,
    *,
    weights_bytes: int = 0,
    entry: dict | None = None,
) -> dict:
    """Merge a live /api/show response with the catalog entry for that model."""
    entry = entry or {}
    info = show.get("model_info") or {}
    details = show.get("details") or {}
    arch = info.get("general.architecture") or ""
    caps = set(show.get("capabilities") or [])

    kv, kv_source = kv_bytes_per_token(info), "computed"
    # Hybrid-attention models read far too high from the generic formula, so a
    # measured figure in the catalog wins where we have one.
    if entry.get("kv_bytes_per_token"):
        kv, kv_source = int(entry["kv_bytes_per_token"]), "measured"

    sampling: dict[str, Any] = {}
    for key, src in (("temperature", "temp"), ("top_p", "top_p"), ("top_k", "top_k")):
        val = info.get(f"general.sampling.{src}")
        if val is not None:
            sampling[key] = val
    sampling.update(parse_parameters(show.get("parameters")))
    sampling.update(entry.get("tuning") or {})

    profile = {
        "id": model_id,
        "label": entry.get("label") or default_label(model_id, info, details),
        "description": entry.get("description") or "",
        "family": arch or details.get("family") or "",
        "params": int(info.get("general.parameter_count") or 0),
        "size_label": info.get("general.size_label") or details.get("parameter_size") or "",
        "quantization": details.get("quantization_level") or "",
        "capabilities": sorted(caps),
        "tools": "tools" in caps,
        "vision": "vision" in caps,
        "think": "thinking" in caps,
        "context_max": int(info.get(f"{arch}.context_length") or 0) or FALLBACK_CONTEXT_MAX,
        "kv_bytes_per_token": kv,
        "kv_source": kv_source,
        "weights_bytes": int(weights_bytes or 0),
        "modified_at": show.get("modified_at") or "",
    }
    for key, cast in _SAMPLING_KEYS.items():
        if key in sampling:
            try:
                profile[key] = cast(float(sampling[key]))
            except (TypeError, ValueError):
                continue
    return profile


def memory_for(profile: dict, num_ctx: int) -> int:
    kv = int(profile.get("kv_bytes_per_token") or 0) * int(num_ctx)
    return int(profile.get("weights_bytes") or 0) + kv + RUNTIME_OVERHEAD


def fits(profile: dict, num_ctx: int, budget_bytes: int) -> bool:
    return memory_for(profile, num_ctx) <= int(budget_bytes)


def max_context(profile: dict, budget_bytes: int) -> int:
    """Largest bucket that fits the budget. 0 means the weights alone do not."""
    limit = int(profile.get("context_max") or 0)
    best = 0
    for bucket in CTX_BUCKETS:
        if limit and bucket > limit:
            break
        if not fits(profile, bucket, budget_bytes):
            break
        best = bucket
    return best


def load_catalog() -> dict:
    try:
        return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 0, "models": {}, "tiers": [], "utility": []}


def catalog_entry(model_id: str, catalog: dict | None = None) -> dict:
    models = (catalog or load_catalog()).get("models") or {}
    return models.get(model_id) or models.get(normalize_tag(model_id)) or {}


def normalize_tag(name: str) -> str:
    """Ollama library tags are lowercase and default to :latest.

    Hugging Face tags obey neither rule, so they pass through untouched.
    Ollama keeps the repository's own capitalisation in `ollama list`, so
    folding the case here would stop an installed model matching itself, and
    a Hugging Face repository has no `latest` — the tag after the colon is a
    quantization, and inventing one gets "model not found" from the pull.
    """
    value = (name or "").strip()
    if value.lower().startswith(("hf.co/", "huggingface.co/")):
        return value
    value = value.lower()
    for prefix in ("registry.ollama.ai/library/", "library/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    if value and ":" not in value:
        value += ":latest"
    return value


def search_catalog(query: str = "", catalog: dict | None = None) -> list[dict]:
    """Catalog entries matching a query, most recommended first."""
    models = (catalog or load_catalog()).get("models") or {}
    q = (query or "").strip().lower()
    hits = []
    for tag, entry in models.items():
        haystack = " ".join([
            tag,
            entry.get("label") or "",
            entry.get("description") or "",
            " ".join(entry.get("tags") or []),
        ]).lower()
        if q and q not in haystack:
            continue
        hits.append({"tag": tag, **entry})
    hits.sort(key=lambda e: (-int(e.get("rank") or 0), e.get("label") or e["tag"]))
    return hits


def recommend(usable_gb: float, catalog: dict | None = None) -> dict:
    """Tier for a machine with this much usable memory, VRAM before RAM."""
    cat = catalog or load_catalog()
    tiers = cat.get("tiers") or []
    chosen = None
    for tier in tiers:
        low = float(tier.get("min_gb") or 0)
        high = tier.get("max_gb")
        if usable_gb >= low and (high is None or usable_gb < float(high)):
            chosen = tier
            break
    if chosen is None:
        chosen = tiers[-1] if tiers else {"models": []}
    return {**chosen, "utility": cat.get("utility") or []}


async def fetch_show(model_id: str, *, timeout: float = 30.0) -> dict:
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(f"{OLLAMA_HOST}/api/show", json={"model": model_id})
        r.raise_for_status()
        return r.json()


def manifest_size(model_id: str, *, timeout: float = 15.0) -> int:
    """Download size of a model that is not installed yet. 0 when the tag is
    unknown. Uses urllib so the installer can call it before pip has run."""
    import urllib.request

    tag = normalize_tag(model_id)
    if tag.lower().startswith(("hf.co/", "huggingface.co/")):
        # Hugging Face is not an Ollama registry: sizes come per quantization
        # file, and the library module owns which files count as weights.
        import library

        return library.hf_tag_size(tag, timeout=timeout)
    name, _, version = tag.partition(":")
    if "/" not in name:
        name = f"library/{name}"
    url = f"https://registry.ollama.ai/v2/{name}/manifests/{version or 'latest'}"
    req = urllib.request.Request(
        url, headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            layers = json.loads(resp.read()).get("layers") or []
    except Exception:
        return 0
    return sum(
        int(layer.get("size") or 0)
        for layer in layers
        if str(layer.get("mediaType") or "").endswith("image.model")
    )


async def fetch_manifest_size(model_id: str, *, timeout: float = 15.0) -> int:
    return await asyncio.to_thread(manifest_size, model_id, timeout=timeout)


_cache_memo: tuple[tuple, dict] | None = None


def _cache_stamp() -> tuple:
    try:
        st = PROFILE_CACHE.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return ()


def load_cache() -> dict:
    """Memoized on the file's stamp: the agent loop reads this once per step."""
    global _cache_memo
    stamp = _cache_stamp()
    if _cache_memo and _cache_memo[0] == stamp:
        return _cache_memo[1]
    try:
        data = json.loads(PROFILE_CACHE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    _cache_memo = (stamp, data)
    return data


def save_cache(cache: dict) -> None:
    global _cache_memo
    PROFILE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROFILE_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    tmp.replace(PROFILE_CACHE)
    _cache_memo = (_cache_stamp(), cache)


def cache_is_current(cached: dict, modified_at: str) -> bool:
    return bool(cached) and cached.get("modified_at") == modified_at and modified_at != ""


async def probe(model_id: str, *, weights_bytes: int = 0, refresh: bool = False) -> dict:
    """Profile for an installed model, cached until Ollama reports a new build."""
    cache = load_cache()
    profiles = cache.setdefault("profiles", {})
    show = await fetch_show(model_id)
    cached = profiles.get(model_id) or {}
    if not refresh and cache_is_current(cached, show.get("modified_at") or ""):
        return cached
    profile = profile_from_show(
        model_id, show, weights_bytes=weights_bytes, entry=catalog_entry(model_id)
    )
    profiles[model_id] = profile
    save_cache(cache)
    return profile


# ── registry: which installed model serves which role ──────────────────────

def installed(settings: dict | None = None) -> dict[str, dict]:
    """model_id -> profile. Probe cache over the built-in defaults."""
    out = {mid: dict(meta) for mid, meta in config.MODELS.items()}
    for mid, profile in (load_cache().get("profiles") or {}).items():
        merged = dict(out.get(mid) or {})
        merged.update({k: v for k, v in profile.items() if v not in (None, "")})
        merged.setdefault("color", config.DEFAULT_COLOR)
        merged.setdefault("keep_alive", config.DEFAULT_KEEP_ALIVE)
        out[mid] = merged
    return out


def can_serve(profile: dict, role: str) -> bool:
    if role == "agentic":
        return bool(profile.get("tools"))
    if role == "reasoning":
        return bool(profile.get("think"))
    return True


def utility_models(catalog: dict | None = None) -> set[str]:
    """Models that exist to serve Aether itself and never belong in a picker."""
    return {e["tag"] for e in (catalog or load_catalog()).get("utility") or []}


def role_map(settings: dict | None = None, registry: dict | None = None) -> dict[str, list[str]]:
    """model_id -> assigned roles. An unassigned model falls back to capability."""
    reg = registry if registry is not None else installed(settings)
    stored = (settings or {}).get("model_roles") or {}
    utility = utility_models()
    out = {}
    for mid, profile in reg.items():
        roles = stored.get(mid)
        if roles is None:
            roles = [] if mid in utility else ["chat"] + (["agentic"] if can_serve(profile, "agentic") else [])
        out[mid] = [r for r in ("chat", "agentic") if r in roles and can_serve(profile, r)]
    return out


def models_for(role: str, settings: dict | None = None, registry: dict | None = None) -> list[str]:
    reg = registry if registry is not None else installed(settings)
    assigned = role_map(settings, reg)
    if role == "reasoning":
        # Reasoning is the chat role with thinking on, so it is not assignable.
        return [m for m, roles in assigned.items() if "chat" in roles and can_serve(reg[m], "reasoning")]
    return [m for m, roles in assigned.items() if role in roles]


def _preference(role: str) -> tuple[str, ...]:
    if role == "agentic":
        # The MoE coder activates ~3B parameters per token, so it emits tool
        # calls far faster than the dense model that shares its role.
        return (config.DEFAULT_CODER_MODEL, config.DEFAULT_AGENT_MODEL)
    return (config.DEFAULT_CHAT_MODEL,)


def active(role: str, settings: dict | None = None, registry: dict | None = None) -> str | None:
    """The model a role runs on, or None when nothing installed can serve it."""
    reg = registry if registry is not None else installed(settings)
    options = models_for(role, settings, reg)
    if not options:
        return None
    chosen = ((settings or {}).get("model_active") or {}).get(role)
    if chosen in options:
        return chosen
    for preferred in _preference(role):
        if preferred in options:
            return preferred
    return options[0]


def vision_model(settings: dict | None = None, registry: dict | None = None) -> str | None:
    """Computer use needs eyes, so it routes here rather than to the active agent."""
    reg = registry if registry is not None else installed(settings)
    options = [m for m in models_for("agentic", settings, reg) if reg[m].get("vision")]
    if not options:
        return None
    preferred = active("agentic", settings, reg)
    return preferred if preferred in options else options[0]


def context_limit(model_id: str, role: str, settings: dict | None = None, profile: dict | None = None) -> int:
    settings = settings or {}
    profile = profile if profile is not None else installed(settings).get(model_id) or {}
    want = (settings.get("model_context") or {}).get(model_id)
    if not want:
        want = (settings.get("role_context") or {}).get(role)
    want = int(want or config.ROLE_CONTEXT.get(role) or FALLBACK_CONTEXT_MAX)
    ceiling = int(profile.get("context_max") or want)
    role_cap = config.ROLE_CONTEXT_MAX.get(role)
    if role_cap:
        ceiling = min(ceiling, int(role_cap))
    return min(want, ceiling)


def resolve(model_id: str, role: str, settings: dict | None = None, registry: dict | None = None) -> dict:
    """The meta dict the run needs: model profile plus whatever the role overrides."""
    reg = registry if registry is not None else installed(settings)
    meta = dict(reg.get(model_id) or {"id": model_id, "label": model_id})
    meta.setdefault("id", model_id)
    meta.setdefault("label", model_id)
    meta.setdefault("color", config.DEFAULT_COLOR)
    meta.setdefault("keep_alive", config.DEFAULT_KEEP_ALIVE)
    meta.update(config.ROLE_TUNING.get(role) or {})
    suffix = config.ROLE_LABEL_SUFFIX.get(role)
    if suffix and not meta["label"].endswith(suffix):
        meta["label"] += suffix
    if config.ROLE_COLOR.get(role):
        meta["color"] = config.ROLE_COLOR[role]
    meta["context_max"] = min(
        int(meta.get("context_max") or FALLBACK_CONTEXT_MAX),
        int(config.ROLE_CONTEXT_MAX.get(role) or FALLBACK_CONTEXT_MAX * 8),
    )
    meta["role"] = role
    meta["effort"] = config.effort_for(role)
    meta["context"] = context_limit(model_id, role, settings, meta)
    return meta


def system_prompt(role: str, profile: dict) -> str:
    prompt = config.SYSTEM_PROMPTS[role]
    label = (profile or {}).get("label")
    if label:
        prompt += config.MODEL_IDENTITY.format(label=label)
    if role == "agentic" and profile.get("vision"):
        prompt += config.VISION_ADDON
    return prompt


def title_model(settings: dict | None = None, registry: dict | None = None) -> str | None:
    """Smallest installed model, so naming a chat never displaces the GPU model."""
    reg = registry if registry is not None else installed(settings)
    if config.TITLE_MODEL_ID in reg:
        return config.TITLE_MODEL_ID
    sized = [(p.get("params") or p.get("weights_bytes") or 0, m) for m, p in reg.items()]
    sized = [(n, m) for n, m in sized if n]
    return min(sized)[1] if sized else (next(iter(reg), None))


async def refresh_registry() -> dict[str, dict]:
    """Profile every installed model. Cheap after the first run: the probe cache
    is keyed on Ollama's build stamp, so only a rebuilt or new tag is re-read."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(f"{OLLAMA_HOST}/api/tags")
            r.raise_for_status()
            tags = r.json().get("models") or []
    except Exception:
        return installed()
    cache = load_cache()
    profiles = cache.setdefault("profiles", {})
    catalog = load_catalog()
    changed = False
    for tag in tags:
        model_id = tag.get("name") or tag.get("model")
        if not model_id:
            continue
        try:
            show = await fetch_show(model_id)
        except Exception:
            continue
        if cache_is_current(profiles.get(model_id) or {}, show.get("modified_at") or ""):
            continue
        profiles[model_id] = profile_from_show(
            model_id, show,
            weights_bytes=int(tag.get("size") or 0),
            entry=catalog_entry(model_id, catalog),
        )
        changed = True
    # A model the user removed in Ollama must stop appearing in the pickers.
    live = {t.get("name") or t.get("model") for t in tags}
    for stale in [m for m in profiles if m not in live]:
        profiles.pop(stale)
        changed = True
    if changed:
        save_cache(cache)
    return installed()


def effort_levels(profile: dict | None = None) -> list[dict]:
    """Effort tiers as this model actually runs them, low to high.

    A tier's `think` flag is a ceiling, not a promise: a model with no thinking
    mode runs every tier direct. The hint says which, because the difference is
    invisible otherwise and it is the first thing users get wrong.
    """
    supports = bool((profile or {}).get("think"))
    out = []
    for key in config.EFFORT_ORDER:
        tier = config.EFFORT.get(key)
        if not tier:
            continue
        thinking = bool(tier.get("think")) and supports
        if not supports:
            note = config.THINKING_UNSUPPORTED
        else:
            note = config.THINKING_ON if thinking else config.THINKING_OFF
        out.append({**tier, "key": key, "think": thinking,
                    "hint": (tier.get("hint") or "") + note})
    return out


def thinking_summary(profile: dict | None = None) -> dict:
    """What to tell the user about thinking on this model, in one line."""
    supports = bool((profile or {}).get("think"))
    tiers = [k for k in config.EFFORT_ORDER if (config.EFFORT.get(k) or {}).get("think")]
    if not supports:
        return {"supported": False, "from": None,
                "summary": "This model has no thinking mode. Every effort level answers directly."}
    first = tiers[0] if tiers else None
    label = (config.EFFORT.get(first) or {}).get("label", first) if first else None
    return {
        "supported": True,
        "from": first,
        "summary": f"{label} and above think before answering; lower levels answer directly."
                   if first else "This model answers directly at every level.",
    }


# ── hardware ───────────────────────────────────────────────────────────────

def _nvidia_vram_bytes() -> tuple[int, str]:
    """Largest single GPU, since Ollama does not split one model across cards."""
    exe = shutil.which("nvidia-smi")
    if not exe and sys.platform == "win32":
        candidate = Path(os.environ.get("ProgramFiles", "")) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"
        exe = str(candidate) if candidate.exists() else None
    if not exe:
        return 0, ""
    try:
        out = subprocess.check_output(
            [exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            text=True, timeout=10, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return 0, ""
    best, name = 0, ""
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            mib = int(float(parts[-1]))
        except ValueError:
            continue
        if mib > best:
            best, name = mib, ", ".join(parts[:-1])
    return best * 1024 * 1024, name


def _total_ram_bytes() -> int:
    if sys.platform == "win32":
        try:
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = Status()
            status.dwLength = ctypes.sizeof(Status)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return int(status.ullTotalPhys)
        except Exception:
            return 0
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 0


def model_store() -> Path:
    """Where Ollama keeps weights, which is the disk a pull actually fills."""
    if OLLAMA_MODELS:
        return Path(OLLAMA_MODELS)
    if sys.platform == "win32":
        return Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".ollama" / "models"
    for candidate in (Path("/usr/share/ollama/.ollama/models"), Path.home() / ".ollama" / "models"):
        if candidate.exists():
            return candidate
    return Path.home() / ".ollama" / "models"


def _free_disk_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return 0


# Without a GPU, size is not the limit, speed is: a 27B on CPU answers at
# roughly a word a second and a 14B is not much better. RAM says such a model
# would load, so recommendations are capped into the 8B tier, which is the
# largest that stays usable on a CPU.
CPU_RECOMMEND_CAP = 8 * 1024**3


def hardware() -> dict:
    """What this machine can actually run. VRAM decides when a GPU is present:
    a model that spills to system RAM works but crawls."""
    vram, gpu = _nvidia_vram_bytes()
    ram = _total_ram_bytes()
    store = model_store()
    usable = vram or ram
    return {
        "gpu": gpu or "none detected",
        "vram_bytes": vram,
        "ram_bytes": ram,
        "free_disk_bytes": _free_disk_bytes(store),
        "model_store": str(store),
        "cpu_only": not vram,
        # Ollama will page into RAM, but the usable figure is what runs at speed.
        "usable_bytes": usable,
        # What to recommend against, which is not the same as what fits.
        "recommend_bytes": usable if vram else min(ram, CPU_RECOMMEND_CAP),
    }


def fit_report(profile: dict, budget_bytes: int) -> dict:
    """Whether this model runs here, and how much window it gets if so."""
    window = max_context(profile, budget_bytes)
    return {
        "fits": window > 0,
        "max_context": window,
        "weights_bytes": int(profile.get("weights_bytes") or 0),
        "kv_bytes_per_token": int(profile.get("kv_bytes_per_token") or 0),
        "kv_source": profile.get("kv_source") or "computed",
        "required_bytes": memory_for(profile, window or CTX_BUCKETS[0]),
    }


class PullProgress:
    """Aggregate an Ollama pull into a single percentage.

    Ollama reports bytes per layer, and a layer announces its total before any
    of it has arrived, so the newest layer's own percentage sends the bar back
    to zero several times over. Summing every layer seen so far fixes that.
    Bytes only ever grow; the percentage can still step back slightly when a
    new layer's total first appears, which for a real model is negligible
    beside the weights layer.
    """

    def __init__(self) -> None:
        self.layers: dict[str, tuple[int, int]] = {}

    def update(self, chunk: dict) -> dict:
        status = str(chunk.get("status") or "")
        digest = chunk.get("digest")
        if digest:
            total = int(chunk.get("total") or 0)
            done = int(chunk.get("completed") or 0)
            known_total, known_done = self.layers.get(digest, (0, 0))
            self.layers[digest] = (max(total, known_total), max(done, known_done))
        total = sum(t for t, _ in self.layers.values())
        done = sum(d for _, d in self.layers.values())
        return {
            "type": "progress",
            "status": status,
            "total": total,
            "completed": done,
            "pct": round(100 * done / total, 1) if total else None,
        }


def install_plan(installed: set[str] | None = None) -> dict:
    """Hardware, and what each wizard choice would install. Shared by both
    installers so the Windows one does not carry a second copy of the catalog."""
    installed = {normalize_tag(t) for t in (installed or set())}
    catalog = load_catalog()
    hw = hardware()
    utility = list(catalog.get("utility") or [])

    def tags_for(tier: dict) -> list[str]:
        seen, out = set(), []
        for entry in utility + list(tier.get("models") or []):
            tag = entry.get("tag")
            if tag and tag not in seen:
                seen.add(tag)
                out.append(tag)
        return out

    tiers = catalog.get("tiers") or []
    default_tier = next((t for t in tiers if t.get("id") == "default"), {})
    budget_gb = (hw.get("recommend_bytes") or 0) / 1024**3
    recommended_tier = recommend(budget_gb, catalog)

    def describe(tier: dict) -> dict:
        tags = tags_for(tier)
        missing = [t for t in tags if normalize_tag(t) not in installed]
        return {
            "id": tier.get("id"),
            "label": tier.get("label"),
            "note": tier.get("note"),
            "tags": tags,
            "missing": missing,
            "download_bytes": sum(manifest_size(t) for t in missing),
        }

    default = describe(default_tier)
    recommended = describe(recommended_tier)
    return {
        "hardware": hw,
        "default": default,
        "recommended": recommended,
        "same": sorted(default["tags"]) == sorted(recommended["tags"]),
        "library_url": catalog.get("library_url"),
    }


if __name__ == "__main__":
    if "--plan" in sys.argv:
        print(json.dumps(install_plan(), indent=2))
