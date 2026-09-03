# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""Web search helper: DuckDuckGo, page fetch, and wttr.in for weather."""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urlparse

import httpx

_UA = "Mozilla/5.0 (compatible; AetherBot/1.0; +local)"
_WEATHER_RE = re.compile(
    r"\b(weather|temperature|forecast|humidity|wind\s*chill|radar|"
    r"how\s+hot|how\s+cold|rain(?:ing)?|snow(?:ing)?|humid)\b",
    re.I,
)
# "can you search", "look it up", "google that": not a real topic
_META_SEARCH_RE = re.compile(
    r"^\s*((hey|hi|hello|ok|okay|please)[,!]?\s+)*"
    r"((can|could|would|will)\s+you\s+)?"
    r"(please\s+)?"
    r"(search(\s+(the\s+)?web)?(\s+(for\s+(it|that|this|online))?)?|"
    r"look(\s+it)?\s+up|"
    r"google(\s+it|\s+that)?|"
    r"check(\s+(online|the\s+web))?|"
    r"find\s+(out|it|that)|"
    r"use\s+(web\s+)?search|"
    r"do\s+a\s+(web\s+)?search)"
    r"\s*[?.!]?\s*$",
    re.I,
)
_SEARCH_FOR_RE = re.compile(
    r"^\s*((hey|hi|hello|ok|okay|please)[,!]?\s+)*"
    r"((can|could|would|will)\s+you\s+)?"
    r"(please\s+)?"
    r"(search(\s+the\s+web)?\s+(for\s+)?|look\s+up\s+|google\s+|find\s+(out\s+)?)"
    r"(.+?)\s*$",
    re.I,
)
_CITY_STATE_RE = re.compile(
    r"\b([A-Za-z][A-Za-z .'-]{0,40}?),\s*([A-Z]{2})\b"
)
_CITY_STATE_SPACE_RE = re.compile(
    r"\b([A-Za-z][A-Za-z .'-]{0,40}?)\s+([A-Z]{2})\b"
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg", "nav", "footer", "header"}:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg", "nav", "footer", "header"} and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip:
            return
        t = data.strip()
        if t:
            self._chunks.append(t)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._chunks)).strip()


def strip_research_block(text: str) -> str:
    """Remove injected web-research blocks from a stored user message."""
    if not text:
        return ""
    return re.split(r"\n\s*# Web research \(live\)\b", text, maxsplit=1)[0].strip()


def is_meta_search_request(query: str) -> bool:
    q = (query or "").strip()
    if not q:
        return False
    return bool(_META_SEARCH_RE.match(q))


def should_auto_web_search(query: str) -> bool:
    """Run research even if the toggle is off (explicit search ask, or live weather)."""
    q = (query or "").strip()
    if not q:
        return False
    if is_meta_search_request(q):
        return True
    if _SEARCH_FOR_RE.match(q) and not is_meta_search_request(q):
        return True
    # Weather needs live data, so do not make the user hunt for the toggle
    if _WEATHER_RE.search(q):
        return True
    return False


def resolve_research_query(query: str, prior_user_messages: list[str] | None = None) -> str:
    """
    Pick what to actually search.
    'can you search' → last real user question.
    'search for X' → X.
    """
    q = strip_research_block(query or "").strip()
    if not q:
        return ""

    if is_meta_search_request(q):
        for prev in reversed(prior_user_messages or []):
            prev = strip_research_block(prev)
            if prev and not is_meta_search_request(prev):
                return prev
        return q

    m = _SEARCH_FOR_RE.match(q)
    if m:
        topic = (m.group(m.lastindex) or "").strip(" \t?.!")
        if topic and not is_meta_search_request(topic) and len(topic) > 2:
            return topic
    return q


def _extract_place(query: str) -> str:
    raw = (query or "").strip()
    # Drop chat fluff / weather words so city+state stands alone
    q = re.sub(r"[?!]+", " ", raw)
    q = re.sub(
        r"\b("
        r"hey|hi|hello|please|whats|what(?:'s|\s+is)|the|current|right\s+now|"
        r"today|tonight|this\s+week|how\s+hot|how\s+cold|"
        r"weather|temperature|forecast|humidity|wind\s*chill|radar|rain(?:ing)?|snow(?:ing)?|humid"
        r")\b",
        " ",
        q,
        flags=re.I,
    )
    q = re.sub(r"\s+", " ", q).strip(" \t-:,.")
    q = re.sub(r"^(?:in|at|near)\s+", "", q, flags=re.I).strip()

    m = re.search(r"^([A-Za-z][A-Za-z .'-]{0,40}?),\s*([A-Z]{2})\b", q)
    if m:
        return f"{m.group(1).strip()}, {m.group(2)}"

    m = re.search(
        r"\b([A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,2})\s+([A-Z]{2})\b",
        q,
    )
    if m:
        return f"{m.group(1).strip()}, {m.group(2)}"

    return q or raw


def weather_lookup(place: str) -> dict[str, Any] | None:
    place = (place or "").strip()
    if not place:
        return None
    url = f"https://wttr.in/{quote(place)}?format=j1"
    try:
        with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": _UA}) as client:
            r = client.get(url)
            r.raise_for_status()
            data = r.json()
    except Exception:
        return None
    try:
        cur = data["current_condition"][0]
        area = data["nearest_area"][0]
        name = area["areaName"][0]["value"]
        region = area.get("region", [{"value": ""}])[0].get("value") or ""
        country = area.get("country", [{"value": ""}])[0].get("value") or ""
        where = ", ".join(x for x in [name, region, country] if x)
        desc = (cur.get("weatherDesc") or [{"value": ""}])[0].get("value") or ""
        forecast_lines = []
        for day in (data.get("weather") or [])[:3]:
            date = day.get("date") or ""
            avg = day.get("avgtempF") or day.get("avgtempC") or "?"
            mx = day.get("maxtempF") or "?"
            mn = day.get("mintempF") or "?"
            ddesc = ""
            try:
                ddesc = day["hourly"][4]["weatherDesc"][0]["value"]
            except Exception:
                pass
            forecast_lines.append(f"  - {date}: avg {avg}°F (low {mn}°F / high {mx}°F) {ddesc}".rstrip())
        return {
            "place": where or place,
            "temp_f": cur.get("temp_F"),
            "feels_like_f": cur.get("FeelsLikeF"),
            "humidity": cur.get("humidity"),
            "wind_mph": cur.get("windspeedMiles"),
            "description": desc,
            "forecast": "\n".join(forecast_lines),
        }
    except Exception:
        return None


def web_search(query: str, max_results: int = 6) -> list[dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        return []
    try:
        from search_router import clean_query
        cleaned = clean_query(query)
        if len(cleaned.split()) >= 2:
            query = cleaned
    except Exception:
        pass
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError as e:
            raise RuntimeError("ddgs package not installed") from e

    out: list[dict[str, Any]] = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=max_results):
            out.append({
                "title": r.get("title") or "",
                "url": r.get("href") or r.get("url") or "",
                "snippet": r.get("body") or r.get("snippet") or "",
            })
    return out


def fetch_page_text(url: str, max_chars: int = 2800) -> str:
    if not url or not url.startswith("http"):
        return ""
    host = urlparse(url).netloc.lower()
    # Skip known JS-heavy shells that yield nothing useful without a browser
    if any(h in host for h in ("twitter.com", "x.com", "facebook.com", "instagram.com")):
        return ""
    try:
        with httpx.Client(timeout=12.0, follow_redirects=True, headers={"User-Agent": _UA}) as client:
            r = client.get(url)
            if r.status_code >= 400:
                return ""
            ctype = (r.headers.get("content-type") or "").lower()
            if "html" not in ctype and "text/" not in ctype:
                return ""
            raw = r.text
    except Exception:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        return ""
    text = parser.text()
    if len(text) < 80:
        return ""
    return text[:max_chars]


def enrich_results(results: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    enriched = []
    fetched = 0
    for r in results:
        item = dict(r)
        if fetched < limit and r.get("url"):
            page = fetch_page_text(r["url"])
            if page:
                item["page_text"] = page
                fetched += 1
        enriched.append(item)
    return enriched


def run_web_research(query: str, max_results: int = 6) -> dict[str, Any]:
    """Full research pack for the model: weather (if relevant) + search + page excerpts."""
    query = (query or "").strip()
    pack: dict[str, Any] = {"query": query, "weather": None, "results": []}
    if not query:
        return pack
    if _WEATHER_RE.search(query):
        place = _extract_place(query)
        pack["place_query"] = place
        pack["weather"] = weather_lookup(place)
    try:
        results = web_search(query, max_results=max_results)
        pack["results"] = enrich_results(results, limit=3)
    except Exception as e:
        pack["error"] = str(e)
    return pack


def format_search_context(results: list[dict[str, Any]] | dict[str, Any]) -> str:
    """Accept either a raw result list (legacy) or a research pack."""
    if isinstance(results, dict) and ("results" in results or "weather" in results):
        pack = results
        lines = [
            "# Web research (live)",
            "Use the facts below as ground truth for current information. Prefer Current weather / page excerpts over vague homepage blurbs. Cite sources by name or URL when helpful.",
        ]
        if pack.get("query"):
            lines.append(f"Research query: {pack['query']}")
        if pack.get("router_reason"):
            lines.append(f"(router: {pack['router_reason']})")
        if pack.get("error"):
            lines.append(f"[search error: {pack['error']}]")
        w = pack.get("weather")
        if w:
            lines.append("")
            lines.append("## Current weather (wttr.in)")
            lines.append(
                f"- Location: {w.get('place')}\n"
                f"- Now: {w.get('temp_f')}°F (feels like {w.get('feels_like_f')}°F), {w.get('description')}\n"
                f"- Humidity: {w.get('humidity')}% · Wind: {w.get('wind_mph')} mph"
            )
            if w.get("forecast"):
                lines.append("- Forecast:\n" + w["forecast"])
        results_list = pack.get("results") or []
        if results_list:
            lines.append("")
            lines.append("## Search results")
            for i, r in enumerate(results_list, 1):
                lines.append(f"{i}. {r.get('title')}\n   {r.get('url')}\n   {r.get('snippet') or ''}")
                if r.get("page_text"):
                    lines.append(f"   Excerpt: {r['page_text'][:1200]}")
        if not w and not results_list:
            lines.append("No web results.")
        return "\n".join(lines)

    # legacy list format
    if not results:
        return "No web results."
    lines = ["# Web search results", "Use these results for current facts when answering."]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title')}\n   {r.get('url')}\n   {r.get('snippet')}")
        if r.get("page_text"):
            lines.append(f"   Excerpt: {r['page_text'][:1200]}")
    return "\n".join(lines)
