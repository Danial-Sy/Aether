# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""The two remote libraries Aether installs from: Ollama's and Hugging Face's.

Both are browse-only. Nothing here runs a model or decides what one can do.
Installing goes through the same `ollama pull` either way, because Ollama
imports GGUF straight from Hugging Face when the tag starts with `hf.co/`, and
the real capability answer still comes from `/api/show` after the download.

So this module has one job: turn two very different catalogues into one row
shape the models screen can render, and say honestly what we know about a
model before a byte of it has arrived. Hugging Face is a hundred thousand
repositories with no curation, so "what is wrong with this one" matters more
than "what is right with it": every row carries its own warnings.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from config import DATA
from models import RUNTIME_OVERHEAD

OLLAMA_LIBRARY_URL = "https://ollama.com/library"
HF_API = "https://huggingface.co/api/models"
HF_PREFIX = "hf.co/"

CACHE_PATH = DATA / "settings" / "library-ollama.json"
# Ollama ships a handful of models a month. A day-old list is never wrong in a
# way the user would notice, and it makes search feel local, because it is.
CACHE_TTL = 24 * 3600

PAGE = 24
SOURCES = ("ollama", "huggingface")
SORTS = ("popular", "trending", "newest", "updated")

_HF_SORT = {
    "popular": "downloads",
    "trending": "trendingScore",
    "newest": "createdAt",
    "updated": "lastModified",
}

# Parameter bands, so "will this fit" is askable before any size is known.
PARAM_BANDS = {
    "tiny": (0, 4e9),
    "small": (4e9, 15e9),
    "medium": (15e9, 35e9),
    "large": (35e9, None),
}

# Neither library exposes an embedding flag we can trust, so we match the
# words both use. An embedding model cannot hold a conversation, and Aether
# has no role for one, so it is noise in a picker rather than a choice.
_EMBEDDING_PIPELINES = {"feature-extraction", "sentence-similarity"}

_SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}


def is_hf_tag(name: str) -> bool:
    return (name or "").strip().lower().startswith(("hf.co/", "huggingface.co/"))


def hf_repo(tag: str) -> str:
    """The `owner/name` inside an hf.co tag, without the quantization."""
    body = re.sub(r"^(hf\.co|huggingface\.co)/", "", (tag or "").strip(), flags=re.I)
    return body.rsplit(":", 1)[0] if ":" in body.split("/")[-1] else body


def parse_count(text: str | None) -> int:
    """"119.2M" -> 119200000. Ollama renders pull counts, never sends them."""
    raw = (text or "").strip().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kKmMbBtT]?)", raw)
    if not match:
        return 0
    value = float(match.group(1))
    return int(value * _SUFFIX.get(match.group(2).lower(), 1))


def parse_params(text: str | None) -> int:
    """"8b" or "27.3B" -> a parameter count. 0 when it is not a size at all."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kKmMbBtT])", (text or "").strip())
    return int(float(match.group(1)) * _SUFFIX[match.group(2).lower()]) if match else 0


def size_label(params: int) -> str:
    if not params:
        return ""
    if params >= 1e9:
        billions = params / 1e9
        return f"{billions:.0f}B" if billions >= 10 else f"{billions:.1f}B"
    return f"{round(params / 1e6)}M"


# ── Ollama's library ───────────────────────────────────────────────────────
#
# Ollama publishes no browse API, so the library page is parsed. That is a
# scrape and scrapes rot, which is why every failure here degrades to the
# curated catalog.json rather than to an error: a stale list of good models
# beats a broken screen.

_LI = re.compile(r'<li\s[^>]*class="flex items-baseline.*?</li>', re.S)
_NAME = re.compile(r'href="/library/([^"?#/]+)"')
_DESC = re.compile(r'<p class="max-w-lg[^"]*">(.*?)</p>', re.S)
_BADGE = re.compile(r'<span\s+class="inline-flex[^"]*">\s*(.*?)\s*</span>', re.S)
_STAT = re.compile(r">\s*([\d.,]+\s*[KMBT]?)\s*</span>\s*<span class=\"hidden sm:flex\">&nbsp;(\w+)")
_UPDATED = re.compile(r'title="([^"]*(?:UTC|GMT)[^"]*)"')


def _text(html: str) -> str:
    """Tag soup to a plain string. The library page has no nested markup in
    the fields we read, so unescaping the five XML entities is enough."""
    out = re.sub(r"<[^>]+>", "", html or "")
    for entity, char in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")):
        out = out.replace(entity, char)
    return " ".join(out.split())


def parse_library(html: str) -> list[dict]:
    """Every model on ollama.com/library, as normalized rows."""
    rows = []
    for block in _LI.findall(html or ""):
        name = _NAME.search(block)
        if not name:
            continue
        badges = [_text(b) for b in _BADGE.findall(block)]
        variants = [b for b in badges if parse_params(b)]
        capabilities = [b.lower() for b in badges if not parse_params(b)]
        stats = {kind.lower(): parse_count(value) for value, kind in _STAT.findall(block)}
        updated = _UPDATED.search(block)
        desc = _DESC.search(block)
        rows.append({
            "name": name.group(1),
            "description": _text(desc.group(1)) if desc else "",
            "variants": variants,
            "params": max((parse_params(v) for v in variants), default=0),
            "min_params": min((parse_params(v) for v in variants), default=0),
            "capabilities": capabilities,
            "downloads": stats.get("pulls", 0),
            "tag_count": stats.get("tags", 0),
            "updated": updated.group(1) if updated else "",
        })
    return rows


def _cache_read() -> dict:
    try:
        cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return cached if isinstance(cached.get("models"), list) else {}


def _cache_write(models: list[dict]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fetched_at": time.time(), "models": models}), encoding="utf-8")
    tmp.replace(CACHE_PATH)


async def ollama_library(*, refresh: bool = False, timeout: float = 20.0) -> list[dict]:
    """The whole library, cached for a day. Falls back to the last good copy,
    and then to nothing, so being offline costs the browse list and not more."""
    import httpx

    cached = _cache_read()
    fresh = cached and (time.time() - float(cached.get("fetched_at") or 0)) < CACHE_TTL
    if cached and fresh and not refresh:
        return cached["models"]
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(OLLAMA_LIBRARY_URL)
            resp.raise_for_status()
        models = parse_library(resp.text)
    except Exception:
        return cached.get("models") or []
    if not models:
        # The page loaded but parsed to nothing, which means the markup moved.
        # Keep whatever we had rather than caching the emptiness.
        return cached.get("models") or []
    _cache_write(models)
    return models


async def ollama_variants(name: str, *, timeout: float = 20.0) -> list[dict]:
    """Every tag of one model, with the size and window Ollama advertises."""
    import httpx

    url = f"{OLLAMA_LIBRARY_URL}/{name}/tags"
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        html = resp.text
    except Exception:
        return []
    seen, out = set(), []
    for tag in re.findall(rf'href="/library/({re.escape(name)}:[^"?#]+)"', html):
        if tag in seen:
            continue
        seen.add(tag)
        # The row that follows the link carries "digest • 5.2GB • 40K context".
        after = html.split(f'href="/library/{tag}"', 1)[-1][:1200]
        detail = _text(after)
        size = re.search(r"([\d.]+)\s*(GB|MB)", detail, re.I)
        window = re.search(r"([\d.]+)\s*([KM])\s*context", detail, re.I)
        scale = 1024**3 if size and size.group(2).upper() == "GB" else 1024**2
        out.append({
            "tag": tag,
            "label": tag.split(":", 1)[1],
            "weights_bytes": int(float(size.group(1)) * scale) if size else 0,
            "context_length": int(float(window.group(1)) * (1000 if window.group(2).upper() == "K" else 1e6))
            if window else 0,
        })
    return out


# ── Hugging Face ───────────────────────────────────────────────────────────
#
# Queried live rather than cached: the GGUF corner alone is tens of thousands
# of repositories and grows hourly, so there is nothing here worth keeping a
# local copy of. `expand[]` returns the GGUF header inline, which is what lets
# a row say how big the model is and whether its template can call tools
# without downloading anything.

_HF_EXPAND = ("gguf", "downloads", "likes", "lastModified", "createdAt",
              "gated", "pipeline_tag", "tags", "author", "cardData")

# Not models: a projector is the eyes of a vision model and useless alone, an
# imatrix is calibration data, and MTP is a speculative-decoding draft head.
_NOT_A_MODEL = re.compile(r"(?:^|/)(?:mmproj|imatrix|mtp)[-_.]?", re.I)
_SPLIT = re.compile(r"^(.*?)-(\d{5})-of-(\d{5})\.gguf$", re.I)
_QUANT = re.compile(
    r"(?:^|[-_.])((?:IQ|Q)\d+(?:_[A-Za-z0-9]+)*|BF16|FP16|F16|F32)$", re.I
)


def _quant_label(rfilename: str) -> str:
    """The name Ollama wants after the colon. Quantization lives in the file
    name for most publishers and in a directory name for the rest."""
    directory, _, base = rfilename.rpartition("/")
    stem = _SPLIT.sub(r"\1", base)
    stem = stem[:-5] if stem.lower().endswith(".gguf") else stem
    match = _QUANT.search(stem)
    if match:
        return match.group(1)
    if directory and _QUANT.search(f"-{directory.split('/')[-1]}"):
        return directory.split("/")[-1]
    return ""


def parse_quants(siblings: list[dict]) -> list[dict]:
    """One row per installable quantization, with split files added together."""
    groups: dict[str, dict] = {}
    for entry in siblings or []:
        name = entry.get("rfilename") or ""
        if not name.lower().endswith(".gguf") or _NOT_A_MODEL.search(name):
            continue
        label = _quant_label(name)
        if not label:
            continue
        row = groups.setdefault(label, {"label": label, "weights_bytes": 0, "parts": 0})
        row["weights_bytes"] += int(entry.get("size") or 0)
        row["parts"] += 1
    out = list(groups.values())
    out.sort(key=lambda q: q["weights_bytes"])
    return out


def has_projector(siblings: list[dict]) -> bool:
    return any(
        (s.get("rfilename") or "").lower().endswith(".gguf")
        and re.search(r"(?:^|/)mmproj", s.get("rfilename") or "", re.I)
        for s in siblings or []
    )


async def hf_search(
    query: str = "",
    sort: str = "popular",
    cursor: str | None = None,
    limit: int = PAGE,
    *,
    timeout: float = 20.0,
) -> tuple[list[dict], str | None]:
    """One page of GGUF repositories, and the cursor for the next one."""
    import httpx

    params: list[tuple[str, Any]] = [
        ("filter", "gguf"),
        ("sort", _HF_SORT.get(sort, "downloads")),
        ("direction", -1),
        ("limit", max(1, min(int(limit or PAGE), 100))),
    ]
    if query:
        params.append(("search", query))
    if cursor:
        params.append(("cursor", cursor))
    params += [("expand[]", field) for field in _HF_EXPAND]
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(HF_API, params=params)
        resp.raise_for_status()
        return resp.json(), _next_cursor(resp.headers.get("link"))


def _next_cursor(link_header: str | None) -> str | None:
    """Hugging Face pages with an opaque cursor in a Link header. Only the
    cursor is kept and the URL is rebuilt here, so a header from the network
    can never redirect our next request somewhere else."""
    match = re.search(r'<([^>]+)>;\s*rel="next"', link_header or "")
    if not match:
        return None
    values = parse_qs(urlparse(match.group(1)).query).get("cursor") or []
    return values[0] if values else None


async def hf_repo_detail(repo: str, *, timeout: float = 25.0) -> dict:
    import httpx

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(f"{HF_API}/{repo}", params={"blobs": "true"})
        resp.raise_for_status()
        return resp.json()


# ── One row shape ──────────────────────────────────────────────────────────

# Q4_K_M, the quantization both libraries default to, lands near 4.8 bits per
# weight. Used only to filter and to warn; exact bytes arrive with the variant
# list, and the truth arrives with /api/show after the install.
BITS_PER_WEIGHT = 4.8


def estimate_bytes(params: int) -> int:
    return int(params * BITS_PER_WEIGHT / 8) if params else 0


def _template_text(gguf: dict) -> str:
    template = (gguf or {}).get("chat_template")
    if isinstance(template, list):
        return " ".join(str(t.get("template") or "") for t in template if isinstance(t, dict))
    return str(template or "")


def _band_of(params: int) -> str:
    for band, (low, high) in PARAM_BANDS.items():
        if params >= low and (high is None or params < high):
            return band
    return "large"


def _variants_installed(tags: set[str], prefix: str) -> list[str]:
    """Which versions of this entry are already here, by their tag suffix."""
    lowered = prefix.lower()
    out = []
    for tag in tags:
        low = tag.lower()
        if low == lowered:
            out.append("latest")
        elif low.startswith(f"{lowered}:"):
            out.append(tag[len(prefix) + 1:])
    return sorted(out)


def _same_size(a: int, b: int, tolerance: float = 0.02) -> bool:
    """Two counts of the same model's parameters, allowing for how each
    library rounds and whether a projector was counted in."""
    return bool(a and b) and abs(a - b) <= tolerance * max(a, b)


# What may follow a model's name and still be the same model: nothing, the
# repacker's suffix, or a separator into its parameter size.
_NAME_ENDS = re.compile(r"^(?:$|[-_ ]gguf\b|[-_ ]\d)", re.I)


def _names_match(family: str, path: str) -> bool:
    """Is `family` the whole name in `path`, rather than the start of another?

    `qwen3.8` is the model in `unsloth/Qwen3.8-27B-GGUF`, because what follows
    it is its size. `qwen3` is not, twice over: in `Qwen3.8-27B` the name keeps
    going into another digit, and in `qwen3-coder-30b` it keeps going into
    another word. Both are different models that merely start the same way.
    """
    if not family or not path:
        return False
    family, path = family.lower(), path.lower()
    return any(_NAME_ENDS.match(path[m.end():])
               for m in re.finditer(re.escape(family), path))


def installed_index(registry: dict[str, dict] | None) -> list[tuple]:
    """Installed models in the shapes a browse row can be recognised by.

    The point is that the same weights carry the same name whichever library
    they came through, so pulling Qwen3.8 from Ollama has to stop Hugging Face
    offering to download it again under a repository name.
    """
    out = []
    for model_id, profile in (registry or {}).items():
        path = hf_repo(model_id) if is_hf_tag(model_id) else model_id.split(":")[0]
        out.append((
            model_id,
            profile.get("family") or "",
            int(profile.get("params") or 0),
            path,
        ))
    return out


def same_weights_warning(model_id: str) -> str:
    """Architecture and parameter count identify a model, not a copy of it: an
    abliterated or uncensored fine-tune matches its base on both. So this says
    what is actually known and leaves the judgement to the reader."""
    return (f"Same size and architecture as {model_id}, which you already have. "
            "If this is a repack rather than a fine-tune, it is the same weights twice.")


def already_installed_as(
    index: list[tuple], *, family: str = "", params: int = 0, name: str = ""
) -> str:
    """The installed model that is these same weights from the other library.

    Architecture and parameter count are read from the GGUF header by both
    libraries, so they agree exactly; the name is the fallback for an Ollama
    row, whose architecture is not known until something is installed.
    """
    for model_id, its_family, its_params, its_path in index:
        if not _same_size(params, its_params):
            continue
        if family and its_family and family == its_family:
            return model_id
        if _names_match(name, its_path) or _names_match(its_path.split("/")[-1], name):
            return model_id
    return ""


def normalize_ollama(row: dict, *, installed: set[str], budget: int,
                     index: list[tuple] | None = None) -> dict:
    caps = set(row.get("capabilities") or [])
    params = int(row.get("min_params") or 0)
    estimated = estimate_bytes(params)
    here = _variants_installed(installed, row["name"])
    warnings = []
    if "tools" not in caps:
        warnings.append("No tool support, so it can chat but cannot run Agentic.")

    # Every parameter size this family comes in, against everything installed.
    elsewhere = ""
    if not here:
        for variant in row.get("variants") or []:
            elsewhere = already_installed_as(
                index or [], params=parse_params(variant), name=row["name"])
            if elsewhere:
                break
    if elsewhere:
        warnings.append(same_weights_warning(elsewhere))
    return {
        "source": "ollama",
        "id": row["name"],
        "tag": row["name"],
        "name": row["name"],
        "publisher": "ollama",
        "description": row.get("description") or "",
        "params": params,
        "params_max": int(row.get("params") or 0),
        "size_label": ", ".join(row.get("variants") or []),
        "estimated_bytes": estimated,
        "context_length": 0,
        "downloads": int(row.get("downloads") or 0),
        "likes": None,
        "updated": row.get("updated") or "",
        "variant_count": int(row.get("tag_count") or 0),
        "capabilities": {
            "tools": "tools" in caps,
            "vision": "vision" in caps,
            "thinking": "thinking" in caps,
        },
        "installed": bool(here),
        "installed_variants": here,
        "elsewhere": elsewhere,
        "fits": None if not estimated else (estimated + RUNTIME_OVERHEAD) <= budget,
        "warnings": warnings,
        "band": _band_of(params),
    }


def _hf_description(row: dict, gguf: dict) -> str:
    """Hugging Face has no description field, so this is assembled from what
    the repository does state. `base_model` is the useful part: a GGUF repo is
    almost always somebody's quantization of a model named there."""
    card = row.get("cardData") or {}
    base = card.get("base_model")
    if isinstance(base, list):
        base = base[0] if base else None
    bits = []
    if base and str(base).lower() not in (row.get("id") or "").lower():
        bits.append(f"Quantized from {base}")
    elif gguf.get("architecture"):
        bits.append(f"{gguf['architecture']} weights in GGUF")
    context = int(gguf.get("context_length") or 0)
    if context:
        bits.append(f"{context // 1024}K context")
    if card.get("license"):
        bits.append(str(card["license"]))
    return " · ".join(bits)


def normalize_hf(row: dict, *, installed: set[str], budget: int,
                 index: list[tuple] | None = None) -> dict:
    repo = row.get("id") or ""
    gguf = row.get("gguf") or {}
    params = int(gguf.get("total") or 0)
    estimated = estimate_bytes(params)
    template = _template_text(gguf)
    context = int(gguf.get("context_length") or 0)
    pipeline = row.get("pipeline_tag") or ""
    here = _variants_installed(installed, f"{HF_PREFIX}{repo}")

    warnings = []
    if row.get("gated"):
        warnings.append("Gated: needs a Hugging Face account with access granted to this repository.")
    if not gguf:
        warnings.append("Hugging Face has not read this repository's GGUF header, so its size and "
                        "abilities are unknown until it is installed.")
    elif not template:
        warnings.append("No chat template, so Ollama has to guess how to format the conversation. "
                        "Replies may come out with raw markers in them.")
    elif "tools" not in template:
        warnings.append("The chat template has no tool support, so it can chat but cannot run Agentic.")
    if pipeline == "image-text-to-text":
        warnings.append("Vision needs a separate projector file that an Ollama import leaves behind, "
                        "so this will be text-only.")
    if context and context < 8192:
        warnings.append(f"Short {context // 1024}K context window.")

    # The architecture and parameter count come from the same GGUF header
    # Ollama reads, so this catches the same model arriving under a repository
    # name that looks nothing like the Ollama tag already installed.
    elsewhere = "" if here else already_installed_as(
        index or [], family=gguf.get("architecture") or "", params=params,
        name=repo.split("/")[-1])
    if elsewhere:
        warnings.append(same_weights_warning(elsewhere))

    return {
        "source": "huggingface",
        "id": repo,
        "tag": f"{HF_PREFIX}{repo}",
        "name": repo.split("/")[-1],
        "publisher": row.get("author") or repo.split("/")[0],
        "description": _hf_description(row, gguf),
        "params": params,
        "params_max": params,
        "size_label": size_label(params),
        "estimated_bytes": estimated,
        "context_length": context,
        "downloads": int(row.get("downloads") or 0),
        "likes": int(row.get("likes") or 0),
        "updated": row.get("lastModified") or "",
        "variant_count": 0,
        "capabilities": {
            "tools": bool(template) and "tools" in template,
            "vision": pipeline == "image-text-to-text",
            "thinking": "think" in template.lower() if template else False,
        },
        "installed": bool(here),
        "installed_variants": here,
        "elsewhere": elsewhere,
        "fits": None if not estimated else (estimated + RUNTIME_OVERHEAD) <= budget,
        "warnings": warnings,
        "band": _band_of(params),
        "architecture": gguf.get("architecture") or "",
    }


def is_chattable(row: dict) -> bool:
    """Embedding and reranking models have no place in a chat picker."""
    pipeline = row.get("pipeline_tag") or ""
    if pipeline in _EMBEDDING_PIPELINES:
        return False
    caps = set(row.get("capabilities") or []) if isinstance(row.get("capabilities"), list) else set()
    return "embedding" not in caps


# ── Browsing ───────────────────────────────────────────────────────────────
#
# One screen, two libraries, one scroll. Ollama's list is small and held
# locally, so it sorts and filters exactly. Hugging Face is streamed a page at
# a time behind its own cursor, and the band and fit filters are applied to
# each page as it arrives, so a filtered scroll walks several upstream pages
# to fill one of ours rather than returning three rows and looking finished.

_MAX_UPSTREAM_PAGES = 5


def _passes(entry: dict, band: str, fits_only: bool) -> bool:
    if band and entry.get("band") != band:
        return False
    if fits_only and entry.get("fits") is False:
        return False
    return True


def _sort_ollama(rows: list[dict], sort: str) -> list[dict]:
    if sort in ("newest", "updated"):
        return sorted(rows, key=lambda r: _updated_key(r.get("updated")), reverse=True)
    return sorted(rows, key=lambda r: int(r.get("downloads") or 0), reverse=True)


def _updated_key(text: str | None) -> float:
    """Ollama prints "Nov 30, 2024 10:34 PM UTC"; Hugging Face sends ISO."""
    raw = (text or "").strip()
    if not raw:
        return 0.0
    for fmt in ("%b %d, %Y %I:%M %p UTC", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return time.mktime(time.strptime(raw, fmt))
        except ValueError:
            continue
    return 0.0


async def browse(
    source: str = "ollama",
    query: str = "",
    sort: str = "popular",
    cursor: str | None = None,
    *,
    band: str = "",
    fits_only: bool = False,
    registry: dict[str, dict] | None = None,
    budget: int = 0,
    limit: int = PAGE,
) -> dict:
    """One page of one library, normalized, filtered and ready to render."""
    installed = set(registry or {})
    index = installed_index(registry)
    sort = sort if sort in SORTS else "popular"
    band = band if band in PARAM_BANDS else ""

    if source == "huggingface":
        items: list[dict] = []
        next_cursor = cursor
        pages = 0
        while len(items) < limit and pages < _MAX_UPSTREAM_PAGES:
            rows, next_cursor = await hf_search(query, sort, next_cursor, limit=limit)
            pages += 1
            for row in rows:
                if not is_chattable(row):
                    continue
                entry = normalize_hf(row, installed=installed, budget=budget, index=index)
                if _passes(entry, band, fits_only):
                    items.append(entry)
            if not next_cursor:
                break
        return {"source": source, "items": items, "next": next_cursor, "total": None}

    rows = [r for r in await ollama_library() if is_chattable(r)]
    words = (query or "").strip().lower().split()
    if words:
        def matches(row: dict) -> bool:
            haystack = f"{row['name']} {row.get('description') or ''}".lower()
            return all(word in haystack for word in words)
        rows = [r for r in rows if matches(r)]
    rows = _sort_ollama(rows, sort)
    entries = [normalize_ollama(r, installed=installed, budget=budget, index=index)
               for r in rows]
    entries = [e for e in entries if _passes(e, band, fits_only)]
    start = int(cursor or 0)
    page = entries[start:start + limit]
    following = start + len(page)
    return {
        "source": source,
        "items": page,
        "next": str(following) if following < len(entries) else None,
        "total": len(entries),
    }


async def variants(source: str, model_id: str, *, budget: int = 0) -> dict:
    """Every installable version of one entry, largest fit first."""
    if source == "huggingface":
        detail = await hf_repo_detail(model_id)
        siblings = detail.get("siblings") or []
        rows = parse_quants(siblings)
        notes = []
        if has_projector(siblings):
            notes.append("This repository ships a vision projector (mmproj) that an Ollama "
                         "import leaves behind, so the model will be text-only here.")
        if not rows:
            notes.append("No installable GGUF weights in this repository.")
        out = [{
            "tag": f"{HF_PREFIX}{model_id}:{row['label']}",
            "label": row["label"],
            "weights_bytes": row["weights_bytes"],
            "context_length": int((detail.get("gguf") or {}).get("context_length") or 0),
            "parts": row["parts"],
        } for row in rows]
    else:
        rows = await ollama_variants(model_id)
        notes = []
        out = [{
            "tag": row["tag"],
            "label": row["label"],
            "weights_bytes": row["weights_bytes"],
            "context_length": row["context_length"],
            "parts": 1,
        } for row in rows]
        if not out:
            out = [{"tag": f"{model_id}:latest", "label": "latest",
                    "weights_bytes": 0, "context_length": 0, "parts": 1}]
    for row in out:
        needed = (row["weights_bytes"] or 0) + RUNTIME_OVERHEAD
        row["fits"] = bool(budget) and row["weights_bytes"] > 0 and needed <= budget
        row["recommended"] = False

    # Quantization trades accuracy for room, so the best version of a model is
    # the largest one this machine can actually hold. It goes first, because a
    # list of twenty-five is a question nobody wants to be asked.
    best = max((r for r in out if r["fits"]), key=lambda r: r["weights_bytes"], default=None)
    if best:
        best["recommended"] = True
        out.remove(best)
        out.insert(0, best)
    return {"source": source, "id": model_id, "variants": out, "notes": notes}


def hf_tag_size(tag: str, *, timeout: float = 15.0) -> int:
    """Download bytes behind one `hf.co/owner/repo:QUANT` tag, 0 when the
    quantization is not in the repository. Uses urllib rather than httpx so
    the installer can call it before pip has run, which is the same reason
    models.manifest_size does."""
    import urllib.request

    body = re.sub(r"^(hf\.co|huggingface\.co)/", "", (tag or "").strip(), flags=re.I)
    repo, _, quant = body.rpartition(":")
    if not repo or "/" not in repo:
        return 0
    url = f"{HF_API}/{repo}?blobs=true"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            siblings = json.loads(resp.read()).get("siblings") or []
    except Exception:
        return 0
    wanted = quant.lower()
    return sum(row["weights_bytes"] for row in parse_quants(siblings)
               if row["label"].lower() == wanted)
