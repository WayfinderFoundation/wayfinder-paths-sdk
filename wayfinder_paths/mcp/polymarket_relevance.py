from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from wayfinder_paths.core.clients.PolymarketClient import (
    PolymarketSort,
    PolymarketStatus,
)

_MAX_RECALL_LIMIT = 50
_MAX_EXTRA_SEARCHES = 1
_MAX_EVENT_HYDRATIONS = 3
_MAX_DIRECT_HYDRATIONS = 2
_EXPANSION_TIMEOUT_S = 2.5

_FILLER = {
    "a",
    "about",
    "again",
    "all",
    "an",
    "are",
    "at",
    "bet",
    "by",
    "can",
    "company",
    "coming",
    "could",
    "does",
    "do",
    "end",
    "edge",
    "for",
    "from",
    "game",
    "getting",
    "get",
    "give",
    "going",
    "here",
    "i",
    "in",
    "is",
    "it",
    "likely",
    "make",
    "made",
    "making",
    "market",
    "markets",
    "match",
    "me",
    "most",
    "next",
    "of",
    "odd",
    "odds",
    "on",
    "out",
    "priced",
    "pricing",
    "quick",
    "right",
    "s",
    "see",
    "show",
    "stage",
    "take",
    "team",
    "teams",
    "the",
    "there",
    "think",
    "this",
    "to",
    "trade",
    "user",
    "users",
    "we",
    "what",
    "when",
    "where",
    "which",
    "who",
    "will",
    "worth",
    "would",
}

_CONNECTORS = {"or", "vs", "versus", "against", "and", "between"}

_TEMPORAL_TERMS = {
    "tonight",
    "today",
    "tomorrow",
    "week",
    "month",
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
}

_INTENT_TERMS = {
    *_TEMPORAL_TERMS,
    "advance",
    "agreement",
    "assets",
    "beat",
    "beats",
    "boot",
    "bps",
    "ceasefire",
    "control",
    "cup",
    "cut",
    "decision",
    "demand",
    "deal",
    "draw",
    "election",
    "favored",
    "favorite",
    "fed",
    "first",
    "fomc",
    "goalscorer",
    "golden",
    "group",
    "hike",
    "hold",
    "house",
    "increase",
    "ipo",
    "knockout",
    "meeting",
    "moneyline",
    "nominee",
    "outright",
    "president",
    "prime",
    "rate",
    "rates",
    "release",
    "restored",
    "scorer",
    "signed",
    "qualification",
    "qualify",
    "round",
    "surrender",
    "tie",
    "troop",
    "tournament",
    "uranium",
    "withdraw",
    "withdrawal",
    "win",
    "winner",
    "wins",
    "world",
}

_WIN_TERMS = {"win", "wins", "winning", "won"}
_DRAW_TERMS = {"draw", "tie"}
_MATCH_INTENT_TERMS = {
    "beat",
    "draw",
    "game",
    "match",
    "moneyline",
    "tie",
    "upset",
    "vs",
}
_ENTITY_NOISE = {
    "advance",
    "beat",
    "beats",
    "boot",
    "control",
    "decision",
    "draw",
    "fed",
    "game",
    "goal",
    "goalscorer",
    "golden",
    "group",
    "hike",
    "hold",
    "house",
    "increase",
    "knockout",
    "match",
    "moneyline",
    "out",
    "qualification",
    "qualify",
    "rate",
    "round",
    "score",
    "scorer",
    "stage",
    "tie",
    "top",
    "tournament",
    "win",
    "winner",
    "winning",
    "world",
    "cup",
    "demand",
    "troop",
    "withdraw",
    "withdrawal",
}
_SLUGISH_RE = re.compile(
    r"(?:https?://\S+/)?([a-z0-9]+(?:-[a-z0-9]+){2,})(?:[/?#].*)?$", re.I
)


@dataclass(frozen=True)
class SlugCandidate:
    slug: str
    kind: str = "slug"


@dataclass
class QueryPlan:
    original: str
    search_query: str
    tokens: list[str]
    signal_tokens: list[str]
    expanded_signal_tokens: list[str]
    entities: list[str]
    entity_terms: list[str]
    intent_terms: list[str]
    variants: list[str] = field(default_factory=list)
    slug_candidates: list[SlugCandidate] = field(default_factory=list)


@dataclass
class RelevanceResult:
    ok: bool
    rows: list[dict[str, Any]]
    metadata: dict[str, Any]
    error: Any | None = None


def _normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\b([a-zA-Z0-9]+)[’']s\b", r"\1", text)
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text.lower())
    return " ".join(text.split())


def _canonical_token(token: str) -> str:
    match = re.fullmatch(r"(\d+)(st|nd|rd|th)", token)
    if match:
        return match.group(1)
    if token == "versus":
        return "vs"
    if token == "gop":
        return "republican"
    if token in {"advances", "advancing", "advanced"}:
        return "advance"
    if token in {"withdrawal", "withdraws", "withdrawing"}:
        return "withdraw"
    if token in {"scores", "scoring"}:
        return "score"
    if token == "goals":
        return "goal"
    if token in {"democrats", "democratic"}:
        return "democrat"
    if token == "republicans":
        return "republican"
    if token in {"us", "bps", "fomc", "this", "vs"}:
        return token
    if token.endswith("ies") and len(token) > 4:
        return f"{token[:-3]}y"
    if token.endswith("s") and len(token) > 3 and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokens(value: Any) -> list[str]:
    return [_canonical_token(token) for token in _normalize(value).split()]


def _slugify(tokens: list[str]) -> str:
    return "-".join(t for t in tokens if t)


def _unique(items: list[str], *, limit: int | None = None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = _normalize(item)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
        if limit is not None and len(out) >= limit:
            break
    return out


def build_query_plan(query: str) -> QueryPlan:
    tokens = _tokens(query)
    signal = [t for t in tokens if t not in _FILLER and t not in _CONNECTORS]
    entities = _extract_entity_pair(tokens) or _infer_entity_pair(tokens, signal)
    intent_terms = [t for t in signal if t in _INTENT_TERMS]
    entity_terms = _entity_terms(
        signal=signal, entities=entities, intent_terms=intent_terms
    )
    grammar_variants = _grammar_variants(
        tokens=tokens,
        signal=signal,
        entities=entities,
        entity_terms=entity_terms,
        intent_terms=intent_terms,
    )

    fallback_query = " ".join(signal[:8]) or _normalize(query)
    search_query = grammar_variants[0] if grammar_variants else fallback_query
    variants = [fallback_query, *grammar_variants[1:]]
    variants = [
        variant
        for variant in _unique(variants, limit=3)
        if _normalize(variant) != _normalize(search_query)
    ]

    return QueryPlan(
        original=query,
        search_query=search_query,
        tokens=tokens,
        signal_tokens=signal,
        expanded_signal_tokens=_unique([*signal, *_tokens(search_query)]),
        entities=entities,
        entity_terms=entity_terms,
        intent_terms=intent_terms,
        variants=variants,
        slug_candidates=_slug_candidates(query),
    )


def _extract_entity_pair(tokens: list[str]) -> list[str]:
    for idx, token in enumerate(tokens):
        if token not in _CONNECTORS:
            continue
        left = _nearest_signal(tokens[:idx], reverse=True)
        right = _nearest_signal(tokens[idx + 1 :], reverse=False)
        if left and right and left != right:
            return [left, right]
    return []


def _infer_entity_pair(tokens: list[str], signal: list[str]) -> list[str]:
    signal_set = set(signal)
    token_set = set(tokens)
    has_match_shape = bool(
        (_MATCH_INTENT_TERMS & signal_set)
        or (_MATCH_INTENT_TERMS & token_set)
        or ({"world", "cup"} <= signal_set and len(signal) >= 4)
    )
    if not has_match_shape:
        return []

    candidates = [
        token
        for token in signal
        if token not in _INTENT_TERMS
        and token not in _ENTITY_NOISE
        and not token.isdigit()
        and len(token) > 1
    ]
    return _unique(candidates, limit=2) if len(candidates) >= 2 else []


def _nearest_signal(tokens: list[str], *, reverse: bool) -> str | None:
    iterable = reversed(tokens) if reverse else iter(tokens)
    for token in iterable:
        if token in _FILLER or token in _CONNECTORS:
            continue
        if token in _INTENT_TERMS or token in _ENTITY_NOISE:
            continue
        return token
    return None


def _entity_terms(
    *, signal: list[str], entities: list[str], intent_terms: list[str]
) -> list[str]:
    terms = list(entities)
    intent = set(intent_terms)
    for token in signal:
        if token in intent or token in _ENTITY_NOISE or token.isdigit():
            continue
        if len(token) <= 1:
            continue
        terms.append(token)
    return _unique(terms, limit=5)


def _grammar_variants(
    *,
    tokens: list[str],
    signal: list[str],
    entities: list[str],
    entity_terms: list[str],
    intent_terms: list[str],
) -> list[str]:
    variants: list[str] = []
    signal_set = set(signal)
    token_set = set(tokens)
    has_world_cup = {"world", "cup"} <= signal_set
    has_match_cue = bool(
        (_MATCH_INTENT_TERMS & signal_set)
        or (_MATCH_INTENT_TERMS & token_set)
        or ("upset" in signal_set)
    )

    if len(entities) >= 2:
        a, b = entities[:2]
        rest = [t for t in signal if t not in {a, b}]
        if has_match_cue or (
            has_world_cup
            and "winner" not in signal_set
            and "outright" not in signal_set
        ):
            variants.append(" ".join([a, b]))
            if _DRAW_TERMS & signal_set:
                variants.append(" ".join([a, b, "draw"]))
            if {"beat", "beats", "moneyline", "upset"} & signal_set:
                variants.append(" ".join([a, b]))
        variants.append(" ".join([a, b, *rest[:5]]))
        variants.append(" ".join([b, a, *rest[:5]]))

    for idx, token in enumerate(tokens):
        if token not in _WIN_TERMS:
            continue
        tail = [
            t for t in tokens[idx + 1 :] if t not in _FILLER and t not in _CONNECTORS
        ]
        head = [t for t in tokens[:idx] if t not in _FILLER and t not in _CONNECTORS]
        if tail:
            variants.append(" ".join([*tail[:5], "winner"]))
        if head and tail:
            variants.append(" ".join([*head[:3], "win", *tail[:5]]))

    if "outright" in signal_set and intent_terms:
        event_terms = [t for t in intent_terms if t != "outright"]
        if event_terms:
            variants.append(" ".join([*event_terms[:5], "winner"]))

    if "rate" in signal_set and "decision" in signal_set and "fed" not in signal_set:
        variants.insert(0, "fed decision")

    if (
        "fed" in signal_set
        and "july" in signal_set
        and ({"decision", "rate", "hike", "hold", "increase"} & signal_set)
    ):
        if "hike" in signal_set or "increase" in signal_set:
            amounts = [t for t in signal if t.isdigit() or t == "bps"]
            variants.insert(0, " ".join(["fed", "increase", "july", *amounts[:2]]))
        variants.insert(0, "fed july decision")

    if "july" in signal_set and "hike" in signal_set and "fed" not in signal_set:
        variants.insert(0, "fed increase july 25 bps")

    if "house" in signal_set:
        party_terms = [t for t in signal if t in {"democrat", "republican"}]
        if party_terms:
            variants.insert(0, " ".join([party_terms[0], "house", "control"]))
        if "control" in signal_set or "election" in signal_set or "2026" in signal_set:
            variants.append("house control 2026")

    if "iran" in signal_set and "demand" in signal_set:
        variants.insert(0, "iran demand trump")

    if "iran" in signal_set and ({"troop", "withdraw", "withdrawal"} & signal_set):
        variants.insert(0, "iran troop withdrawal")

    single_subject = [t for t in entity_terms if t not in {"world", "cup"}]
    if single_subject and ("match" in token_set or "game" in token_set):
        if _DRAW_TERMS & signal_set:
            variants.insert(0, " ".join([single_subject[0], "draw"]))
        else:
            variants.insert(0, " ".join([single_subject[0], "match"]))

    has_top_scorer = (
        {"top", "scorer"} <= signal_set
        or "goalscorer" in signal_set
        or ("goal" in signal_set and ("most" in token_set or "top" in signal_set))
        or {"golden", "boot"} <= signal_set
    )
    if has_top_scorer:
        event_terms = [
            t
            for t in signal
            if t
            not in {"top", "goal", "score", "scorer", "goalscorer", "golden", "boot"}
        ]
        player_terms = [t for t in entity_terms if t not in {"world", "cup"}]
        if player_terms:
            variants.insert(0, " ".join([*player_terms[:2], "top", "goalscorer"]))
        if "world" in signal_set or "cup" in signal_set or not player_terms:
            variants.append("world cup goalscorer")

    has_advance_shape = (
        "knockout" in signal_set
        or "advance" in signal_set
        or "qualify" in signal_set
        or "qualification" in signal_set
        or ("group" in signal_set and "out" in token_set)
        or ("round" in signal_set and "next" in token_set)
    )
    if has_advance_shape:
        subject = [t for t in entity_terms if t not in {"group", "stage", "round"}]
        if subject:
            variants.insert(0, " ".join([subject[0], "advance", "knockout"]))
            variants.append(" ".join([subject[0], "knockout", "world", "cup"]))
        else:
            variants.insert(0, "knockout advance")

    if entity_terms and _WIN_TERMS & signal_set:
        event_terms = [
            t for t in intent_terms if t not in _WIN_TERMS and t not in _DRAW_TERMS
        ]
        if event_terms:
            variants.append(" ".join([*entity_terms[:2], "win", *event_terms[:5]]))

    if _DRAW_TERMS & signal_set and len(entities) >= 2:
        variants.append(" ".join([entities[0], entities[1], "draw"]))

    subject_terms = [t for t in entity_terms if t != "us"] or entity_terms
    precise_temporal_terms = _TEMPORAL_TERMS - {"week", "month"}
    date_terms = [t for t in signal if t.isdigit() or t in precise_temporal_terms]
    suppress_date_shortcut = bool(
        "house" in signal_set
        or "fed" in signal_set
        or ("rate" in signal_set and "decision" in signal_set)
        or has_advance_shape
    )
    if subject_terms and date_terms and not suppress_date_shortcut:
        variants.insert(0, " ".join([*subject_terms[:3], *date_terms[:2]]))

    key_actions = [
        t
        for t in intent_terms
        if t not in _TEMPORAL_TERMS and t not in _WIN_TERMS and t not in _DRAW_TERMS
    ]
    if (
        subject_terms
        and key_actions
        and len(signal) > len(subject_terms) + len(key_actions)
    ):
        variants.append(" ".join([*subject_terms[:3], *key_actions[:2]]))

    return _unique(variants, limit=4)


def _slug_candidates(query: str) -> list[SlugCandidate]:
    text = str(query or "").strip()
    candidates: list[SlugCandidate] = []
    for match in _SLUGISH_RE.finditer(text):
        slug = match.group(1).strip("-").lower()
        if slug and not slug.isdigit():
            candidates.append(SlugCandidate(slug=slug, kind="slug"))
    return candidates[:_MAX_DIRECT_HYDRATIONS]


def _market_text(market: dict[str, Any]) -> str:
    pieces: list[str] = []
    for key in ("question", "title", "symbol", "slug", "eventSlug", "event_slug"):
        value = market.get(key)
        if value:
            pieces.append(str(value))
    event = market.get("_event")
    if isinstance(event, dict):
        pieces.extend(str(event.get(k)) for k in ("title", "slug") if event.get(k))
    events = market.get("events")
    if isinstance(events, list):
        for item in events[:2]:
            if isinstance(item, dict):
                pieces.extend(
                    str(item.get(k)) for k in ("title", "slug") if item.get(k)
                )
    return _normalize(" ".join(pieces))


def _token_forms(token: str) -> set[str]:
    forms = {token}
    if token in _WIN_TERMS or token == "winner":
        forms.update({"win", "wins", "winning", "winner", "won"})
    elif token == "withdraw":
        forms.update({"withdraw", "withdraws", "withdrawing", "withdrawal"})
    elif token == "advance":
        forms.update({"advance", "advances", "advancing", "advanced"})
    elif token.endswith("s") and len(token) > 3:
        forms.add(token[:-1])
    else:
        forms.add(f"{token}s")
    return forms


def _token_hit(token: str, text_tokens: set[str], text: str) -> bool:
    forms = _token_forms(token)
    if forms & text_tokens:
        return True
    if len(token) >= 4:
        return any(form in text for form in forms)
    return False


def _coverage(tokens: list[str], text: str) -> float:
    if not tokens:
        return 1.0
    text_tokens = {_canonical_token(token) for token in text.split()}
    hits = sum(1 for token in tokens if _token_hit(token, text_tokens, text))
    return hits / len(tokens)


def _is_match_reformulation(plan: QueryPlan) -> bool:
    query_tokens = _tokens(plan.search_query)
    if len(plan.entities) >= 2 and query_tokens[:2] == plan.entities[:2]:
        return "winner" not in query_tokens and "outright" not in query_tokens
    return "match" in query_tokens and any(
        token in plan.entity_terms for token in query_tokens
    )


def _effective_signal_terms(plan: QueryPlan) -> list[str]:
    terms = list(plan.expanded_signal_tokens)
    if _is_match_reformulation(plan):
        terms = [
            token
            for token in terms
            if token not in {"world", "cup", "tournament", "outright"}
        ]
    return terms


def _effective_intent_terms(plan: QueryPlan) -> list[str]:
    terms = list(plan.intent_terms)
    if _is_match_reformulation(plan):
        terms = [
            token
            for token in terms
            if token not in {"world", "cup", "tournament", "outright"}
        ]
    return terms


def _market_family_adjustment(text: str, plan: QueryPlan) -> float:
    signal = set(_effective_signal_terms(plan))
    tokens = set(plan.tokens)
    penalty = 0.0

    wants_say_market = bool(
        {"announcer", "announcers", "say", "word", "phrase"} & signal
    )
    if not wants_say_market and (
        "announcer" in text
        or "announcers" in text
        or " will the announcers say " in f" {text} "
    ):
        penalty -= 4.0

    wants_weather = bool({"temperature", "weather", "rain", "snow", "heat"} & signal)
    if not wants_weather and ("temperature" in text or "weather" in text):
        penalty -= 3.5

    wants_speaker = "speaker" in signal
    if not wants_speaker and "speaker of the house" in text:
        penalty -= 2.75

    wants_retirement = bool({"retire", "retirement", "running"} & signal)
    if not wants_retirement and ("retire" in text or "not running" in text):
        penalty -= 2.75

    wants_dissent = "dissent" in signal
    if not wants_dissent and "dissent" in text:
        penalty -= 2.5

    wants_prop = bool(
        {"assist", "assists", "shot", "shots", "goal", "score", "player", "prop"}
        & signal
    )
    wants_top_scorer = bool({"goalscorer", "scorer", "boot"} & signal) or (
        "most" in tokens and "goal" in signal
    )
    if not wants_prop and not wants_top_scorer:
        prop_markers = (
            " assists ",
            " shots ",
            " score a goal ",
            "player to score",
            "goals gte",
            "btts",
        )
        if any(marker in f" {text} " for marker in prop_markers):
            penalty -= 2.0

    wants_draw = bool(_DRAW_TERMS & signal)
    if not wants_draw and " draw" in text:
        penalty -= 1.5

    if "house" in signal and "control" in signal and " seats " in f" {text} ":
        penalty -= 1.75

    if "rate" in signal and "hike" in signal and "south african reserve bank" in text:
        penalty -= 2.5

    target_adjustment = _target_side_adjustment(text, plan)
    return penalty + target_adjustment


def _target_side_adjustment(text: str, plan: QueryPlan) -> float:
    signal = set(_effective_signal_terms(plan))
    if not ({"beat", "beats", "moneyline", "upset"} & signal or _WIN_TERMS & signal):
        return 0.0
    if not plan.entities:
        return 0.0
    target = plan.entities[0]
    others = plan.entities[1:]
    text_padded = f" {text} "
    adjustment = 0.0
    target_patterns = (
        f"will {target} win",
        f"{target} win",
        f"{target} beat",
        f"{target} beats",
        f"{target}-win",
    )
    if any(pattern in text_padded for pattern in target_patterns):
        adjustment += 1.25
    for other in others:
        other_patterns = (
            f"will {other} win",
            f"{other} win",
            f"{other} beat",
            f"{other} beats",
            f"{other}-win",
        )
        if any(pattern in text_padded for pattern in other_patterns):
            adjustment -= 1.0
    return adjustment


def score_market(market: dict[str, Any], plan: QueryPlan) -> dict[str, Any]:
    text = _market_text(market)
    query_norm = _normalize(plan.search_query)
    signal = [t for t in _effective_signal_terms(plan) if t not in _CONNECTORS]
    entity_coverage = _coverage(plan.entity_terms, text)
    intent_coverage = _coverage(_effective_intent_terms(plan), text)
    signal_coverage = _coverage(signal, text)
    similarity = SequenceMatcher(None, query_norm, text).ratio() if text else 0.0
    exact_phrase = 1.0 if query_norm and query_norm in text else 0.0
    tradable_bonus = _tradable_bonus(market)
    liquidity_bonus = min(
        float(market.get("liquidity") or market.get("liquidityNum") or 0) / 1_000_000,
        0.25,
    )
    family_adjustment = _market_family_adjustment(text, plan)

    score = (
        2.75 * entity_coverage
        + 2.25 * intent_coverage
        + 1.75 * signal_coverage
        + 0.8 * similarity
        + exact_phrase
        + tradable_bonus
        + liquidity_bonus
        + family_adjustment
    )
    if plan.entity_terms and entity_coverage < 0.7:
        score -= 1.25
    if plan.intent_terms and intent_coverage < 0.5:
        score -= 0.5
    return {
        "score": round(score, 6),
        "entityCoverage": round(entity_coverage, 4),
        "intentCoverage": round(intent_coverage, 4),
        "signalCoverage": round(signal_coverage, 4),
        "similarity": round(similarity, 4),
        "familyAdjustment": round(family_adjustment, 4),
    }


def _tradable_bonus(market: dict[str, Any]) -> float:
    active = market.get("active")
    closed = market.get("closed")
    accepting = market.get("acceptingOrders")
    enable_order_book = market.get("enableOrderBook")
    bonus = 0.0
    if active is not False and closed is not True:
        bonus += 0.25
    if accepting is not False and enable_order_book is not False:
        bonus += 0.25
    return bonus


def rerank_markets(
    markets: list[dict[str, Any]], plan: QueryPlan
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for market in markets:
        row = dict(market)
        relevance = score_market(row, plan)
        row["_relevance"] = relevance
        ranked.append(row)
    ranked.sort(
        key=lambda row: (
            float(row.get("_relevance", {}).get("score") or 0),
            float(
                row.get("volume24h")
                or row.get("volume24hr")
                or row.get("volume24hrClob")
                or 0
            ),
            float(
                row.get("liquidity")
                or row.get("liquidityNum")
                or row.get("liquidityClob")
                or 0
            ),
        ),
        reverse=True,
    )
    return ranked


def needs_expansion(
    plan: QueryPlan, ranked: list[dict[str, Any]], raw_rows: list[dict[str, Any]]
) -> tuple[bool, str]:
    if not raw_rows:
        return True, "empty_results"
    if not ranked:
        return True, "empty_ranked"

    top = ranked[0].get("_relevance", {})
    top_score = float(top.get("score") or 0)
    if plan.entity_terms and float(top.get("entityCoverage") or 0) < 0.85:
        return True, "missing_named_entities"
    if plan.intent_terms and float(top.get("intentCoverage") or 0) < 0.55:
        return True, "missing_intent_terms"
    threshold = 4.1 if plan.entity_terms else 3.4
    if top_score < threshold:
        return True, "low_score"
    if len(ranked) > 1:
        second_score = float(ranked[1].get("_relevance", {}).get("score") or 0)
        if top_score - second_score < 0.08:
            return True, "small_score_gap"
    return False, "confident"


def dedupe_markets(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for market in markets:
        key = str(
            market.get("conditionId")
            or market.get("condition_id")
            or market.get("slug")
            or market.get("id")
            or id(market)
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(market)
    return out


async def relevance_search(
    adapter: Any,
    *,
    query: str,
    limit: int,
    sort: PolymarketSort,
    status: PolymarketStatus,
    candidate_limit: int,
) -> RelevanceResult:
    started = time.perf_counter()
    plan = build_query_plan(query)
    recall_limit = min(max(int(limit), int(candidate_limit) * 4, 30), _MAX_RECALL_LIMIT)
    metadata: dict[str, Any] = {
        "mode": "fast",
        "userQuery": query,
        "queriesTried": [plan.search_query],
        "directHydrations": [],
        "eventHydrations": [],
        "confidence": "low",
        "expansionReason": None,
        "queryPlan": {
            "searchQuery": plan.search_query,
            "signalTokens": plan.signal_tokens,
            "entities": plan.entities,
            "entityTerms": plan.entity_terms,
            "intentTerms": plan.intent_terms,
            "variants": plan.variants,
        },
    }

    ok_first, first = await adapter.search_markets(
        query=plan.search_query,
        limit=recall_limit,
        sort=sort,
        status=status,
    )
    rows: list[dict[str, Any]] = first if ok_first and isinstance(first, list) else []
    ranked = rerank_markets(dedupe_markets(rows), plan)
    expand, reason = needs_expansion(plan, ranked, rows)

    if expand:
        metadata["mode"] = "expanded"
        metadata["expansionReason"] = reason
        extra_rows, tried_queries, direct_hydrations, event_hydrations = await _expand(
            adapter=adapter,
            plan=plan,
            seed_rows=ranked or rows,
            recall_limit=recall_limit,
            sort=sort,
            status=status,
        )
        metadata["queriesTried"].extend(tried_queries)
        metadata["directHydrations"].extend(direct_hydrations)
        metadata["eventHydrations"].extend(event_hydrations)
        rows = dedupe_markets([*rows, *extra_rows])
        ranked = rerank_markets(rows, plan)

    if ranked:
        top = ranked[0].get("_relevance", {})
        top_score = float(top.get("score") or 0)
        metadata["confidence"] = (
            "high" if top_score >= (4.1 if plan.entity_terms else 3.4) else "medium"
        )
    metadata["elapsedMs"] = round((time.perf_counter() - started) * 1000, 2)
    metadata["returnedRowsBeforeTruncation"] = len(ranked)

    if not ok_first and not ranked:
        return RelevanceResult(False, [], metadata, first)
    return RelevanceResult(True, ranked, metadata)


async def _expand(
    *,
    adapter: Any,
    plan: QueryPlan,
    seed_rows: list[dict[str, Any]],
    recall_limit: int,
    sort: PolymarketSort,
    status: PolymarketStatus,
) -> tuple[list[dict[str, Any]], list[str], list[str], list[str]]:
    variants = plan.variants[:_MAX_EXTRA_SEARCHES]
    slug_candidates = plan.slug_candidates[:_MAX_DIRECT_HYDRATIONS]
    event_slugs = _rank_event_slugs(seed_rows, plan, limit=_MAX_EVENT_HYDRATIONS)
    tried_queries: list[str] = []
    direct_hydrations: list[str] = []
    event_hydrations: list[str] = []

    async def search_variant(q: str) -> list[dict[str, Any]]:
        tried_queries.append(q)
        ok_rows, rows = await adapter.search_markets(
            query=q,
            limit=recall_limit,
            sort=sort,
            status=status,
        )
        return rows if ok_rows and isinstance(rows, list) else []

    async def hydrate_event(slug: str) -> list[dict[str, Any]]:
        ok_event, event = await adapter.get_event_by_slug(slug)
        if not ok_event or not isinstance(event, dict):
            return []
        event_hydrations.append(slug)
        return _event_markets(event, slug)

    async def hydrate_direct(candidate: SlugCandidate) -> list[dict[str, Any]]:
        ok_event, event = await adapter.get_event_by_slug(candidate.slug)
        if ok_event and isinstance(event, dict):
            direct_hydrations.append(f"event:{candidate.slug}")
            return _event_markets(event, candidate.slug)

        ok_market, market = await adapter.get_market_by_slug(candidate.slug)
        if ok_market and isinstance(market, dict):
            direct_hydrations.append(f"market:{candidate.slug}")
            return [market]
        return []

    tasks = (
        [search_variant(q) for q in variants]
        + [hydrate_event(slug) for slug in event_slugs]
        + [hydrate_direct(c) for c in slug_candidates]
    )
    if not tasks:
        return [], [], [], []
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=_EXPANSION_TIMEOUT_S,
        )
    except TimeoutError:
        return [], tried_queries, direct_hydrations, event_hydrations

    rows: list[dict[str, Any]] = []
    for result in results:
        if isinstance(result, Exception):
            continue
        rows.extend(result)
    return rows, tried_queries, direct_hydrations, event_hydrations


def _event_markets(event: dict[str, Any], slug: str) -> list[dict[str, Any]]:
    markets = event.get("markets")
    if not isinstance(markets, list):
        return []
    out: list[dict[str, Any]] = []
    event_meta = {"slug": event.get("slug") or slug, "title": event.get("title")}
    for market in markets:
        if not isinstance(market, dict):
            continue
        row = dict(market)
        row.setdefault("eventSlug", event.get("slug") or slug)
        row.setdefault("_event", event_meta)
        out.append(row)
    return out


def _rank_event_slugs(
    rows: list[dict[str, Any]], plan: QueryPlan, *, limit: int
) -> list[str]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        slug = str(row.get("eventSlug") or row.get("event_slug") or "").strip()
        if not slug:
            continue
        grouped.setdefault(slug, []).append(row)

    scored: list[tuple[float, str]] = []
    for slug, group_rows in grouped.items():
        event_title = _event_title(group_rows)
        synthetic = {
            "slug": slug,
            "eventSlug": slug,
            "question": " ".join([slug, event_title]),
            "_event": {"slug": slug, "title": event_title},
        }
        event_score = float(score_market(synthetic, plan)["score"])
        row_score = max(
            float(
                row.get("_relevance", {}).get("score")
                or score_market(row, plan)["score"]
            )
            for row in group_rows
        )
        score = max(event_score, row_score) + min(len(group_rows), 6) * 0.04
        threshold = 2.6 if not plan.entity_terms else 2.9
        if score >= threshold:
            scored.append((score, slug))

    scored.sort(reverse=True)
    return [slug for _, slug in scored[:limit]]


def _event_title(rows: list[dict[str, Any]]) -> str:
    for row in rows:
        event = row.get("_event")
        if isinstance(event, dict) and event.get("title"):
            return str(event["title"])
        events = row.get("events")
        if isinstance(events, list):
            for item in events:
                if isinstance(item, dict) and item.get("title"):
                    return str(item["title"])
    return ""
