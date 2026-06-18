"""Generic path-dependent event-market simulator.

This module is intentionally sport-agnostic. It handles the reusable mechanics behind
field markets such as tournament winners, playoff outrights, season awards with staged
cuts, or any event where a participant's fair probability depends on remaining path
state instead of one sportsbook number.

Input is a compact JSON event pack:

```json
{
  "participants": [
    {"id": "a", "name": "Team A", "rating": 2000,
     "evidence": [{"claim": "starter returns", "direction": "for_yes",
                   "strength": "medium", "sourceQuality": "primary",
                   "freshness": "fresh", "independence": "independent",
                   "alreadyPriced": "maybe", "resolutionRelevance": "direct"}]}
  ],
  "groups": [
    {"id": "G1", "participants": ["a", "b", "c", "d"],
     "qualifiers": [{"rank": 1, "slot": "G1_1"}, {"rank": 2, "slot": "G1_2"}],
     "matches": [{"a": "a", "b": "b", "status": "completed", "score": [1, 0]}]}
  ],
  "wildcards": [{"source_rank": 3, "count": 2, "slot_prefix": "WC"}],
  "bracket": {
    "matches": [
      {"id": "s1", "a": {"slot": "G1_1"}, "b": {"slot": "WC1"}},
      {"id": "s2", "a": {"participant": "x"}, "b": {"participant": "y"}},
      {"id": "final", "a": {"winner": "s1"}, "b": {"winner": "s2"}}
    ],
    "champion_match": "final"
  },
  "target": {"type": "champion"},
  "markets": [{"participant_id": "a", "venue": "polymarket", "bid": 0.08, "ask": 0.09}]
}
```

The agent remains responsible for building the event pack from the current sports data,
market boards, and research evidence. The simulator owns the repeated math: conditioning
on completed state, applying evidence as rating adjustments, running Monte Carlo paths,
and classifying edge against executable prices.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wayfinder_paths.quant.polymarket_edge import evidence_llr

_ELO_LOGIT_SCALE = 400.0 / math.log(10.0)
_VALID_EVIDENCE_DIRECTIONS = frozenset({"for_yes", "against_yes"})
_EVIDENCE_DIRECTION_ALIASES = {
    "for": "for_yes",
    "yes": "for_yes",
    "positive": "for_yes",
    "bullish": "for_yes",
    "sign_for": "for_yes",
    "for_yes": "for_yes",
    "against": "against_yes",
    "no": "against_yes",
    "negative": "against_yes",
    "bearish": "against_yes",
    "sign_against": "against_yes",
    "against_yes": "against_yes",
}


def _normalize_evidence_direction(value: Any) -> str:
    return _EVIDENCE_DIRECTION_ALIASES.get(str(value or "").strip().lower(), str(value or ""))


def _normalize_evidence_card(card: Mapping[str, Any]) -> Mapping[str, Any]:
    direction = _normalize_evidence_direction(card.get("direction"))
    if direction == str(card.get("direction") or ""):
        return card
    normalized = dict(card)
    normalized["direction"] = direction
    return normalized


@dataclass(frozen=True)
class Participant:
    id: str
    name: str
    rating: float
    state: str = "clean_unplayed"
    rating_adjustment: float = 0.0
    evidence: tuple[Mapping[str, Any], ...] = ()

    @property
    def effective_rating(self) -> float:
        evidence_delta = sum(
            evidence_llr(card)
            for card in self.evidence
            if str(card.get("direction") or "") in _VALID_EVIDENCE_DIRECTIONS
        )
        return self.rating + self.rating_adjustment + evidence_delta * _ELO_LOGIT_SCALE

    @property
    def ignored_evidence(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            card
            for card in self.evidence
            if str(card.get("direction") or "") not in _VALID_EVIDENCE_DIRECTIONS
        )


@dataclass(frozen=True)
class Market:
    participant_id: str
    venue: str = ""
    bid: float | None = None
    ask: float | None = None
    price: float | None = None
    liquidity: float | None = None

    @property
    def reference_price(self) -> float | None:
        if self.bid is not None and self.ask is not None:
            return (float(self.bid) + float(self.ask)) / 2.0
        if self.ask is not None:
            return float(self.ask)
        if self.price is not None:
            return float(self.price)
        if self.bid is not None:
            return float(self.bid)
        return None

    @property
    def entry_price(self) -> float | None:
        """Executable-ish BUY entry price.

        A midpoint is useful context, but an actionable long edge should clear the ask.
        Bid-only rows are not actionable buy candidates.
        """
        if self.ask is not None:
            return float(self.ask)
        if self.price is not None:
            return float(self.price)
        return None

    @property
    def price_source(self) -> str:
        if self.bid is not None and self.ask is not None:
            return "bid_ask_mid"
        if self.ask is not None:
            return "ask_only"
        if self.price is not None:
            return "price"
        if self.bid is not None:
            return "bid_only"
        return "missing"

    @property
    def entry_source(self) -> str:
        if self.ask is not None:
            return "ask"
        if self.price is not None:
            return "price"
        if self.bid is not None:
            return "bid_only"
        return "missing"


@dataclass
class Standing:
    participant_id: str
    points: int = 0
    gf: int = 0
    ga: int = 0

    @property
    def gd(self) -> int:
        return self.gf - self.ga


@dataclass(frozen=True)
class SimulationConfig:
    participants: dict[str, Participant]
    groups: list[dict[str, Any]] = field(default_factory=list)
    wildcards: list[dict[str, Any]] = field(default_factory=list)
    bracket: dict[str, Any] = field(default_factory=dict)
    target: Mapping[str, Any] = field(default_factory=lambda: {"type": "champion"})
    markets: dict[str, tuple[Market, ...]] = field(default_factory=dict)
    model_provenance: Mapping[str, Any] = field(default_factory=dict)
    iterations: int = 20000
    seed: int = 42
    min_edge_abs: float = 0.005
    min_edge_rel: float = 0.20


@dataclass(frozen=True)
class CandidateResult:
    participant_id: str
    name: str
    probability: float
    wins: int
    market_price: float | None
    entry_price: float | None
    price_source: str
    entry_source: str
    venue: str
    edge_abs: float | None
    edge_rel: float | None
    classification: str
    decision: str
    diagnostic_flags: tuple[str, ...] = ()
    ignored_evidence: tuple[Mapping[str, Any], ...] = ()


def load_config(data: Mapping[str, Any]) -> SimulationConfig:
    participants = {
        str(row["id"]): Participant(
            id=str(row["id"]),
            name=str(row.get("name") or row["id"]),
            rating=float(row.get("rating", 1500.0)),
            state=str(row.get("state") or "clean_unplayed"),
            rating_adjustment=float(row.get("rating_adjustment", 0.0)),
            evidence=tuple(
                _normalize_evidence_card(card) for card in row.get("evidence") or ()
            ),
        )
        for row in data.get("participants", [])
    }
    markets_by_participant: dict[str, list[Market]] = defaultdict(list)
    for row in _market_rows(data):
        participant_id = str(
            row.get("participant_id")
            or row.get("participantId")
            or row.get("subject_id")
            or row.get("id")
        )
        markets_by_participant[participant_id].append(
            Market(
                participant_id=participant_id,
                venue=str(row.get("venue") or ""),
                bid=_optional_float(row.get("bid")),
                ask=_optional_float(row.get("ask")),
                price=_optional_float(
                    row.get("mid") if row.get("price") is None else row.get("price")
                ),
                liquidity=_optional_float(row.get("liquidity") or row.get("depth")),
            )
        )
    markets = {
        participant_id: tuple(markets)
        for participant_id, markets in markets_by_participant.items()
    }
    model_provenance = dict(data.get("modelProvenance") or data.get("model_provenance") or {})
    if "rating_source" in data and "ratingSource" not in model_provenance:
        model_provenance["ratingSource"] = data["rating_source"]
    if "bracket_source" in data and "bracketSource" not in model_provenance:
        model_provenance["bracketSource"] = data["bracket_source"]
    if "bracket_confidence" in data and "bracketConfidence" not in model_provenance:
        model_provenance["bracketConfidence"] = data["bracket_confidence"]
    return SimulationConfig(
        participants=participants,
        groups=list(data.get("groups") or []),
        wildcards=list(data.get("wildcards") or []),
        bracket=dict(data.get("bracket") or {}),
        target=dict(data.get("target") or {"type": "champion"}),
        markets=markets,
        model_provenance=model_provenance,
        iterations=int(data.get("iterations", 20000)),
        seed=int(data.get("seed", 42)),
        min_edge_abs=float(data.get("min_edge_abs", 0.005)),
        min_edge_rel=float(data.get("min_edge_rel", 0.20)),
    )


def _market_rows(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for key in ("markets", "predictionMarketBoard", "annotatedBoard"):
        value = data.get(key)
        if isinstance(value, list):
            rows.extend(row for row in value if isinstance(row, Mapping))
    return rows


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def elo_win_probability(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((float(rating_b) - float(rating_a)) / 400.0))


def _poisson(rng: random.Random, lmbda: float) -> int:
    threshold = math.exp(-max(float(lmbda), 0.0))
    k = 0
    p = 1.0
    while p > threshold:
        k += 1
        p *= rng.random()
    return k - 1


def _simulated_score(
    a: Participant,
    b: Participant,
    *,
    rng: random.Random,
    baseline_goals: float,
) -> tuple[int, int]:
    p_a = elo_win_probability(a.effective_rating, b.effective_rating)
    lambda_a = baseline_goals * 2.0 * p_a
    lambda_b = baseline_goals * 2.0 * (1.0 - p_a)
    return _poisson(rng, lambda_a), _poisson(rng, lambda_b)


def _rank_key(standing: Standing, rng: random.Random) -> tuple[int, int, int, float]:
    return (standing.points, standing.gd, standing.gf, rng.random())


def _apply_result(standing_a: Standing, standing_b: Standing, goals_a: int, goals_b: int) -> None:
    standing_a.gf += goals_a
    standing_a.ga += goals_b
    standing_b.gf += goals_b
    standing_b.ga += goals_a
    if goals_a > goals_b:
        standing_a.points += 3
    elif goals_b > goals_a:
        standing_b.points += 3
    else:
        standing_a.points += 1
        standing_b.points += 1


def simulate_groups(
    config: SimulationConfig,
    rng: random.Random,
) -> tuple[dict[str, str], dict[str, list[Standing]]]:
    slots: dict[str, str] = {}
    standings_by_group: dict[str, list[Standing]] = {}

    for group in config.groups:
        group_id = str(group["id"])
        participant_ids = [str(pid) for pid in group.get("participants", [])]
        standings = {pid: Standing(pid) for pid in participant_ids}
        baseline_goals = float(group.get("baseline_goals", 1.25))

        matches = _group_matches(group, participant_ids)

        for match in matches:
            a_id = str(match["a"])
            b_id = str(match["b"])
            if a_id not in standings or b_id not in standings:
                raise ValueError(f"group {group_id} references unknown participant")
            goals_a, goals_b = _match_score(config, match, a_id, b_id, rng, baseline_goals)
            _apply_result(standings[a_id], standings[b_id], goals_a, goals_b)

        ordered = sorted(standings.values(), key=lambda s: _rank_key(s, rng), reverse=True)
        standings_by_group[group_id] = ordered

        for qualifier in group.get("qualifiers") or []:
            rank = int(qualifier["rank"])
            if rank <= 0 or rank > len(ordered):
                continue
            slot = str(qualifier.get("slot") or f"{group_id}_{rank}")
            slots[slot] = ordered[rank - 1].participant_id

    _assign_wildcards(config, standings_by_group, slots, rng)
    return slots, standings_by_group


def _match_score(
    config: SimulationConfig,
    match: Mapping[str, Any],
    a_id: str,
    b_id: str,
    rng: random.Random,
    baseline_goals: float,
) -> tuple[int, int]:
    status = str(match.get("status") or "scheduled")
    if status == "completed":
        if match.get("score") is not None:
            score = list(match["score"])
            return int(score[0]), int(score[1])
        winner = match.get("winner")
        if winner == a_id:
            return 1, 0
        if winner == b_id:
            return 0, 1
        return 0, 0
    return _simulated_score(
        config.participants[a_id],
        config.participants[b_id],
        rng=rng,
        baseline_goals=baseline_goals,
    )


def _group_matches(group: Mapping[str, Any], participant_ids: list[str]) -> list[dict[str, Any]]:
    matches = [dict(match) for match in group.get("matches") or []]
    if not group.get("complete_round_robin", True):
        return matches

    seen = {frozenset((str(match["a"]), str(match["b"]))) for match in matches}
    for i in range(len(participant_ids)):
        for j in range(i + 1, len(participant_ids)):
            key = frozenset((participant_ids[i], participant_ids[j]))
            if key not in seen:
                matches.append({"a": participant_ids[i], "b": participant_ids[j]})
    return matches


def _assign_wildcards(
    config: SimulationConfig,
    standings_by_group: Mapping[str, list[Standing]],
    slots: dict[str, str],
    rng: random.Random,
) -> None:
    for wildcard in config.wildcards:
        rank = int(wildcard["source_rank"])
        count = int(wildcard["count"])
        prefix = str(wildcard.get("slot_prefix") or f"WC{rank}")
        candidates = [
            standing
            for standings in standings_by_group.values()
            if len(standings) >= rank
            for standing in [standings[rank - 1]]
        ]
        candidates.sort(key=lambda s: _rank_key(s, rng), reverse=True)
        for idx, standing in enumerate(candidates[:count], 1):
            slots[f"{prefix}{idx}"] = standing.participant_id


def _resolve_endpoint(
    endpoint: Any,
    slots: Mapping[str, str],
    winners: Mapping[str, str],
) -> str:
    if isinstance(endpoint, str):
        return slots.get(endpoint, endpoint)
    if not isinstance(endpoint, Mapping):
        raise ValueError(f"invalid bracket endpoint {endpoint!r}")
    if endpoint.get("participant") is not None:
        return str(endpoint["participant"])
    if endpoint.get("slot") is not None:
        slot = str(endpoint["slot"])
        if slot not in slots:
            raise ValueError(f"slot {slot!r} is not assigned")
        return slots[slot]
    if endpoint.get("winner") is not None:
        match_id = str(endpoint["winner"])
        if match_id not in winners:
            raise ValueError(f"winner of {match_id!r} is not known yet")
        return winners[match_id]
    raise ValueError(f"invalid bracket endpoint {endpoint!r}")


def simulate_bracket(config: SimulationConfig, slots: Mapping[str, str], rng: random.Random) -> str:
    champion, _match_participants, _match_winners = _simulate_bracket_trace(config, slots, rng)
    if champion is None:
        raise ValueError("event config needs bracket.matches or exactly one participant")
    return champion


def _simulate_bracket_trace(
    config: SimulationConfig,
    slots: Mapping[str, str],
    rng: random.Random,
) -> tuple[str | None, dict[str, tuple[str, str]], dict[str, str]]:
    bracket = config.bracket
    matches = list(bracket.get("matches") or [])
    if not matches:
        if len(config.participants) == 1:
            only_participant = next(iter(config.participants))
            return only_participant, {}, {}
        return None, {}, {}

    winners: dict[str, str] = {}
    match_participants: dict[str, tuple[str, str]] = {}
    for match in matches:
        match_id = str(match["id"])
        a_id = _resolve_endpoint(match["a"], slots, winners)
        b_id = _resolve_endpoint(match["b"], slots, winners)
        if a_id not in config.participants or b_id not in config.participants:
            raise ValueError(f"match {match_id} references unknown participant")
        match_participants[match_id] = (a_id, b_id)
        if str(match.get("status") or "scheduled") == "completed":
            winner = str(match["winner"])
            if winner not in (a_id, b_id):
                raise ValueError(f"completed match {match_id} winner is not a participant")
            winners[match_id] = winner
            continue
        p_a = elo_win_probability(
            config.participants[a_id].effective_rating,
            config.participants[b_id].effective_rating,
        )
        winners[match_id] = a_id if rng.random() < p_a else b_id

    champion_match = str(bracket.get("champion_match") or matches[-1]["id"])
    if champion_match not in winners:
        raise ValueError(f"champion_match {champion_match!r} was not simulated")
    return winners[champion_match], match_participants, winners


def run_simulation(config: SimulationConfig) -> list[CandidateResult]:
    rng = random.Random(config.seed)
    wins: dict[str, int] = defaultdict(int)
    seen_completed = _participants_with_completed_state(config)
    config_flags = _config_diagnostic_flags(config)

    for _ in range(config.iterations):
        slots, _standings = simulate_groups(config, rng)
        champion, match_participants, match_winners = _simulate_bracket_trace(config, slots, rng)
        for participant_id in _target_successes(
            config,
            slots=slots,
            champion=champion,
            match_participants=match_participants,
            match_winners=match_winners,
        ):
            wins[participant_id] += 1

    probabilities = {
        participant_id: wins.get(participant_id, 0) / max(config.iterations, 1)
        for participant_id in config.participants
    }
    distribution_flags = _distribution_diagnostic_flags(probabilities)
    common_flags = tuple(dict.fromkeys((*config_flags, *distribution_flags)))

    rows: list[CandidateResult] = []
    for participant_id, participant in config.participants.items():
        probability = probabilities[participant_id]
        classification = _classification(participant, probability, seen_completed)
        evidence_flags = (
            ("invalid_evidence_direction",) if participant.ignored_evidence else ()
        )
        participant_flags = tuple(
            dict.fromkeys(
                (
                    *common_flags,
                    *evidence_flags,
                )
            )
        )
        markets = config.markets.get(participant_id) or (None,)
        for market in markets:
            price = market.reference_price if market else None
            entry = market.entry_price if market else None
            edge_abs = probability - entry if entry is not None else None
            edge_rel = edge_abs / entry if edge_abs is not None and entry and entry > 0 else None
            rows.append(
                CandidateResult(
                    participant_id=participant_id,
                    name=participant.name,
                    probability=probability,
                    wins=wins.get(participant_id, 0),
                    market_price=price,
                    entry_price=entry,
                    price_source=market.price_source if market else "missing",
                    entry_source=market.entry_source if market else "missing",
                    venue=market.venue if market else "",
                    edge_abs=edge_abs,
                    edge_rel=edge_rel,
                    classification=classification,
                    decision=_decision(
                        config,
                        edge_abs,
                        edge_rel,
                        classification,
                        entry,
                        market.entry_source if market else "missing",
                        participant_flags,
                    ),
                    diagnostic_flags=participant_flags,
                    ignored_evidence=participant.ignored_evidence,
                )
            )
    rows.sort(key=lambda row: row.probability, reverse=True)
    return rows


def _config_diagnostic_flags(config: SimulationConfig) -> tuple[str, ...]:
    provenance = dict(config.model_provenance or {})
    flags: list[str] = []
    rating_source = " ".join(
        str(provenance.get(key) or "")
        for key in ("ratingSource", "rating_source", "ratings", "ratingsSource")
    ).lower()
    if any(
        needle in rating_source
        for needle in (
            "outright",
            "winner probability",
            "champion probability",
            "futures",
            "sportsbook fair",
            "market-implied",
            "market implied",
        )
    ):
        flags.append("market_implied_ratings_diagnostic_only")

    bracket_source = " ".join(
        str(provenance.get(key) or "")
        for key in ("bracketSource", "bracket_source", "bracketConfidence", "bracketQuality")
    ).lower()
    bracket_meta = json.dumps(config.bracket, sort_keys=True).lower() if config.bracket else ""
    if any(
        needle in f"{bracket_source} {bracket_meta}"
        for needle in ("approx", "simplified", "assumption", "estimated")
    ):
        flags.append("approx_bracket")

    return tuple(dict.fromkeys(flags))


def _distribution_diagnostic_flags(probabilities: Mapping[str, float]) -> tuple[str, ...]:
    if not probabilities:
        return ()
    ordered = sorted(probabilities.values(), reverse=True)
    flags: list[str] = []
    if sum(ordered[:3]) >= 0.60 and len(ordered) >= 8:
        flags.append("concentrated_distribution")
    zero_like = sum(1 for value in ordered if value <= 0.0)
    if zero_like / len(ordered) >= 0.25:
        flags.append("zero_probability_mass_warning")
    return tuple(flags)


def _target_successes(
    config: SimulationConfig,
    *,
    slots: Mapping[str, str],
    champion: str | None,
    match_participants: Mapping[str, tuple[str, str]],
    match_winners: Mapping[str, str],
) -> set[str]:
    target = dict(config.target or {"type": "champion"})
    target_type = str(target.get("type") or "champion")

    if target_type == "champion":
        if champion is None:
            raise ValueError("champion target requires bracket.matches or exactly one participant")
        return {champion}

    if target_type == "slot":
        slot_names = target.get("slots")
        if slot_names is None:
            slot_names = [target["slot"]]
        return {slots[str(slot)] for slot in slot_names if str(slot) in slots}

    if target_type == "reach_match":
        match_id = str(target["match"])
        if match_id not in match_participants:
            raise ValueError(f"target match {match_id!r} was not simulated")
        return set(match_participants[match_id])

    if target_type == "match_winner":
        match_id = str(target["match"])
        if match_id not in match_winners:
            raise ValueError(f"target match {match_id!r} was not simulated")
        return {match_winners[match_id]}

    raise ValueError(f"unsupported target type {target_type!r}")


def _participants_with_completed_state(config: SimulationConfig) -> set[str]:
    completed: set[str] = set()
    for group in config.groups:
        for match in group.get("matches") or []:
            if str(match.get("status") or "") == "completed":
                completed.add(str(match["a"]))
                completed.add(str(match["b"]))
    for match in (config.bracket or {}).get("matches") or []:
        if str(match.get("status") or "") == "completed":
            for side in ("a", "b"):
                endpoint = match.get(side)
                if isinstance(endpoint, Mapping) and endpoint.get("participant") is not None:
                    completed.add(str(endpoint["participant"]))
    return completed


def _classification(
    participant: Participant,
    probability: float,
    seen_completed: set[str],
) -> str:
    if participant.state != "clean_unplayed":
        return participant.state
    if probability <= 0.0 and participant.id in seen_completed:
        return "dead_signal"
    if participant.id in seen_completed:
        return "live_conditioned"
    return "clean_unplayed"


def _decision(
    config: SimulationConfig,
    edge_abs: float | None,
    edge_rel: float | None,
    classification: str,
    price: float | None,
    entry_source: str,
    diagnostic_flags: tuple[str, ...],
) -> str:
    if entry_source == "bid_only":
        return "WATCH"
    if price is None:
        return "NO_MARKET"
    if classification == "dead_signal":
        return "SKIP"
    if "market_implied_ratings_diagnostic_only" in diagnostic_flags:
        return "WATCH"
    if "invalid_evidence_direction" in diagnostic_flags:
        return "WATCH"
    if edge_abs is None or edge_rel is None:
        return "WATCH"
    if edge_abs >= config.min_edge_abs and edge_rel >= config.min_edge_rel:
        if (
            "approx_bracket" in diagnostic_flags
            and not bool(config.model_provenance.get("allowActionableApproxBracket"))
        ):
            return "WATCH"
        return "BUY_CANDIDATE"
    if edge_abs > 0:
        return "WATCH"
    return "SKIP"


def rows_as_dicts(rows: list[CandidateResult]) -> list[dict[str, Any]]:
    return [
        {
            "participant_id": row.participant_id,
            "name": row.name,
            "probability": round(row.probability, 6),
            "wins": row.wins,
            "market_price": None if row.market_price is None else round(row.market_price, 6),
            "entry_price": None if row.entry_price is None else round(row.entry_price, 6),
            "price_source": row.price_source,
            "entry_source": row.entry_source,
            "venue": row.venue,
            "edge_abs": None if row.edge_abs is None else round(row.edge_abs, 6),
            "edge_rel": None if row.edge_rel is None else round(row.edge_rel, 6),
            "classification": row.classification,
            "decision": row.decision,
            "diagnostic_flags": list(row.diagnostic_flags),
            "ignoredEvidence": list(row.ignored_evidence),
        }
        for row in rows
    ]


def render(rows: list[CandidateResult], *, top: int = 20) -> str:
    lines = [
        "EVENT MARKET SIM — path-conditioned fair probabilities",
        "",
        (
            f"{'#':>3} {'participant':<24} {'venue':<12} {'sim_p':>8} {'entry':>8} "
            f"{'edge':>8} {'rel':>8} {'state':<17} decision"
        ),
    ]
    for idx, row in enumerate(rows[:top], 1):
        entry = "-" if row.entry_price is None else f"{row.entry_price:.4f}"
        edge = "-" if row.edge_abs is None else f"{row.edge_abs:+.4f}"
        rel = "-" if row.edge_rel is None else f"{row.edge_rel * 100:+.1f}%"
        lines.append(
            f"{idx:>3} {row.name:<24.24} {row.venue:<12.12} {row.probability:>8.4f} {entry:>8} "
            f"{edge:>8} {rel:>8} {row.classification:<17.17} {row.decision}"
        )
    if len(rows) > top:
        lines.append(f"  ... {len(rows) - top} more (see artifacts)")
    lines.append("")
    lines.append(
        "NOTE: sportsbook-derived fields are not executable. Use this output as one "
        "path/current-state model, then distill it against executable order-book depth, "
        "other model views, and qualitative evidence before calling value."
    )
    flags = sorted({flag for row in rows for flag in row.diagnostic_flags})
    if flags:
        lines.append(f"DIAGNOSTIC FLAGS: {', '.join(flags)}")
    return "\n".join(lines)


def write_artifacts(
    rows: list[CandidateResult],
    config: SimulationConfig,
    out_dir: str | Path,
    *,
    stem: str = "event_sim",
) -> list[str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    dict_rows = rows_as_dicts(rows)
    json_path = out / f"{stem}.json"
    json_path.write_text(
        json.dumps(
            {
                "iterations": config.iterations,
                "seed": config.seed,
                "min_edge_abs": config.min_edge_abs,
                "min_edge_rel": config.min_edge_rel,
                "modelProvenance": dict(config.model_provenance),
                "diagnosticFlags": sorted(
                    {flag for row in rows for flag in row.diagnostic_flags}
                ),
                "participants": _participant_summary_rows(rows),
                "rows": dict_rows,
            },
            indent=2,
        )
    )
    artifacts = [str(json_path)]
    if dict_rows:
        csv_path = out / f"{stem}.csv"
        with csv_path.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(dict_rows[0].keys()))
            writer.writeheader()
            writer.writerows(dict_rows)
        artifacts.append(str(csv_path))
    return artifacts


def _participant_summary_rows(rows: list[CandidateResult]) -> list[dict[str, Any]]:
    by_participant: dict[str, CandidateResult] = {}
    for row in rows:
        by_participant.setdefault(row.participant_id, row)
    return [
        {
            "participant_id": row.participant_id,
            "name": row.name,
            "probability": round(row.probability, 6),
            "wins": row.wins,
            "classification": row.classification,
            "diagnostic_flags": list(row.diagnostic_flags),
        }
        for row in sorted(
            by_participant.values(),
            key=lambda item: item.probability,
            reverse=True,
        )
    ]


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Simulate a generic path-dependent event market from a JSON event pack."
    )
    parser.add_argument("--input", required=True, help="event pack JSON file")
    parser.add_argument("--iterations", type=int, default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--out", default=".wayfinder_runs/sports")
    parser.add_argument("--stem", default="event_sim")
    args = parser.parse_args()

    data = json.loads(Path(args.input).read_text())
    if args.iterations is not None:
        data["iterations"] = args.iterations
    if args.seed is not None:
        data["seed"] = args.seed
    config = load_config(data)
    rows = run_simulation(config)
    artifacts = write_artifacts(rows, config, args.out, stem=args.stem)
    print(render(rows, top=args.top))
    print()
    print("artifacts:", " ".join(artifacts) if artifacts else "(none)")


if __name__ == "__main__":
    _main()
