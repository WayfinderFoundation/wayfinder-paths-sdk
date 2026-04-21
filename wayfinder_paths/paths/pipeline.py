from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

STANDARD_OUTPUT_CONTRACT: tuple[str, ...] = (
    "signal_snapshot",
    "selected_playbook",
    "candidate_expressions",
    "null_state",
    "risk_checks",
    "job",
    "next_invalidation",
)

DEFAULT_ARTIFACTS_DIR = ".wf-artifacts"
DEFAULT_PRIMARY_HOSTS: tuple[str, ...] = ("claude", "opencode")


class PipelineGraphError(Exception):
    pass


@dataclass(frozen=True)
class PipelineEdge:
    source: str
    target: str


@dataclass(frozen=True)
class PipelineFailureEdge:
    source: str
    event: str
    target: str
    max_retries: int | None


@dataclass(frozen=True)
class PipelineGraph:
    nodes: tuple[str, ...]
    edges: tuple[PipelineEdge, ...]
    failure_edges: tuple[PipelineFailureEdge, ...]


@dataclass(frozen=True)
class ArchetypeInputSlot:
    name: str
    file_type: str
    path: str
    schema: str
    required: bool


@dataclass(frozen=True)
class ArchetypeAgent:
    agent_id: str
    phase: str
    description: str
    tools: tuple[str, ...]
    output_name: str
    host_mode: str = "worker"


@dataclass(frozen=True)
class PipelineArchetype:
    archetype_id: str
    entry_command: str
    required_policy_sections: tuple[str, ...]
    required_nodes: tuple[str, ...]
    default_edges: tuple[tuple[str, str], ...]
    default_failure_edges: tuple[tuple[str, str, str, int | None], ...]
    input_slots: tuple[ArchetypeInputSlot, ...]
    agents: tuple[ArchetypeAgent, ...]
    # Skills that must be loaded before writing any scripts for this archetype.
    # The skill renderer injects these as prerequisites in generated instructions.
    required_skills: tuple[str, ...] = ()
    # Archetype-specific output-contract fields appended to STANDARD_OUTPUT_CONTRACT.
    # Used by pipelines that produce additional required artifacts (e.g. narrative-radar's trade_book).
    extra_output_contract: tuple[str, ...] = ()


_ARCHETYPES: dict[str, PipelineArchetype] = {
    "conditional-router": PipelineArchetype(
        archetype_id="conditional-router",
        entry_command="conditional-bet",
        required_skills=(
            "using-polymarket-adapter",
            "using-hyperliquid-adapter",
        ),
        required_policy_sections=(
            "archetype",
            "signals",
            "playbooks",
            "null_state",
            "risk",
            "scheduler",
        ),
        required_nodes=(
            "intake",
            "normalize_thesis",
            "market_research",
            "proxy_mapping",
            "qual_research",
            "synthesize",
            "skeptic",
            "risk_gate",
            "compile_job",
            "finalize",
        ),
        default_edges=(
            ("intake", "normalize_thesis"),
            ("normalize_thesis", "market_research"),
            ("normalize_thesis", "proxy_mapping"),
            ("normalize_thesis", "qual_research"),
            ("market_research", "synthesize"),
            ("proxy_mapping", "synthesize"),
            ("qual_research", "synthesize"),
            ("synthesize", "skeptic"),
            ("skeptic", "risk_gate"),
            ("risk_gate", "compile_job"),
            ("compile_job", "finalize"),
        ),
        default_failure_edges=(
            ("market_research", "retryable_error", "market_research", 1),
            ("market_research", "exhausted", "skeptic", 0),
            ("risk_gate", "failed", "finalize", 0),
            ("compile_job", "failed", "finalize", 0),
        ),
        input_slots=(
            ArchetypeInputSlot(
                name="thesis",
                file_type="markdown",
                path="inputs/thesis.md",
                schema="schemas/thesis.schema.json",
                required=True,
            ),
            ArchetypeInputSlot(
                name="mappings",
                file_type="yaml",
                path="inputs/mappings.yaml",
                schema="schemas/mappings.schema.json",
                required=False,
            ),
            ArchetypeInputSlot(
                name="preferences",
                file_type="yaml",
                path="inputs/preferences.yaml",
                schema="schemas/preferences.schema.json",
                required=False,
            ),
        ),
        agents=(
            ArchetypeAgent(
                agent_id="thesis-normalizer",
                phase="normalize_thesis",
                description="Normalize rough user thesis text into structured thresholds and trade triggers.",
                tools=("read", "glob", "grep", "bash"),
                output_name="normalize_thesis.json",
            ),
            ArchetypeAgent(
                agent_id="poly-scout",
                phase="market_research",
                description="Find candidate Polymarket markets and score liquidity, spread, history, and clarity.",
                tools=("read", "glob", "grep", "bash", "webfetch", "websearch"),
                output_name="market_research.json",
            ),
            ArchetypeAgent(
                agent_id="proxy-mapper",
                phase="proxy_mapping",
                description="Map candidate conditions to direct and proxy expressions using the declared playbooks.",
                tools=("read", "glob", "grep", "bash"),
                output_name="proxy_mapping.json",
            ),
            ArchetypeAgent(
                agent_id="qual-researcher",
                phase="qual_research",
                description="Summarize relevant qualitative context and flag unsupported assumptions.",
                tools=("read", "glob", "grep", "bash", "webfetch", "websearch"),
                output_name="qual_research.json",
            ),
            ArchetypeAgent(
                agent_id="null-skeptic",
                phase="skeptic",
                description="Compare all candidates to the null state and reject weak edges.",
                tools=("read", "glob", "grep", "bash"),
                output_name="skeptic.json",
            ),
            ArchetypeAgent(
                agent_id="risk-verifier",
                phase="risk_gate",
                description="Apply risk limits, downgrade unsafe actions to draft, or reject.",
                tools=("read", "glob", "grep", "bash"),
                output_name="risk_gate.json",
            ),
            ArchetypeAgent(
                agent_id="job-compiler",
                phase="compile_job",
                description="Compile the validated policy into a monitorable runner job artifact.",
                tools=("read", "glob", "grep", "bash"),
                output_name="job.json",
            ),
        ),
    ),
    "hedge-finder": PipelineArchetype(
        archetype_id="hedge-finder",
        entry_command="hedge-finder",
        required_skills=(
            "using-delta-lab",
            "using-hyperliquid-adapter",
            "using-pool-token-balance-data",
        ),
        required_policy_sections=(
            "archetype",
            "signals",
            "decision",
            "risk",
            "scheduler",
            "null_state",
        ),
        required_nodes=(
            "intake",
            "exposure_reader",
            "beta_modeler",
            "hedge_search",
            "optimizer",
            "skeptic",
            "risk_gate",
            "compile_job",
            "finalize",
        ),
        default_edges=(
            ("intake", "exposure_reader"),
            ("exposure_reader", "beta_modeler"),
            ("beta_modeler", "hedge_search"),
            ("hedge_search", "optimizer"),
            ("optimizer", "skeptic"),
            ("skeptic", "risk_gate"),
            ("risk_gate", "compile_job"),
            ("compile_job", "finalize"),
        ),
        default_failure_edges=(
            ("hedge_search", "retryable_error", "hedge_search", 1),
            ("optimizer", "failed", "skeptic", 0),
            ("risk_gate", "failed", "finalize", 0),
        ),
        input_slots=(
            ArchetypeInputSlot(
                name="assets",
                file_type="yaml",
                path="inputs/assets.yaml",
                schema="schemas/assets.schema.json",
                required=True,
            ),
            ArchetypeInputSlot(
                name="constraints",
                file_type="yaml",
                path="inputs/constraints.yaml",
                schema="schemas/constraints.schema.json",
                required=True,
            ),
        ),
        agents=(
            ArchetypeAgent(
                agent_id="exposure-reader",
                phase="exposure_reader",
                description="Resolve symbols, fetch time series, and build the portfolio series.",
                tools=("read", "glob", "grep", "bash"),
                output_name="exposure_reader.json",
            ),
            ArchetypeAgent(
                agent_id="beta-modeler",
                phase="beta_modeler",
                description="Estimate factor betas and measure hedge stability.",
                tools=("read", "glob", "grep", "bash"),
                output_name="beta_modeler.json",
            ),
            ArchetypeAgent(
                agent_id="hedge-searcher",
                phase="hedge_search",
                description="Collect hedge candidates with funding, spread, and liquidity context.",
                tools=("read", "glob", "grep", "bash"),
                output_name="hedge_search.json",
            ),
            ArchetypeAgent(
                agent_id="skeptic",
                phase="skeptic",
                description="Reject hedges whose improvement over null is not material.",
                tools=("read", "glob", "grep", "bash"),
                output_name="skeptic.json",
            ),
            ArchetypeAgent(
                agent_id="risk-verifier",
                phase="risk_gate",
                description="Apply notional, leverage, and execution protections before job creation.",
                tools=("read", "glob", "grep", "bash"),
                output_name="risk_gate.json",
            ),
            ArchetypeAgent(
                agent_id="job-compiler",
                phase="compile_job",
                description="Compile the selected hedge into a draft or armed rebalance job.",
                tools=("read", "glob", "grep", "bash"),
                output_name="job.json",
            ),
        ),
    ),
    "spread-radar": PipelineArchetype(
        archetype_id="spread-radar",
        entry_command="spread-radar",
        required_skills=(
            "using-delta-lab",
            "using-hyperliquid-adapter",
            "using-pool-token-balance-data",
        ),
        required_policy_sections=(
            "archetype",
            "universe",
            "features",
            "clustering",
            "candidate_rules",
            "scoring",
            "null_state",
        ),
        required_nodes=(
            "intake",
            "universe_builder",
            "pair_screener",
            "signal_research",
            "skeptic",
            "finalize",
        ),
        default_edges=(
            ("intake", "universe_builder"),
            ("universe_builder", "pair_screener"),
            ("pair_screener", "signal_research"),
            ("signal_research", "skeptic"),
            ("skeptic", "finalize"),
        ),
        default_failure_edges=(
            ("universe_builder", "insufficient_data", "finalize", 0),
            ("pair_screener", "no_pairs", "finalize", 0),
            ("signal_research", "no_edge", "skeptic", 0),
        ),
        input_slots=(
            ArchetypeInputSlot(
                name="theme",
                file_type="markdown",
                path="inputs/theme.md",
                schema="schemas/theme.schema.json",
                required=True,
            ),
            ArchetypeInputSlot(
                name="universe",
                file_type="yaml",
                path="inputs/universe.yaml",
                schema="schemas/universe.schema.json",
                required=False,
            ),
            ArchetypeInputSlot(
                name="notes",
                file_type="markdown",
                path="inputs/notes.md",
                schema="schemas/notes.schema.json",
                required=False,
            ),
        ),
        agents=(
            ArchetypeAgent(
                agent_id="universe-builder",
                phase="universe_builder",
                description="Resolve the asset universe and fetch price/funding data.",
                tools=("read", "glob", "grep", "bash"),
                output_name="universe.json",
            ),
            ArchetypeAgent(
                agent_id="pair-screener",
                phase="pair_screener",
                description="Screen all pairs by half-life and cointegration, classify as stable or drift.",
                tools=("read", "glob", "grep", "bash"),
                output_name="pair_screen.json",
            ),
            ArchetypeAgent(
                agent_id="signal-researcher",
                phase="signal_research",
                description="Parameter sweep with walk-forward backtesting to find the best signal config.",
                tools=("read", "glob", "grep", "bash"),
                output_name="signal_research.json",
            ),
            ArchetypeAgent(
                agent_id="skeptic",
                phase="skeptic",
                description="Quantitative validation: hidden beta, fee sensitivity, parameter robustness, concentration.",
                tools=("read", "glob", "grep", "bash"),
                output_name="skeptic.json",
            ),
        ),
    ),
    "narrative-radar": PipelineArchetype(
        archetype_id="narrative-radar",
        entry_command="narrative-radar",
        required_skills=(
            "using-polymarket-adapter",
            "using-hyperliquid-adapter",
            "using-alpha-lab",
        ),
        extra_output_contract=("trade_book",),
        required_policy_sections=(
            "archetype",
            "domains",
            "verification_protocol",
            "novelty_gate",
            "adversarial",
            "confidence",
            "portfolio_strategy",
            "null_state",
        ),
        required_nodes=(
            "intake",
            "geopolitical_scan",
            "macro_scan",
            "regulatory_scan",
            "tech_scan",
            "structural_scan",
            "thesis_synthesis",
            "novelty_gate",
            "pre_mortem",
            "consensus_audit",
            "historical_analog",
            "portfolio_strategy",
            "compile_inventory",
            "display_compose",
            "finalize",
        ),
        default_edges=(
            ("intake", "geopolitical_scan"),
            ("intake", "macro_scan"),
            ("intake", "regulatory_scan"),
            ("intake", "tech_scan"),
            ("intake", "structural_scan"),
            ("geopolitical_scan", "thesis_synthesis"),
            ("macro_scan", "thesis_synthesis"),
            ("regulatory_scan", "thesis_synthesis"),
            ("tech_scan", "thesis_synthesis"),
            ("structural_scan", "thesis_synthesis"),
            ("thesis_synthesis", "novelty_gate"),
            ("novelty_gate", "pre_mortem"),
            ("pre_mortem", "consensus_audit"),
            ("consensus_audit", "historical_analog"),
            ("historical_analog", "portfolio_strategy"),
            ("portfolio_strategy", "compile_inventory"),
            ("compile_inventory", "display_compose"),
            ("display_compose", "finalize"),
        ),
        default_failure_edges=(
            ("geopolitical_scan", "retryable_error", "geopolitical_scan", 1),
            ("macro_scan", "retryable_error", "macro_scan", 1),
            ("regulatory_scan", "retryable_error", "regulatory_scan", 1),
            ("tech_scan", "retryable_error", "tech_scan", 1),
            ("structural_scan", "retryable_error", "structural_scan", 1),
            ("novelty_gate", "all_killed", "finalize", 0),
            ("historical_analog", "all_rejected", "compile_inventory", 0),
            ("portfolio_strategy", "no_instruments", "compile_inventory", 0),
        ),
        input_slots=(
            ArchetypeInputSlot(
                name="scan_config",
                file_type="yaml",
                path="inputs/scan_config.yaml",
                schema="schemas/scan_config.schema.json",
                required=True,
            ),
            ArchetypeInputSlot(
                name="inventory",
                file_type="json",
                path="inputs/inventory.json",
                schema="schemas/inventory.schema.json",
                required=False,
            ),
            ArchetypeInputSlot(
                name="portfolio",
                file_type="yaml",
                path="inputs/portfolio.yaml",
                schema="schemas/portfolio.schema.json",
                required=False,
            ),
            ArchetypeInputSlot(
                name="watchlist",
                file_type="yaml",
                path="inputs/watchlist.yaml",
                schema="schemas/watchlist.schema.json",
                required=False,
            ),
        ),
        agents=(
            ArchetypeAgent(
                agent_id="geopolitical-analyst",
                phase="geopolitical_scan",
                description="Research interstate tensions, alliance shifts, conflict escalation ladders, and sanctions trajectories using think tank publications and diplomatic signals.",
                tools=("read", "glob", "grep", "bash", "webfetch", "websearch"),
                output_name="geopolitical_scan.json",
            ),
            ArchetypeAgent(
                agent_id="macro-strategist",
                phase="macro_scan",
                description="Research fiscal sustainability, monetary policy divergence, trade imbalances, and commodity supply constraints using central bank and institutional publications.",
                tools=("read", "glob", "grep", "bash", "webfetch", "websearch"),
                output_name="macro_scan.json",
            ),
            ArchetypeAgent(
                agent_id="regulatory-tracker",
                phase="regulatory_scan",
                description="Track legislation in pipeline, enforcement pattern shifts, consultation papers, and international regulatory coordination.",
                tools=("read", "glob", "grep", "bash", "webfetch", "websearch"),
                output_name="regulatory_scan.json",
            ),
            ArchetypeAgent(
                agent_id="tech-scout",
                phase="tech_scan",
                description="Identify technology capability thresholds approaching, adoption curves accelerating, and infrastructure buildouts that will reshape markets.",
                tools=("read", "glob", "grep", "bash", "webfetch", "websearch"),
                output_name="tech_scan.json",
            ),
            ArchetypeAgent(
                agent_id="structural-analyst",
                phase="structural_scan",
                description="Research demographic shifts, energy transition milestones, supply chain restructuring, and institutional changes with long-cycle market implications.",
                tools=("read", "glob", "grep", "bash", "webfetch", "websearch"),
                output_name="structural_scan.json",
            ),
            ArchetypeAgent(
                agent_id="thesis-synthesizer",
                phase="thesis_synthesis",
                description="Merge domain scan outputs, deduplicate theses, reconcile cross-domain reinforcement, and apply confidence updates to the existing inventory.",
                tools=("read", "glob", "grep", "bash"),
                output_name="thesis_synthesis.json",
            ),
            ArchetypeAgent(
                agent_id="novelty-gate",
                phase="novelty_gate",
                description="Filter out theses that are already mainstream by checking coverage volume in major financial media and existing Polymarket market liquidity.",
                tools=("read", "glob", "grep", "bash", "webfetch", "websearch"),
                output_name="novelty_gate.json",
            ),
            ArchetypeAgent(
                agent_id="pre-mortem-analyst",
                phase="pre_mortem",
                description="For each surviving thesis, assume it is wrong and construct the most likely failure scenario to expose weak causal chains and confirmation bias.",
                tools=("read", "glob", "grep", "bash", "webfetch", "websearch"),
                output_name="pre_mortem.json",
            ),
            ArchetypeAgent(
                agent_id="consensus-auditor",
                phase="consensus_audit",
                description="Search for contrarian takes on each thesis, find the strongest counter-arguments, and flag theses where the consensus view contradicts the thesis.",
                tools=("read", "glob", "grep", "bash", "webfetch", "websearch"),
                output_name="consensus_audit.json",
            ),
            ArchetypeAgent(
                agent_id="historical-analogist",
                phase="historical_analog",
                description="Find the closest historical parallel for each thesis, evaluate what happened, and assess whether the base rate supports the thesis materializing.",
                tools=("read", "glob", "grep", "bash", "webfetch", "websearch"),
                output_name="historical_analog.json",
            ),
            ArchetypeAgent(
                agent_id="portfolio-strategist",
                phase="portfolio_strategy",
                description="For validated theses, map to tradeable instruments on Polymarket and Hyperliquid, define transmission mechanisms, and propose trade structures with monitoring triggers.",
                tools=("read", "glob", "grep", "bash", "webfetch", "websearch"),
                output_name="portfolio_strategy.json",
            ),
            ArchetypeAgent(
                agent_id="inventory-compiler",
                phase="compile_inventory",
                description="Compile the final thesis inventory with confidence trajectories, evidence logs, portfolio actions, and monitoring checklists for persistence across runs.",
                tools=("read", "glob", "grep", "bash"),
                output_name="inventory.json",
            ),
            ArchetypeAgent(
                agent_id="display-composer",
                phase="display_compose",
                description="Transform the compiled inventory into a display-ready applet data bundle with human-readable summaries, plain-English descriptions, editorial tags, and computed layout hints.",
                tools=("read", "glob", "grep", "bash"),
                output_name="display.json",
            ),
        ),
    ),
}


def list_pipeline_archetypes() -> tuple[str, ...]:
    return tuple(sorted(_ARCHETYPES))


def get_pipeline_archetype(archetype_id: str) -> PipelineArchetype:
    normalized = str(archetype_id or "").strip()
    try:
        return _ARCHETYPES[normalized]
    except KeyError as exc:
        expected = ", ".join(list_pipeline_archetypes())
        raise PipelineGraphError(
            f"Unknown pipeline archetype '{normalized}'. Expected one of: {expected}"
        ) from exc


def archetype_output_contract(archetype: PipelineArchetype) -> tuple[str, ...]:
    return STANDARD_OUTPUT_CONTRACT + archetype.extra_output_contract


def default_pipeline_graph(archetype_id: str) -> PipelineGraph:
    archetype = get_pipeline_archetype(archetype_id)
    return PipelineGraph(
        nodes=archetype.required_nodes,
        edges=tuple(
            PipelineEdge(source=source, target=target)
            for source, target in archetype.default_edges
        ),
        failure_edges=tuple(
            PipelineFailureEdge(
                source=source,
                event=event,
                target=target,
                max_retries=max_retries,
            )
            for source, event, target, max_retries in archetype.default_failure_edges
        ),
    )


def parse_pipeline_graph(raw_obj: Any) -> PipelineGraph:
    if not isinstance(raw_obj, dict):
        raise PipelineGraphError("pipeline/graph.yaml must be a YAML object")

    raw_nodes = raw_obj.get("nodes") or []
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise PipelineGraphError("pipeline/graph.yaml nodes must be a non-empty list")
    nodes: list[str] = []
    for idx, item in enumerate(raw_nodes):
        if isinstance(item, str):
            node_id = item.strip()
        elif isinstance(item, dict):
            node_id = str(item.get("id") or "").strip()
        else:
            raise PipelineGraphError(
                f"pipeline/graph.yaml nodes[{idx}] must be a string or object"
            )
        if not node_id:
            raise PipelineGraphError(
                f"pipeline/graph.yaml nodes[{idx}] is missing a non-empty id"
            )
        if node_id in nodes:
            raise PipelineGraphError(
                f"pipeline/graph.yaml contains duplicate node id: {node_id}"
            )
        nodes.append(node_id)

    def _parse_edge(item: Any, *, name: str, index: int) -> tuple[str, str]:
        if not isinstance(item, dict):
            raise PipelineGraphError(
                f"pipeline/graph.yaml {name}[{index}] must be an object"
            )
        source = str(item.get("from") or "").strip()
        target = str(item.get("to") or "").strip()
        if not source or not target:
            raise PipelineGraphError(
                f"pipeline/graph.yaml {name}[{index}] must define from and to"
            )
        return source, target

    raw_edges = raw_obj.get("edges") or []
    if not isinstance(raw_edges, list):
        raise PipelineGraphError("pipeline/graph.yaml edges must be a list")
    edges = tuple(
        PipelineEdge(source=source, target=target)
        for idx, item in enumerate(raw_edges)
        for source, target in (_parse_edge(item, name="edges", index=idx),)
    )

    raw_failure_edges = raw_obj.get("failure_edges") or []
    if not isinstance(raw_failure_edges, list):
        raise PipelineGraphError("pipeline/graph.yaml failure_edges must be a list")
    failure_edges: list[PipelineFailureEdge] = []
    for idx, item in enumerate(raw_failure_edges):
        if not isinstance(item, dict):
            raise PipelineGraphError(
                f"pipeline/graph.yaml failure_edges[{idx}] must be an object"
            )
        source = str(item.get("from") or "").strip()
        event_raw = item.get("on")
        if event_raw is None and True in item:
            event_raw = item.get(True)
        event = str(event_raw or "").strip()
        target = str(item.get("to") or "").strip()
        retries_raw = item.get("max_retries")
        if retries_raw is None:
            max_retries: int | None = None
        elif isinstance(retries_raw, int) and retries_raw >= 0:
            max_retries = retries_raw
        else:
            raise PipelineGraphError(
                f"pipeline/graph.yaml failure_edges[{idx}].max_retries must be a non-negative integer"
            )
        if not source or not event or not target:
            raise PipelineGraphError(
                f"pipeline/graph.yaml failure_edges[{idx}] must define from, on, and to"
            )
        failure_edges.append(
            PipelineFailureEdge(
                source=source,
                event=event,
                target=target,
                max_retries=max_retries,
            )
        )

    return PipelineGraph(
        nodes=tuple(nodes),
        edges=edges,
        failure_edges=tuple(failure_edges),
    )


def load_pipeline_graph(graph_path: Path) -> PipelineGraph:
    try:
        raw_obj = yaml.safe_load(graph_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise PipelineGraphError(f"Failed to parse {graph_path}") from exc
    return parse_pipeline_graph(raw_obj)


def validate_pipeline_graph(
    graph: PipelineGraph,
    *,
    archetype: str | None = None,
) -> None:
    node_set = set(graph.nodes)
    for edge in graph.edges:
        if edge.source not in node_set:
            raise PipelineGraphError(
                f"pipeline graph edge references unknown source node: {edge.source}"
            )
        if edge.target not in node_set:
            raise PipelineGraphError(
                f"pipeline graph edge references unknown target node: {edge.target}"
            )
    for edge in graph.failure_edges:
        if edge.source not in node_set:
            raise PipelineGraphError(
                "pipeline graph failure edge references unknown source node: "
                f"{edge.source}"
            )
        if edge.target not in node_set:
            raise PipelineGraphError(
                "pipeline graph failure edge references unknown target node: "
                f"{edge.target}"
            )

    incoming: dict[str, set[str]] = {node: set() for node in graph.nodes}
    outgoing: dict[str, set[str]] = {node: set() for node in graph.nodes}
    for edge in graph.edges:
        incoming[edge.target].add(edge.source)
        outgoing[edge.source].add(edge.target)

    sources = [node for node, parents in incoming.items() if not parents]
    if not sources:
        raise PipelineGraphError("pipeline graph must contain at least one source node")
    if "finalize" not in node_set:
        raise PipelineGraphError("pipeline graph must include a finalize node")

    visited: set[str] = set()
    stack = list(sources)
    while stack:
        node = stack.pop()
        if node in visited:
            continue
        visited.add(node)
        stack.extend(sorted(outgoing[node]))
    if visited != node_set:
        missing = ", ".join(sorted(node_set - visited))
        raise PipelineGraphError(
            f"pipeline graph contains unreachable nodes: {missing}"
        )

    reverse_outgoing: dict[str, set[str]] = {node: set() for node in graph.nodes}
    for edge in graph.edges:
        reverse_outgoing[edge.target].add(edge.source)

    can_finish: set[str] = set()
    stack = ["finalize"]
    while stack:
        node = stack.pop()
        if node in can_finish:
            continue
        can_finish.add(node)
        stack.extend(sorted(reverse_outgoing[node]))
    if can_finish != node_set:
        missing = ", ".join(sorted(node_set - can_finish))
        raise PipelineGraphError(
            f"pipeline graph nodes cannot reach finalize: {missing}"
        )

    if archetype:
        archetype_config = get_pipeline_archetype(archetype)
        missing_nodes = set(archetype_config.required_nodes) - node_set
        if missing_nodes:
            missing = ", ".join(sorted(missing_nodes))
            raise PipelineGraphError(
                f"pipeline graph is missing required {archetype} nodes: {missing}"
            )
