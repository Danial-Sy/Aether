# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Danial Syed
"""
Search router: deterministic gates plus follow-up carry.

Decides whether a turn needs a live web search and what query to run, using the
current message plus prior chat context. No LLM call required for the common path.

Toggle semantics (server):
  force_on  → always search
  auto      → this router decides (default when toggle is off)
  Message "don't search" always vetoes.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------- regex gates
_SEARCH_VERB = re.compile(
    r"\b(searche?|serach|saerch|seach|searh|sarch|google|googel|"
    r"look (it )?up|lookup|look into|pull up|find out|find me)\b",
    re.I,
)
_NO_SEARCH = re.compile(
    r"\b(don'?t|do not|stop|quit|no need to|never|not|without|won'?t need to|"
    r"you don'?t (have|need) to)\s+(\w+\s+){0,3}"
    r"(searche?\w*|google\w*|look(ing)?\s*(it\s*)?up|check(ing)?)\b",
    re.I,
)
_PERSONAL_Q = re.compile(
    r"\b(what|how|where|who)\s+(do|does|would|did|will|are|is)\s+(you|u|ya)\b"
    r"|\bwhat\s+(you|u|ya)\s+(want|think|feel|like|prefer|wish|imagine|choose|be)\b"
    r"|\bdo you (want|like|prefer|think|feel|wish|imagine)\b"
    r"|\byour (?:\w+\s+)?(opinion|take|favorite|favourite|body|style|personality|name|"
    r"choice|pick|vibe|look|design)\b",
    re.I,
)
_CURRENT_INFO = re.compile(
    r"\b(who won|what.?s the score|the score|how much (is|are|does|do)|price of|weather|"
    r"temperature|forecast|humidity|latest|the news|headlines|stock price|exchange rate|"
    r"how\s+hot|how\s+cold|rain(?:ing)?|snow(?:ing)?)\b",
    re.I,
)
_LEAD = re.compile(
    r"^(hey|yo|ok|okay|so|and|please|could you|can you|would you|will you|"
    r"aether)[\s,]*",
    re.I,
)
_ENTITY = re.compile(
    r"\b[A-Z][\w']+(?:\s+(?:of|the|and|&)\s+|\s+)[A-Z0-9][\w']*"
    r"|\b[A-Za-z][\w']{2,}\s?\d+\b"
    r"|\b[A-Za-z]{2,4}\d+\b"
    r"|\b[A-Z]{3,}\b"
)
_SHOUT_WORDS = frozenset("""
WHAT WHY HOW WHO WHEN WHERE WHICH THIS THAT THESE THOSE THERE THEN THAN
YES NO NAH YEAH YEP YUP NOPE OK OKAY SURE FINE REALLY VERY SO SUCH JUST
LOL LMAO LMFAO OMG OMFG WTF IDK IDC IMO IMHO TBH NGL FR SMH BRUH BRO DUDE MAN BUD
WOW WOAH WHOA DAMN DANG SHIT FUCK HELL GOD JESUS
RIGHT EXACTLY TRUE TOTALLY DEFINITELY ABSOLUTELY HONESTLY SERIOUSLY LITERALLY ACTUALLY FINALLY
PEAK FIRE GOATED GOAT BASED CRINGE MID SICK DOPE LIT WILD CRAZY INSANE NUTS
HUGE MASSIVE TINY BIG SMALL BEST WORST GOOD BAD GREAT AWFUL AMAZING TERRIBLE
LOVE HATE NEED WANT LIKE STOP GO HELP PLEASE THANKS THANK SORRY
NEVER ALWAYS EVERY ALL NONE NOTHING EVERYTHING ANYTHING SOMETHING
AND BUT OR THE YOU YOUR MY ME IT IS ARE WAS WERE BE AM DO DID CAN WILL
DONT CANT WONT ISNT ARENT ITS IM IVE ILL WE US OUR THEY THEM HIS HER
NOW SOON LATER TODAY YESTERDAY TOMORROW AGAIN STILL EVEN ONLY MORE LESS
WAY OH HOLY HECK FRICK LEGIT FACTS CAP NAW BET DEADASS LOWKEY HIGHKEY
MUCH TOO REAL DEAD SAME BROO BROOO AYO YO HEY HUH WELL OOF EW UGH
""".split())

_QUESTIONISH = re.compile(
    r"\?|^\s*(what|who|when|where|which|how|why|is|are|was|were|does|do|did|can|could)"
    r"(?:'?s)?\b",
    re.I,
)
_OPINIONY = re.compile(
    r"\b(favorite|favourite|best|worst|better|prefer|rank|opinion|take on|thoughts on|"
    r"do you (like|think|feel)|would you)\b"
    r"|\b(?:you|u|ya)\s+(?:main|run|play|rock|use|watch|watched|seen|read|into)\b"
    r"|\bwho(?:'?s| is) your\b"
    r"|\bwho\s+(?:would\s+)?win(?:s)?\b|\bwins\s+(?:in\s+)?a\s+(?:fight|match|duel)\b"
    r"|\b(?:vs\.?|versus)\b|\bstronger\b|\bweaker\b|\bmid\s?diff\b"
    r"|\bpower[\s-]?scal|\bbe\s+honest\b|\bhot\s+take\b|\bcould\s+\w+\s+beat\b",
    re.I,
)
_SELF_DIRECTED = re.compile(r"\b(you|your|yours|u|ya|yourself|aether|we|us|our)\b", re.I)
_REMINISCE = re.compile(
    r"\b(you remember|remember (?:when|that|the|how)|do you remember|recall when|"
    r"that (?:part|bit|scene|moment) (?:where|when)|the (?:part|bit|scene) (?:where|when)|"
    r"back when|thinking about (?:when|that)|wasn'?t there a)\b",
    re.I,
)
_SCOREISH = re.compile(
    r"\b(zero|one|two|three|four|five|six|seven|eight|nine|ten|nil|\d+)\s*(?:to|[-–:])\s*"
    r"(zero|one|two|three|four|five|six|seven|eight|nine|ten|nil|\d+)\b",
    re.I,
)
_TIME_SENSITIVE = re.compile(
    r"\b(won|win|wins|beat|lost|score|match|game|vs\.?|versus|result|results|final|playoff|"
    r"price|stock|news|latest|current|today|tonight|yesterday|election|champion|standings|"
    r"release|released|who is the|who's the|weather|forecast|temperature)\b",
    re.I,
)
_DATE_Q = re.compile(
    r"\bwhat\s+(day|date|time)\s+is\s+(it|today)\b"
    r"|\bwhat('?s|\s+is)\s+(the\s+)?(date|day|time)\s+(today|right\s+now)\b"
    r"|\bwhat('?s|\s+is)\s+the\s+date\s*\??\s*$"
    r"|\btoday'?s\s+date\b",
    re.I,
)
_WH = re.compile(r"(?:^|[.!?,;]\s*)(what|who|when|where|which|why|how)(?:'?s)?\b", re.I)
_WH_ANY = re.compile(r"\b(what|whats|who|whos|when|where|which|why|how|hows)\b", re.I)
_SMALLTALK = re.compile(
    r"^(hey|hi|hello|yo|sup|hiya|heya|what'?s\s+up|wassup|"
    r"how'?s\s+it\s+going|how\s+are\s+(you|u|ya)|how\s+you\s+doin[g']?|"
    r"good\s+(morning|afternoon|evening|night)|"
    r"thanks|thank\s+you|ty|thx|lol|lmao|haha|hehe|nice|cool|sweet|awesome|damn|"
    r"ok|okay|kk|got\s+it|gotcha|sure|yeah|yep|yup|nah|nope|true|fair|"
    r"nvm|never\s*mind|brb|bye|later|see\s+ya|cya|goodnight|gn|night)"
    r"[\s,.!?]*$",
    re.I,
)
_CONTINUATION = re.compile(
    r"\b(that|thats|those|these|this|it|its|he|she|they|them|him|her|his|their|"
    r"same|too|also|either|both)\b"
    r"|\b(real|really|so|pretty|actually|definitely|honestly|deadass|fr|ngl|tbh)\b"
    r"|\b(better|worse|best|worst|good|bad|great|crazy|wild|insane|nuts|solid|clean|neat)\b",
    re.I,
)
_FOLLOWUP_SHAPE = re.compile(
    r"^\s*(and|but|ok|okay|oh|so|what about|how about|hows about)?\s*"
    r"(.{0,40}?\b("
    r"(at|around|by)\s+(like|about|around|maybe|say)?\s*\d{1,2}(:\d{2})?\s*(am|pm)?"
    r"|^\s*\d{1,2}(:\d{2})?\s*(am|pm)\b"
    r"|tomorrow|tonight|later|this (evening|afternoon|morning|weekend)"
    r"|next (week|hour|day)|then|after that|earlier"
    r")\b)",
    re.I,
)
_HAS_PROPER = re.compile(r"\b[A-Z][a-z]{2,}")
_CARRY_SKIP = re.compile(
    r"^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*$"
    r"|^(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*$"
    r"|^(?:today|tonight|tomorrow|yesterday)$"
    r"|^(?:current|currently|latest|live|now|recent|recently)$"
    r"|^(?:am|pm)$"
    r"|^\d{4}$",
    re.I,
)
_PIVOT = re.compile(r"\b(?:what|how)'?s?\s+about\b", re.I)
_META_ONLY = re.compile(
    r"^\s*((hey|hi|hello|ok|okay|please)[,!]?\s+)*"
    r"((can|could|would|will)\s+you\s+)?"
    r"(please\s+)?"
    r"(search(\s+(the\s+)?web)?(\s+(for\s+(it|that|this|online))?)?|"
    r"look(\s+it)?\s+up|google(\s+it|\s+that)?|"
    r"check(\s+(online|the\s+web))?|find\s+(out|it|that)|"
    r"use\s+(web\s+)?search|do\s+a\s+(web\s+)?search)"
    r"\s*[?.!]?\s*$",
    re.I,
)
_AGAIN = re.compile(
    r"\b(again|recap|remind me|what was (that|it)|same (one|game|match|thing)|once more)\b",
    re.I,
)

_QUERY_STOP = frozenset("""
a an the this that these those my your our their his her its
i me my you your he she it we they them us him her
is are was were be been being am s re m ve ll
do does did done doing have has had having get got
can could will would shall should may might must
what whats what's which who who's whos whose when where why how
hows how's wheres where's whens when's whys why's thats that's theres there's heres here's
there here oh ah um uh
about at by for from in into of on onto to with without
and or but so if then than as too also just still even
no not nah yeah yep yes ok okay
""".split())

_SEARCH_NOISE = re.compile(
    r"\b(oh|ooo+|ah+|aight|yo|hey|man|bro|dude|buddy|lol|lmao|honestly|i mean|like|"
    r"you are|youre|thats|that's|its|it's|im|i'?m|ive|i'?ve|really|actually|of course|"
    r"absolutely|definitely|kinda|sorta|yeah|yea|nah|peak|sharp|awesome|excellent|amazing|"
    r"but|and|so|well|just|gotta|wanna|smth|yk|idk|ngl|tbh|"
    r"gotcha|gotchu|what about|how about|specifically|exactly|tho|though|"
    r"alright|okay|ok|anyway|anyways|btw|wait|hmm+|cool|nice|"
    r"wow+|woah|whoa|bruh|geez|damn|sheesh)\b",
    re.I,
)
_SEARCH_MAX_WORDS = 12
_ROUTER_SKIP_MAX_WORDS = 45


@dataclass
class SearchDecision:
    search: bool
    query: str = ""
    reason: str = ""
    weather: bool = False


def strip_research_block(text: str) -> str:
    if not text:
        return ""
    return re.split(r"\n\s*# Web research \(live\)\b", text, maxsplit=1)[0].strip()


def clean_query(q: str) -> str:
    """Conversational sentence → search-engine query (Reedus clean_query)."""
    q = (q or "").strip()
    if not q:
        return q
    q = re.sub(r"[^\w\s'\-]", " ", q)
    q = _SEARCH_NOISE.sub(" ", q)
    words = [w for w in q.split() if w and w.lower().strip("'") not in _QUERY_STOP]
    if len(words) > _SEARCH_MAX_WORDS:
        words = words[-_SEARCH_MAX_WORDS:]
    return " ".join(words).strip()


def _entity_search(text: str):
    if not text:
        return None
    lower_words = sum(1 for w in re.findall(r"[A-Za-z]{2,}", text) if w.islower())

    def _shouted(w: str) -> bool:
        return w.upper() in _SHOUT_WORDS or (
            w.isupper() and w.isalpha() and len(w) >= 4 and lower_words >= 3
        )

    for m in _ENTITY.finditer(text):
        words = [w for w in re.split(r"[^A-Za-z0-9']+", m.group(0)) if w]
        if words and all(_shouted(w) for w in words):
            continue
        return m
    return None


def _positive_search_cmd(msg: str) -> bool:
    return bool(_SEARCH_VERB.search(msg)) and not _NO_SEARCH.search(msg)


# Question *shape*, as opposed to a message that merely contains a "?".
_INTERROGATIVE_OPEN = re.compile(
    r"^\s*(what|who|when|where|which|how|why|is|are|was|were|does|do|did|can|could)"
    r"(?:'?s)?\b",
    re.I,
)
# Above this, a trailing "?" is a conversational tag ("…use the mp3 instead ok?"),
# not a question about an entity.
_ENTITY_QUESTION_MAX_WORDS = 25


def _entity_question(msg: str) -> bool:
    if not _entity_search(msg):
        return False
    if _INTERROGATIVE_OPEN.search(msg):
        return True
    # A long instruction ending in "ok?" is not a question. The entity pattern
    # matches any word followed by a number, so "your 8 bit music" plus a "?"
    # was forcing a web search on an ordinary implementation request.
    return bool(_QUESTIONISH.search(msg)) and len(msg.split()) <= _ENTITY_QUESTION_MAX_WORDS


def _is_personal_turn(msg: str) -> bool:
    if _positive_search_cmd(msg) or _CURRENT_INFO.search(msg):
        return False
    if _OPINIONY.search(msg) and _SELF_DIRECTED.search(msg):
        return True
    if _entity_question(msg):
        return False
    return bool(_PERSONAL_Q.search(msg))


def _kw_wants_search(msg: str) -> bool:
    return bool(
        _SEARCH_VERB.search(msg)
        or _CURRENT_INFO.search(msg)
        or (_entity_search(msg) and _QUESTIONISH.search(msg))
    )


def _has_query_content(text: str) -> bool:
    if re.search(r"\d", text or ""):
        return True
    return any(
        w.strip("'").lower() not in _QUERY_STOP
        for w in re.findall(r"[\w'’\-]+", text or "")
    )


def _strip_lead_reaction(msg: str) -> str:
    m = (msg or "").strip()
    if not m:
        return msg
    piv = _PIVOT.search(m)
    if piv and piv.start() > 0:
        head, rest = m[: piv.start()], m[piv.start() :]
    else:
        c = m.find(",")
        if c <= 0:
            return msg
        head, rest = m[:c], m[c + 1 :]
    head, rest = head.strip(" ,"), rest.strip(" ,")
    if piv is None and len(head.split()) > 2:
        return msg
    if not head or re.search(r"\d", head) or any(t[:1].isupper() for t in head.split()):
        return msg
    if len(rest.split()) < 3 or not _has_query_content(rest):
        return msg
    return rest


def _kw_query(msg: str) -> str:
    q = _SEARCH_VERB.sub("", _LEAD.sub("", _strip_lead_reaction(msg).strip()))
    q = re.sub(r"\s+", " ", q).strip(" ?.!,")
    cleaned = clean_query(q)
    return cleaned or q or msg


def _is_search_followup(msg: str) -> bool:
    m = (msg or "").strip()
    if not m or len(m.split()) > 12 or _HAS_PROPER.search(m):
        return False
    sh = _FOLLOWUP_SHAPE.search(m)
    if not sh:
        return False
    rest = _strip_lead_reaction(m)
    if sh.group(3):
        rest = rest.replace(sh.group(3), " ")
    rest = re.sub(r"[^\w\s'’\-]", " ", rest)
    rest = _SEARCH_NOISE.sub(" ", rest)
    own = [w for w in re.findall(r"[\w'’\-]+", rest) if w.lower() not in _QUERY_STOP]
    return len(own) <= 1


def _followup_constraint(msg: str) -> str:
    m = _FOLLOWUP_SHAPE.search(msg or "")
    if not m:
        return ""
    c = re.sub(r"\b(?:like|about|around|maybe|say)\s+(?=\d)", "", m.group(3) or "", flags=re.I)
    return re.sub(r"\s+", " ", c).strip()


def _carry_subject(last_query: str) -> str:
    lq = last_query or ""
    m = _FOLLOWUP_SHAPE.search(lq)
    if m and m.group(3):
        lq = f"{lq[: m.start(3)]} {lq[m.end(3) :]}"
    out = []
    for w in lq.split():
        t = w.strip(",.?!'\"")
        if not t or _CARRY_SKIP.match(t):
            continue
        if t[:1].isupper() or t.lower() not in _QUERY_STOP:
            out.append(t)
    return " ".join(out[:5])


def _skip_search_router(msg: str) -> bool:
    ents = _entity_search(msg)
    if (
        _kw_wants_search(msg)
        or _positive_search_cmd(msg)
        or _CURRENT_INFO.search(msg)
        or ents
        or _SCOREISH.search(msg)
        or _DATE_Q.search(msg)
        or re.search(r"\d", msg)
    ):
        return False
    if _TIME_SENSITIVE.search(msg) and (ents or re.search(r"\d", msg) or _WH_ANY.search(msg)):
        return False
    core = _LEAD.sub("", msg).strip()
    if _SMALLTALK.match(core):
        return True
    if "?" in msg or _WH.search(core):
        if len(core.split()) > _ROUTER_SKIP_MAX_WORDS:
            return False
        return bool(_SELF_DIRECTED.search(msg) or _PERSONAL_Q.search(msg))
    return True


def _add_recency(query: str) -> str:
    if _TIME_SENSITIVE.search(query) and not re.search(r"\b(19|20)\d{2}\b", query):
        return f"{query} {_dt.date.today().year}".strip()
    return query


def _prior_user_texts(messages: list[dict[str, Any]] | None) -> list[str]:
    out = []
    for m in messages or []:
        if m.get("role") != "user":
            continue
        t = strip_research_block(m.get("content") or "")
        if t:
            out.append(t)
    return out


def _last_assistant_was_factual(messages: list[dict[str, Any]] | None) -> bool:
    """Heuristic: recent assistant turn mentioned search/weather/scores etc."""
    for m in reversed(messages or []):
        if m.get("role") != "assistant":
            continue
        c = (m.get("content") or "").lower()
        if any(
            k in c
            for k in (
                "°f",
                "°c",
                "weather",
                "forecast",
                "score",
                "according to",
                "wttr",
                "http",
            )
        ):
            return True
        return False
    return False


def _continues_factual_topic(msg: str, messages: list[dict[str, Any]] | None) -> bool:
    if not _last_assistant_was_factual(messages):
        return False
    return bool(_CONTINUATION.search(msg or ""))


def route_search(
    user_msg: str,
    *,
    messages: list[dict[str, Any]] | None = None,
    last_search_query: str | None = None,
    force_on: bool = False,
    agentic: bool = False,
) -> SearchDecision:
    """
    Decide whether to search and what query to use.

    force_on: UI toggle forced, but still respects an explicit 'don't search'.
    agentic: tool-loop turn, so never search on a heuristic, only when asked.
    """
    raw = strip_research_block(user_msg or "").strip()
    if not raw:
        return SearchDecision(False, reason="empty")

    if _NO_SEARCH.search(raw):
        return SearchDecision(False, reason="no-search-veto")

    if agentic and not (force_on or _positive_search_cmd(raw)):
        # Agentic turns are about the project on disk, and these heuristics
        # misfire on build requests. The cost is not just noise: results are
        # untrusted text pasted into the prompt of an agent that runs shell
        # commands. Search only when explicitly asked.
        return SearchDecision(False, reason="agentic-needs-explicit-request")

    if _DATE_Q.search(raw):
        # Clock answers do not need the web.
        return SearchDecision(False, reason="local-clock")

    # Follow-up to prior search (e.g. "what about at 5 pm?")
    if last_search_query and _is_search_followup(raw):
        subject = _carry_subject(last_search_query)
        if subject:
            built = f"{subject} {_followup_constraint(raw)}".strip()
            return SearchDecision(
                True,
                query=_add_recency(built),
                reason="followup",
                weather=bool(_CURRENT_INFO.search(built) or re.search(r"\bweather\b", built, re.I)),
            )

    # Meta "can you search" / "look it up" with no topic → prior user question
    if _META_ONLY.match(raw) or (_positive_search_cmd(raw) and len(raw.split()) <= 6 and not _entity_search(raw)):
        prior = _prior_user_texts(messages)
        topic = ""
        if last_search_query:
            topic = last_search_query
        else:
            for prev in reversed(prior):
                if prev and not _META_ONLY.match(prev) and not _NO_SEARCH.search(prev):
                    topic = prev
                    break
        if topic:
            q = clean_query(_kw_query(topic)) or topic
            return SearchDecision(
                True,
                query=_add_recency(q),
                reason="meta-search",
                weather=bool(_CURRENT_INFO.search(q)),
            )

    if _is_personal_turn(raw) and not force_on:
        return SearchDecision(False, reason="personal")

    if _REMINISCE.search(raw) and not _positive_search_cmd(raw) and not force_on:
        return SearchDecision(False, reason="reminisce")

    forced = force_on or _positive_search_cmd(raw) or _entity_question(raw) or bool(_CURRENT_INFO.search(raw))

    if not forced and not _continues_factual_topic(raw, messages) and _skip_search_router(raw):
        return SearchDecision(False, reason="no-signal")

    if not forced and not _kw_wants_search(raw) and not force_on:
        # Continuation inside a factual thread with weak signals
        if _continues_factual_topic(raw, messages) and last_search_query:
            subject = _carry_subject(last_search_query)
            q = f"{subject} {clean_query(raw)}".strip() if subject else clean_query(raw)
            return SearchDecision(True, query=_add_recency(q or last_search_query), reason="factual-continuation")
        return SearchDecision(False, reason="heuristic-no")

    q = _kw_query(raw)
    # Carry subject into short subjectless follow-ups the shape detector missed
    subject = _carry_subject(last_search_query) if last_search_query else ""
    if subject and not _entity_search(q) and len(q.split()) <= 8:
        q = f"{subject} {q}".strip()

    q = clean_query(q) or q
    q = _add_recency(q)
    return SearchDecision(
        True,
        query=q,
        reason="forced" if forced else "heuristic-yes",
        weather=bool(_CURRENT_INFO.search(raw) or _CURRENT_INFO.search(q)),
    )
