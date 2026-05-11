from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata as importlib_metadata
from importlib import resources
from pathlib import Path
from typing import Any

from wayfinder_paths.paths.pipeline import (
    DEFAULT_ARTIFACTS_DIR,
    STANDARD_OUTPUT_CONTRACT,
    ArchetypeAgent,
    ArchetypeInputSlot,
    default_pipeline_graph,
    get_pipeline_archetype,
)


class PathScaffoldError(Exception):
    pass


_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    value = re.sub(r"-{2,}", "-", value)
    return value


def humanize_slug(slug: str) -> str:
    parts = [p for p in re.split(r"[-_]+", slug.strip()) if p]
    return " ".join([p[:1].upper() + p[1:] for p in parts]) if parts else slug


def _yaml_quote(value: str) -> str:
    escaped = (value or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render_template(text: str, context: dict[str, Any]) -> str:
    rendered = text
    for key, value in context.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
    return rendered


def _read_template(relative_path: str) -> str:
    root = resources.files("wayfinder_paths.paths")
    template_path = root.joinpath("templates").joinpath(relative_path)
    return template_path.read_text(encoding="utf-8")


def _runtime_package_version(package: str = "wayfinder-paths") -> str:
    try:
        return importlib_metadata.version(package)
    except importlib_metadata.PackageNotFoundError:
        return "0.0.0"


@dataclass(frozen=True)
class PathInitResult:
    path_dir: Path
    manifest_path: Path
    created_files: list[Path]
    overwritten_files: list[Path]
    skipped_files: list[Path]


def _build_wfpath_yaml(
    *,
    slug: str,
    name: str,
    version: str,
    summary: str,
    primary_kind: str,
    tags: list[str],
    component_kind: str,
    component_path: str,
    with_applet: bool,
    with_skill: bool,
    template: str,
    archetype: str | None,
) -> str:
    tags_unique: list[str] = []
    for tag in tags:
        t = str(tag).strip()
        if not t:
            continue
        if t not in tags_unique:
            tags_unique.append(t)

    description = summary.strip() or f"Use the {slug} path through Wayfinder."

    lines: list[str] = []
    lines.append('schema_version: "0.1"')
    lines.append("")
    lines.append(f"slug: {slug}")
    lines.append(f"name: {_yaml_quote(name)}")
    lines.append(f"version: {_yaml_quote(version)}")
    if summary.strip():
        lines.append(f"summary: {_yaml_quote(summary)}")
    lines.append("")
    lines.append(f"primary_kind: {primary_kind}")
    lines.append("tags:")
    for tag in tags_unique:
        lines.append(f"  - {tag}")

    lines.append("")
    lines.append("components:")
    lines.append('  - id: "main"')
    lines.append(f"    kind: {component_kind}")
    lines.append(f"    path: {_yaml_quote(component_path)}")

    if with_applet:
        lines.append("")
        lines.append("applet:")
        lines.append('  build_dir: "applet/dist"')
        lines.append('  manifest: "applet/applet.manifest.json"')

    if with_skill:
        lines.append("")
        lines.append("skill:")
        lines.append("  enabled: true")
        lines.append("  source: generated")
        lines.append(f"  name: {_yaml_quote(slug)}")
        lines.append(f"  description: {_yaml_quote(description)}")
        lines.append('  instructions: "skill/instructions.md"')
        lines.append("  runtime:")
        lines.append("    mode: thin")
        lines.append('    package: "wayfinder-paths"')
        lines.append(f'    version: "{_runtime_package_version()}"')
        lines.append('    python: ">=3.12,<3.13"')
        lines.append('    component: "main"')
        lines.append("    bootstrap: uv")
        lines.append("    fallback_bootstrap: pipx")
        lines.append("    prefer_existing_runtime: true")
        lines.append("    require_api_key: false")
        lines.append('    api_key_env: "WAYFINDER_API_KEY"')
        lines.append('    config_path_env: "WAYFINDER_CONFIG_PATH"')

    if template == "pipeline" and archetype:
        archetype_config = get_pipeline_archetype(archetype)
        graph = default_pipeline_graph(archetype)

        lines.append("")
        lines.append("pipeline:")
        lines.append(f'  archetype: "{archetype_config.archetype_id}"')
        lines.append('  graph: "pipeline/graph.yaml"')
        lines.append(f'  artifacts_dir: "{DEFAULT_ARTIFACTS_DIR}"')
        lines.append(f'  entry_command: "{archetype_config.entry_command}"')
        lines.append("  primary_hosts:")
        lines.append("    - claude")
        lines.append("    - opencode")
        lines.append("  output_contract:")
        for field in STANDARD_OUTPUT_CONTRACT + archetype_config.extra_output_contract:
            lines.append(f"    - {field}")

        lines.append("")
        lines.append("inputs:")
        lines.append("  slots:")
        for slot in archetype_config.input_slots:
            lines.append(f"    {slot.name}:")
            lines.append(f'      type: "{slot.file_type}"')
            lines.append(f'      path: "{slot.path}"')
            lines.append(f'      schema: "{slot.schema}"')
            lines.append(f"      required: {str(slot.required).lower()}")

        lines.append("")
        lines.append("agents:")
        for agent in archetype_config.agents:
            lines.append(f'  - id: "{agent.agent_id}"')
            lines.append(f'    phase: "{agent.phase}"')
            lines.append(f"    description: {_yaml_quote(agent.description)}")
            lines.append("    tools:")
            for tool in agent.tools:
                lines.append(f'      - "{tool}"')
            lines.append(
                f'    output: "{DEFAULT_ARTIFACTS_DIR}/$RUN_ID/{agent.output_name}"'
            )
            lines.append(f'    host_mode: "{agent.host_mode}"')

        lines.append("")
        lines.append("host:")
        lines.append("  claude:")
        lines.append('    rules_file: ".claude/CLAUDE.md"')
        lines.append('    skill_dir: ".claude/skills"')
        lines.append('    agent_dir: ".claude/agents"')
        lines.append('    settings_file: ".claude/settings.json"')
        lines.append("  opencode:")
        lines.append('    rules_file: "AGENTS.md"')
        lines.append('    config_file: "opencode.json"')
        lines.append('    skill_dir: ".opencode/skills"')
        lines.append('    agent_dir: ".opencode/agents"')
        lines.append('    command_dir: ".opencode/commands"')
        lines.append('    plugin_dir: ".opencode/plugins"')
        lines.append('    tool_dir: ".opencode/tools"')

        lines.append("")
        lines.append("runtime:")
        lines.append('  state_dir: ".wf-state"')
        lines.append('  tests_dir: "tests"')
        lines.append("  graph_nodes:")
        for node in graph.nodes:
            lines.append(f'    - "{node}"')

    lines.append("")
    return "\n".join(lines)


def _pipeline_readme(
    *,
    name: str,
    slug: str,
    summary: str,
    archetype: str,
    component_path: str,
) -> str:
    description = (
        summary.strip()
        or f"Reference path for the `{archetype}` strategy-pipeline archetype."
    )
    return (
        f"# {name}\n\n"
        f"{description}\n\n"
        "## Why this exists\n\n"
        "This path is the in-repo gold reference for compiled strategy pipelines. "
        "It shows the canonical authoring shape, fixed artifact contract, fixture-driven evals, "
        "and host-specific renders for Claude and OpenCode.\n\n"
        "## Core files\n\n"
        "- `wfpath.yaml` defines the manifest, pipeline metadata, inputs, agents, and host targets.\n"
        "- `policy/default.yaml` holds the strategy policy and risk gates as data.\n"
        "- `pipeline/graph.yaml` defines the ordered workflow graph and failure edges.\n"
        f"- `{component_path}` is the local reference component for the path.\n"
        "- `skill/instructions.md`, `skill/references/`, and `skill/agents/` define the canonical skill layer.\n"
        "- `tests/fixtures/` and `tests/evals/` define output-shape, null-state, risk-gate, and host-render checks.\n\n"
        "## Workflow shape\n\n"
        "1. intake and normalize user intent\n"
        "2. gather signals and supporting research\n"
        "3. generate candidate expressions\n"
        "4. rank against a mandatory null state\n"
        "5. apply risk and execution gates\n"
        "6. compile the job or degrade to draft/null\n"
        "7. emit the standard response envelope\n\n"
        "## Develop\n\n"
        "```bash\n"
        "poetry run wayfinder path doctor --path .\n"
        "poetry run wayfinder path eval --path .\n"
        "poetry run wayfinder path render-skill --path .\n"
        "poetry run wayfinder path build --path . --out dist/bundle.zip\n"
        "```\n"
    )


def _slot_placeholder(slot: ArchetypeInputSlot, *, archetype: str) -> str:
    if archetype == "conditional-router":
        if slot.name == "thesis":
            return (
                "# Thesis\n\n"
                "If US recession probability rises above 60%, reduce alt beta.\n"
                "If it rises above 80%, short the alt basket.\n"
                "If it falls below 35%, re-add risk.\n"
            )
        if slot.name == "mappings":
            return (
                "conditions:\n"
                "  recession:\n"
                '    polymarket_search: "US recession"\n'
                "    proxies:\n"
                "      risk_off:\n"
                '        sell: ["SOL", "DOGE"]\n'
                "      crash_mode:\n"
                '        short: ["SOL", "DOGE", "XRP"]\n'
                "      risk_on:\n"
                '        long: ["ETH", "SOL"]\n'
            )
        if slot.name == "preferences":
            return (
                'execution_mode: "draft"\n'
                "prefer_direct_market: false\n"
                "max_candidates: 4\n"
                "allow_shorting: true\n"
            )
    if archetype == "hedge-finder":
        if slot.name == "assets":
            return (
                "assets:\n"
                '  - symbol: "ETH"\n'
                "    weight_usd: 40000\n"
                '  - symbol: "SOL"\n'
                "    weight_usd: 15000\n"
                '  - symbol: "HYPE"\n'
                "    weight_usd: 10000\n"
            )
        if slot.name == "constraints":
            return (
                'factors: ["BTC", "ETH", "SOL"]\n'
                "max_hedges: 3\n"
                "target_residual_beta: 0.15\n"
                "rebalance_band: 0.10\n"
                "max_leverage: 2\n"
                'margin_mode: "isolated"\n'
            )
    if archetype == "spread-radar":
        if slot.name == "theme":
            return (
                "# Theme\n\n"
                "Find a spread in alt-L1s where the pair still has a catalyst and "
                "not just a statistical gap.\n"
            )
        if slot.name == "universe":
            return 'symbols: ["SOL", "SUI", "AVAX", "HYPE"]\noverrides: ["SEI"]\n'
        if slot.name == "notes":
            return (
                "# Notes\n\n"
                "- Favor simple two-leg spreads over baskets unless the catalyst is cluster-wide.\n"
                "- Reject trades that are mostly directional beta.\n"
            )
    if archetype == "narrative-radar":
        if slot.name == "scan_config":
            return (
                "domains:\n"
                "  geopolitical:\n"
                "    enabled: true\n"
                "    focus_regions:\n"
                '      - "Middle East"\n'
                '      - "East Asia"\n'
                '      - "Europe"\n'
                "  macro:\n"
                "    enabled: true\n"
                "    focus_areas:\n"
                '      - "sovereign debt"\n'
                '      - "central bank policy"\n'
                '      - "commodity supply"\n'
                "  regulatory:\n"
                "    enabled: true\n"
                "    focus_areas:\n"
                '      - "crypto regulation"\n'
                '      - "AI governance"\n'
                '      - "trade policy"\n'
                "  tech:\n"
                "    enabled: true\n"
                "    focus_areas:\n"
                '      - "AI capabilities"\n'
                '      - "blockchain infrastructure"\n'
                "  structural:\n"
                "    enabled: true\n"
                "    focus_areas:\n"
                '      - "energy transition"\n'
                '      - "supply chain restructuring"\n'
                "\n"
                "novelty_threshold: 0.30\n"
                "confidence_threshold: 0.30\n"
                "portfolio_confidence_threshold: 0.35\n"
                "max_theses_per_domain: 5\n"
                "lookback_hours: 168\n"
                "# Filter mode: 'relaxed' lets through saturated-headline theses\n"
                "# when the specific mechanism or instrument leg is still uncrowded.\n"
                'filter_mode: "relaxed"\n'
            )
        if slot.name == "inventory":
            return '{"theses": [], "run_history": [], "version": "0.1"}\n'
        if slot.name == "portfolio":
            return (
                "positions:\n"
                "  # - instrument: ETH-PERP\n"
                "  #   venue: hyperliquid\n"
                "  #   side: long\n"
                "  #   notional_usd: 10000\n"
                "instruments:\n"
                "  polymarket: true\n"
                "  perps: true\n"
                "max_notional_usd: 25000\n"
            )
        if slot.name == "watchlist":
            return (
                "watch:\n"
                "  # Themes to investigate or track specifically.\n"
                '  # - label: "EU fiscal rules enforcement"\n'
                "  #   domain: macro\n"
                "  #   keywords:\n"
                '  #     - "EU fiscal compact"\n'
                '  #     - "Italian sovereign debt"\n'
                "ignore:\n"
                "  # Known themes to exclude (already mainstream).\n"
                '  # - "Bitcoin ETF approval"\n'
            )
    if slot.file_type == "markdown":
        title = slot.name.replace("_", " ").title()
        return f"# {title}\n\nTODO: describe the {slot.name} input.\n"
    if slot.file_type == "yaml":
        return f'metadata:\n  slot: "{slot.name}"\n  status: draft\nvalues: []\n'
    return "{}\n"


def _slot_schema(slot: ArchetypeInputSlot, *, archetype: str) -> str:
    schema: dict[str, Any]
    if slot.file_type == "markdown":
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": slot.name,
            "type": "string",
            "contentMediaType": "text/markdown",
            "minLength": 1,
        }
        return json.dumps(schema, indent=2) + "\n"

    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": slot.name,
        "type": "object",
        "additionalProperties": True,
    }
    if archetype == "conditional-router" and slot.name == "mappings":
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "conditional-router-mappings",
            "type": "object",
            "required": ["conditions"],
            "properties": {
                "conditions": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "polymarket_search": {"type": "string"},
                            "proxies": {"type": "object"},
                        },
                        "required": ["polymarket_search"],
                    },
                }
            },
            "additionalProperties": False,
        }
    elif archetype == "conditional-router" and slot.name == "preferences":
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "conditional-router-preferences",
            "type": "object",
            "properties": {
                "execution_mode": {
                    "type": "string",
                    "enum": ["quote", "draft", "armed"],
                },
                "prefer_direct_market": {"type": "boolean"},
                "max_candidates": {"type": "integer", "minimum": 1},
                "allow_shorting": {"type": "boolean"},
            },
            "additionalProperties": True,
        }
    elif archetype == "hedge-finder" and slot.name == "assets":
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "hedge-finder-assets",
            "type": "object",
            "required": ["assets"],
            "properties": {
                "assets": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["symbol", "weight_usd"],
                        "properties": {
                            "symbol": {"type": "string"},
                            "weight_usd": {"type": "number", "exclusiveMinimum": 0},
                        },
                    },
                }
            },
            "additionalProperties": False,
        }
    elif archetype == "hedge-finder" and slot.name == "constraints":
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "hedge-finder-constraints",
            "type": "object",
            "properties": {
                "factors": {"type": "array", "items": {"type": "string"}},
                "max_hedges": {"type": "integer", "minimum": 1},
                "target_residual_beta": {"type": "number", "minimum": 0},
                "rebalance_band": {"type": "number", "minimum": 0},
                "max_leverage": {"type": "number", "minimum": 1},
                "margin_mode": {"type": "string"},
            },
            "additionalProperties": True,
        }
    elif archetype == "spread-radar" and slot.name == "universe":
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "spread-radar-universe",
            "type": "object",
            "properties": {
                "symbols": {"type": "array", "items": {"type": "string"}},
                "overrides": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": True,
        }
    elif archetype == "narrative-radar" and slot.name == "scan_config":
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "narrative-radar-scan-config",
            "type": "object",
            "required": ["domains"],
            "properties": {
                "domains": {
                    "type": "object",
                    "properties": {
                        "geopolitical": {"type": "object"},
                        "macro": {"type": "object"},
                        "regulatory": {"type": "object"},
                        "tech": {"type": "object"},
                        "structural": {"type": "object"},
                    },
                },
                "novelty_threshold": {"type": "number", "minimum": 0, "maximum": 1},
                "confidence_threshold": {"type": "number", "minimum": 0, "maximum": 1},
                "portfolio_confidence_threshold": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "max_theses_per_domain": {"type": "integer", "minimum": 1},
                "lookback_hours": {"type": "integer", "minimum": 1},
                "filter_mode": {
                    "type": "string",
                    "enum": ["relaxed", "strict"],
                },
            },
            "additionalProperties": True,
        }
    elif archetype == "narrative-radar" and slot.name == "inventory":
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "narrative-radar-inventory",
            "type": "object",
            "properties": {
                "theses": {"type": "array"},
                "run_history": {"type": "array"},
                "version": {"type": "string"},
            },
            "additionalProperties": True,
        }
    elif archetype == "narrative-radar" and slot.name == "portfolio":
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "narrative-radar-portfolio",
            "type": "object",
            "properties": {
                "positions": {"type": "array"},
                "instruments": {"type": "object"},
                "max_notional_usd": {"type": "number", "minimum": 0},
            },
            "additionalProperties": True,
        }
    elif archetype == "narrative-radar" and slot.name == "watchlist":
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "narrative-radar-watchlist",
            "type": "object",
            "properties": {
                "watch": {"type": "array"},
                "ignore": {"type": "array"},
            },
            "additionalProperties": True,
        }
    return json.dumps(schema, indent=2) + "\n"


def _pipeline_policy_template(archetype: str) -> str:
    if archetype == "conditional-router":
        return (
            "archetype: conditional-router\n\n"
            "signals:\n"
            "  recession_prob:\n"
            '    source: "polymarket"\n'
            '    query: "US recession"\n'
            '    field: "implied_probability"\n'
            "    liquidity_floor_usd: 25000\n"
            "    max_spread_cents: 3\n\n"
            "playbooks:\n"
            "  risk_off:\n"
            '    when: "recession_prob >= 0.60"\n'
            "    options:\n"
            '      - type: "hyperliquid_basket"\n'
            '        side: "sell"\n'
            '        symbols: ["SOL", "DOGE"]\n'
            "        size_pct: 0.20\n\n"
            "  crash_mode:\n"
            '    when: "recession_prob >= 0.80"\n'
            "    options:\n"
            '      - type: "hyperliquid_basket"\n'
            '        side: "short"\n'
            '        symbols: ["SOL", "DOGE", "XRP"]\n'
            "        leverage: 2\n"
            "        size_pct: 0.35\n\n"
            "  risk_on:\n"
            '    when: "recession_prob < 0.35"\n'
            "    options:\n"
            '      - type: "hyperliquid_basket"\n'
            '        side: "long"\n'
            '        symbols: ["ETH", "SOL"]\n'
            "        size_pct: 0.20\n\n"
            "null_state:\n"
            '  action: "hold"\n'
            "  require_score_above: 0.65\n\n"
            "risk:\n"
            "  max_notional_usd: 25000\n"
            "  max_leverage: 2\n"
            '  margin_mode: "isolated"\n'
            "  max_daily_loss_pct: 3\n"
            "  stop_loss_pct: 1.5\n"
            "  take_profit_pct: 4.0\n\n"
            "scheduler:\n"
            "  poll_seconds: 300\n"
            "  cooldown_seconds: 21600\n"
        )
    if archetype == "hedge-finder":
        return (
            "archetype: hedge-finder\n\n"
            "signals:\n"
            "  portfolio_series:\n"
            '    source: "delta_lab"\n'
            "    lookback_days: 60\n\n"
            "  hedge_universe:\n"
            '    source: "hyperliquid"\n'
            '    symbols: ["BTC", "ETH", "SOL", "HYPE"]\n\n'
            "decision:\n"
            '  objective: "minimize_residual_beta_net_cost"\n'
            '  factors: ["BTC", "ETH", "SOL"]\n'
            "  max_hedges: 3\n"
            "  target_residual_beta: 0.15\n\n"
            "null_state:\n"
            "  minimum_improvement: 0.10\n\n"
            "risk:\n"
            "  max_notional_usd: 30000\n"
            "  max_leverage: 2\n"
            '  margin_mode: "isolated"\n'
            "  stop_loss_pct: 1.5\n"
            "  take_profit_pct: 4.0\n"
            "  max_spread_bps: 20\n\n"
            "scheduler:\n"
            "  interval_seconds: 14400\n"
        )
    if archetype == "narrative-radar":
        return (
            "archetype: narrative-radar\n\n"
            "domains:\n"
            "  geopolitical:\n"
            "    source_protocol:\n"
            "      - category: think_tank_publications\n"
            "        search_patterns:\n"
            '          - "geopolitical risk analysis {year}"\n'
            '          - "conflict escalation assessment"\n'
            '          - "sanctions trajectory"\n'
            "      - category: diplomatic_signals\n"
            "        search_patterns:\n"
            '          - "military deployment exercises {region}"\n'
            '          - "diplomatic recall ambassador"\n'
            '          - "sanctions proposed draft"\n'
            "      - category: expert_commentary\n"
            "        search_patterns:\n"
            '          - "geopolitical risk outlook {year} {next_year}"\n'
            "\n"
            "  macro:\n"
            "    source_protocol:\n"
            "      - category: institutional_publications\n"
            "        search_patterns:\n"
            '          - "IMF Article IV consultation {year}"\n'
            '          - "BIS quarterly review"\n'
            '          - "sovereign debt sustainability"\n'
            "      - category: central_bank_communications\n"
            "        search_patterns:\n"
            '          - "central bank policy divergence"\n'
            '          - "fiscal deficit trajectory"\n'
            '          - "commodity supply constraint"\n'
            "\n"
            "  regulatory:\n"
            "    source_protocol:\n"
            "      - category: legislative_trackers\n"
            "        search_patterns:\n"
            '          - "crypto regulation bill committee"\n'
            '          - "stablecoin legislation"\n'
            '          - "AI governance regulation"\n'
            "      - category: enforcement_patterns\n"
            "        search_patterns:\n"
            '          - "SEC enforcement action crypto"\n'
            '          - "EU MiCA enforcement"\n'
            '          - "CFTC digital assets"\n'
            "\n"
            "  tech:\n"
            "    source_protocol:\n"
            "      - category: research_publications\n"
            "        search_patterns:\n"
            '          - "AI capability breakthrough {year}"\n'
            '          - "blockchain scalability milestone"\n'
            "      - category: industry_roadmaps\n"
            "        search_patterns:\n"
            '          - "technology deployment timeline"\n'
            '          - "infrastructure buildout"\n'
            "\n"
            "  structural:\n"
            "    source_protocol:\n"
            "      - category: long_cycle_research\n"
            "        search_patterns:\n"
            '          - "energy transition milestone {year}"\n'
            '          - "supply chain restructuring"\n'
            '          - "demographic shift economic impact"\n'
            "\n"
            "verification_protocol:\n"
            "  min_tool_calls_per_thesis: 3\n"
            "  require_evidence_source_url: true\n"
            "  require_currency_check: true\n"
            "  recency_window_days: 30\n"
            "  min_recent_evidence_per_thesis: 1\n"
            "  drop_if_catalyst_already_fired: true\n"
            "\n"
            "novelty_gate:\n"
            "  mainstream_article_threshold: 40\n"
            "  polymarket_volume_threshold_usd: 1000000\n"
            "  bank_research_note_threshold: 3\n"
            "  search_targets:\n"
            '    - "bloomberg.com"\n'
            '    - "reuters.com"\n'
            '    - "ft.com"\n'
            '    - "wsj.com"\n'
            '    - "cnbc.com"\n'
            "  # Relaxed-mode pass criterion: even when headline coverage is\n"
            "  # saturated, a thesis PASSES novelty if the specific mechanism\n"
            "  # or instrument leg is still uncrowded (zero Polymarket volume\n"
            "  # + no published sell-side note on the specific angle).\n"
            "  relaxed_pass_on_angle: true\n"
            "\n"
            "adversarial:\n"
            "  pre_mortem:\n"
            '    prompt: "Assume this thesis is wrong. What is the single most likely reason?"\n'
            "  consensus_audit:\n"
            '    prompt: "Find the strongest published argument against this thesis."\n'
            "  historical_analog:\n"
            '    prompt: "Find the closest historical parallel. Did the risk materialize or fade?"\n'
            "\n"
            "confidence:\n"
            "  initial_score: 0.40\n"
            "  supporting_evidence_delta: 0.08\n"
            "  contrary_evidence_delta: 0.10\n"
            "  precondition_met_delta: 0.10\n"
            "  precondition_invalidated_delta: 0.20\n"
            "  catalyst_occurred_delta: 0.15\n"
            "  catalyst_missed_delta: 0.15\n"
            "  staleness_decay_per_run: 0.02\n"
            "  dormant_threshold: 0.20\n"
            "  escalating_threshold: 0.75\n"
            "\n"
            "portfolio_strategy:\n"
            "  min_confidence: 0.35\n"
            "  max_timeline_months: 12\n"
            "  instruments:\n"
            "    polymarket: true\n"
            "    perps: true\n"
            "  max_notional_usd: 25000\n"
            "\n"
            "null_state:\n"
            '  action: "hold"\n'
            "  require_thesis_count_above: 0\n"
            "  require_avg_confidence_above: 0.40\n"
        )
    return (
        "archetype: spread-radar\n\n"
        "universe:\n"
        '  source: "input_or_default"\n'
        '  default_symbols: ["ETH", "SOL", "SUI", "AVAX", "HYPE"]\n\n'
        "features:\n"
        "  returns_7d:\n"
        '    source: "delta_lab"\n\n'
        "  funding_7d:\n"
        '    source: "hyperliquid"\n\n'
        "clustering:\n"
        '  method: "correlation_plus_funding"\n'
        "  lookback_days: 30\n\n"
        "candidate_rules:\n"
        "  min_zscore: 2.0\n\n"
        "scoring:\n"
        "  weights:\n"
        "    dislocation: 0.35\n"
        "    funding: 0.20\n"
        "    liquidity: 0.20\n"
        "    catalyst: 0.15\n"
        "    simplicity: 0.10\n\n"
        "null_state:\n"
        "  require_score_above: 0.65\n"
    )


def _pipeline_graph_text(archetype: str) -> str:
    graph = default_pipeline_graph(archetype)
    lines = ["nodes:"]
    for node in graph.nodes:
        lines.append(f'  - id: "{node}"')
    lines.append("")
    lines.append("edges:")
    for edge in graph.edges:
        lines.append(f'  - from: "{edge.source}"')
        lines.append(f'    to: "{edge.target}"')
    lines.append("")
    lines.append("failure_edges:")
    for edge in graph.failure_edges:
        lines.append(f'  - from: "{edge.source}"')
        lines.append(f'    on: "{edge.event}"')
        lines.append(f'    to: "{edge.target}"')
        if edge.max_retries is not None:
            lines.append(f"    max_retries: {edge.max_retries}")
    lines.append("")
    return "\n".join(lines)


def _pipeline_instructions(slug: str, archetype: str) -> str:
    if archetype == "conditional-router":
        return (
            f"# {humanize_slug(slug)}\n\n"
            "Use this skill when the user describes a conditional macro, political, "
            "or thematic thesis and wants it converted into monitorable trades and a job.\n\n"
            "Read `references/pipeline.md`, `references/signals.md`, and `references/risk.md` before starting.\n\n"
            "Execution order:\n"
            "1. Spawn `thesis-normalizer`, `poly-scout`, `proxy-mapper`, and `qual-researcher` in parallel.\n"
            "2. Synthesize candidate expressions from their artifacts.\n"
            "3. Run `null-skeptic`, then `risk-verifier`, then `job-compiler`.\n\n"
            "Rules:\n"
            "1. You are the only orchestrator.\n"
            "2. Workers are leaf agents and must not spawn more agents.\n"
            "3. Every worker writes exactly one artifact under `.wf-artifacts/$RUN_ID/`.\n"
            "4. Never skip the null state, even when a thesis looks strong.\n"
            "5. If market quality is weak or risk validation fails, degrade to `draft` or `null`.\n"
            "6. The final output must contain:\n"
            "   - `signal_snapshot`\n"
            "   - `selected_playbook`\n"
            "   - `candidate_expressions`\n"
            "   - `null_state`\n"
            "   - `risk_checks`\n"
            "   - `job`\n"
            "   - `next_invalidation`\n"
        )
    if archetype == "hedge-finder":
        return (
            f"# {humanize_slug(slug)}\n\n"
            "Use this skill when the user wants to hedge a multi-asset portfolio with "
            "perpetuals or other available SDK surfaces while preserving one run "
            "snapshot for console and applet output.\n\n"
            "Read `references/pipeline.md`, `references/signals.md`, and "
            "`references/risk.md` before starting.\n\n"
            "Execution order:\n"
            "1. Load `inputs/assets.yaml`, `inputs/constraints.yaml`, and "
            "`policy/default.yaml`.\n"
            "2. Run `exposure-reader`, `beta-modeler`, and `hedge-searcher`, "
            "complete the optimizer phase from their artifacts, then run "
            "`skeptic` -> `risk-verifier` -> `job-compiler` -> "
            "`display-composer`.\n"
            "3. Treat `.wf-artifacts/$RUN_ID/display.json` as the display "
            "snapshot for both console summaries and applet dashboards.\n"
            "4. Surface the same funding rates, notionals, leverage, and "
            "market-quality values from `display.json`; do not recompute or "
            "browser-refetch those values in the final answer.\n\n"
            "Rules:\n"
            "1. You are the only orchestrator.\n"
            "2. Workers are leaf agents and must not spawn more agents.\n"
            "3. Every worker writes exactly one artifact under `.wf-artifacts/$RUN_ID/`.\n"
            "4. Never skip the null state, even when a hedge looks strong.\n"
            "5. If market quality is weak or risk validation fails, degrade to "
            "`draft` or `null`.\n"
            "6. The final output must contain:\n"
            "   - `signal_snapshot`\n"
            "   - `selected_playbook`\n"
            "   - `candidate_expressions`\n"
            "   - `null_state`\n"
            "   - `risk_checks`\n"
            "   - `job`\n"
            "   - `next_invalidation`\n"
            "   - `display` — path to `.wf-artifacts/$RUN_ID/display.json`, "
            "the authoritative rendered snapshot for this run\n"
        )
    if archetype == "narrative-radar":
        return (
            f"# {humanize_slug(slug)}\n\n"
            "Use this skill when the user wants to discover emerging macro narratives that could "
            "become dominant market drivers but are not yet mainstream. This pipeline identifies "
            "structural risks and theses across geopolitics, macro, regulation, technology, and "
            "structural shifts, then validates them adversarially and maps surviving theses to "
            "tradeable instruments.\n\n"
            "Read `references/pipeline.md`, `references/signals.md`, and `references/risk.md` before starting.\n\n"
            "Execution order:\n"
            "1. Load `inputs/scan_config.yaml` and `inputs/inventory.json` (previous state if recurring run).\n"
            "2. Spawn all five domain scan agents in parallel:\n"
            "   `geopolitical-analyst`, `macro-strategist`, `regulatory-tracker`, `tech-scout`, `structural-analyst`.\n"
            "3. Run `thesis-synthesizer` to merge domain outputs with existing inventory.\n"
            "4. Run the adversarial chain in sequence:\n"
            "   `novelty-gate` → `pre-mortem-analyst` → `consensus-auditor` → `historical-analogist`.\n"
            "5. Run `portfolio-strategist` only on theses that survived all adversarial gates.\n"
            "6. Run `inventory-compiler` to persist updated inventory AND emit `trade_book.md`.\n"
            "7. Run `display-composer` to transform the compiled inventory into a display-ready JSON bundle for the applet dashboard.\n"
            "8. **Primary user-facing outputs are `trade_book.md` and `display.json`** — always surface both after the run completes.\n\n"
            "Rules:\n"
            "1. You are the only orchestrator.\n"
            "2. Workers are leaf agents and must not spawn more agents.\n"
            "3. Every worker writes exactly one artifact under `.wf-artifacts/$RUN_ID/`.\n"
            "4. The adversarial chain is mandatory — never skip the novelty gate or skeptic phases.\n"
            "5. Every thesis must have a specific, falsifiable prediction with a deadline.\n"
            "6. If this is a recurring run, existing theses get confidence updates, not re-generation.\n"
            "7. Theses that are already mainstream (high media coverage, large Polymarket volume) must be killed (unless relaxed filter mode rescues a specific uncrowded leg).\n"
            "8. The final output must contain:\n"
            "   - `signal_snapshot`\n"
            "   - `selected_playbook`\n"
            "   - `candidate_expressions`\n"
            "   - `null_state`\n"
            "   - `risk_checks`\n"
            "   - `job`\n"
            "   - `next_invalidation`\n"
            "   - `trade_book` — path to the markdown trade book (primary human-readable artifact)\n"
        )
    return (
        f"# {humanize_slug(slug)}\n\n"
        f"Use this skill to orchestrate the `{archetype}` pipeline.\n\n"
        "Read `references/pipeline.md`, `references/signals.md`, and `references/risk.md` before starting.\n\n"
        "Rules:\n"
        "1. You are the orchestrator.\n"
        "2. Use the declared worker agents for analysis fan-out.\n"
        "3. Workers are leaf agents and must not spawn more agents.\n"
        "3. Every worker writes exactly one artifact under `.wf-artifacts/$RUN_ID/`.\n"
        "4. Always evaluate the null state before job compilation.\n"
        "5. If risk validation fails, downgrade to `draft` or `null`.\n"
        "6. Final output must match the declared output contract.\n"
    )


def _pipeline_reference_pipeline(archetype: str) -> str:
    if archetype == "conditional-router":
        return (
            "# Pipeline\n\n"
            "This path compiles a conditional trade thesis into a fixed, phase-ordered workflow.\n\n"
            "Ordered phases:\n"
            "1. `intake`\n"
            "2. `normalize_thesis`\n"
            "3. parallel fan-out: `market_research`, `proxy_mapping`, `qual_research`\n"
            "4. `synthesize`\n"
            "5. `skeptic`\n"
            "6. `risk_gate`\n"
            "7. `compile_job`\n"
            "8. `finalize`\n\n"
            "Failure policy:\n"
            "- retry `market_research` once on retryable errors\n"
            "- if market research is exhausted, continue into skeptic with partial inputs\n"
            "- if `risk_gate` fails, stop at `draft` or `null`\n"
            "- if `compile_job` fails, stop without arming the job\n\n"
            "Artifact rule:\n"
            "- every worker owns exactly one JSON artifact under `.wf-artifacts/$RUN_ID/`\n"
            "- the orchestrator reads artifacts and owns final synthesis\n"
        )
    if archetype == "hedge-finder":
        return (
            "# Pipeline\n\n"
            "This path finds hedge candidates for a multi-asset portfolio and "
            "publishes one run snapshot for both console and applet views.\n\n"
            "Ordered phases:\n"
            "1. `intake` — load assets, constraints, and policy\n"
            "2. `exposure_reader` — resolve symbols and build the portfolio series\n"
            "3. `beta_modeler` — estimate factor betas and residual exposures\n"
            "4. `hedge_search` — collect candidates with funding, spread, and liquidity context\n"
            "5. `optimizer` — select hedge weights against residual beta and net cost\n"
            "6. `skeptic` — compare selected hedges against the null state\n"
            "7. `risk_gate` — apply notional, leverage, and execution protections\n"
            "8. `compile_job` — compile a draft or armed rebalance job\n"
            "9. `display_compose` — write `display.json`, the display snapshot "
            "used by applets and console output\n"
            "10. `finalize` — emit the standard response envelope including a "
            "`display` pointer\n\n"
            "Artifact rule:\n"
            "- every worker owns exactly one JSON artifact under `.wf-artifacts/$RUN_ID/`\n"
            "- `display.json` is the authoritative rendered snapshot for user-facing "
            "funding rates, notionals, leverage, and hedge metrics\n"
            "- if an applet intentionally performs a browser-side live refresh, it "
            "must label the live timestamp separately from the run snapshot\n"
        )
    if archetype == "narrative-radar":
        return (
            "# Pipeline\n\n"
            "This path discovers emerging macro narratives and maps them to tradeable instruments.\n\n"
            "Ordered phases:\n"
            "1. `intake` — load scan config and previous inventory\n"
            "2. parallel fan-out: `geopolitical_scan`, `macro_scan`, `regulatory_scan`, `tech_scan`, `structural_scan`\n"
            "3. `thesis_synthesis` — merge domain outputs, deduplicate, update existing inventory\n"
            "4. `novelty_gate` — kill theses that are already mainstream\n"
            "5. `pre_mortem` — assume each thesis is wrong, find the most likely failure mode\n"
            "6. `consensus_audit` — find the strongest counter-argument for each thesis\n"
            "7. `historical_analog` — find historical parallels and base rates\n"
            "8. `portfolio_strategy` — map surviving theses to instruments and trade structures\n"
            "9. `compile_inventory` — persist updated inventory for next run AND write `trade_book.md` as the primary human-readable artifact (summary table + one short section per retained trade)\n"
            "10. `display_compose` — transform inventory into a display-ready JSON bundle with plain-English summaries, tags, and layout hints for the applet\n"
            "11. `finalize` — emit the standard response envelope including `trade_book` pointer\n\n"
            "Failure policy:\n"
            "- retry any domain scan once on retryable errors\n"
            "- if novelty gate kills all theses, skip to finalize with null state\n"
            "- if adversarial chain rejects all theses, compile inventory with no portfolio actions\n"
            "- if no instruments found, compile inventory without trade structures\n\n"
            "Artifact rule:\n"
            "- every worker owns exactly one JSON artifact under `.wf-artifacts/$RUN_ID/`\n"
            "- the orchestrator reads artifacts and owns final synthesis\n\n"
            "State persistence:\n"
            "- `inputs/inventory.json` carries the thesis inventory across runs\n"
            "- each run updates confidence scores based on new evidence\n"
            "- `compile_inventory` writes the updated inventory back\n"
        )
    return (
        "# Pipeline\n\n"
        f"This path uses the `{archetype}` archetype.\n\n"
        "Execution model:\n"
        "- graph-defined normal edges for the happy path\n"
        "- failure edges for retries, fallback, and downgrade behavior\n"
        "- one artifact per worker under `.wf-artifacts/$RUN_ID/`\n"
        "- explicit null-state and risk gates before any armed job is produced\n"
    )


def _pipeline_reference_signals() -> str:
    return (
        "# Signals\n\n"
        "Every pipeline output must use the standard response envelope:\n"
        "- `signal_snapshot`\n"
        "- `selected_playbook`\n"
        "- `candidate_expressions`\n"
        "- `null_state`\n"
        "- `risk_checks`\n"
        "- `job`\n"
        "- `next_invalidation`\n\n"
        "Operational signal vocabulary:\n"
        "- `armed`\n"
        "- `entered`\n"
        "- `exited`\n"
        "- `paused`\n"
        "- `null-state-selected`\n"
        "- `error`\n"
    )


def _pipeline_reference_risk() -> str:
    return (
        "# Risk\n\n"
        "Every live-action path must define explicit limits, invalidation logic, and a draft fallback.\n"
        "Always rank a null-state lane before arming any job.\n"
        "Do not arm a path unless market quality checks and the risk block both pass.\n"
        "If an action path cannot satisfy the risk block, the compiler should return `draft` or `null`, not `armed`.\n"
    )


def _pipeline_reference_examples(slug: str) -> str:
    return (
        "# Examples\n\n"
        "Useful commands:\n"
        f"- `poetry run wayfinder path doctor --path examples/paths/{slug}`\n"
        f"- `poetry run wayfinder path eval --path examples/paths/{slug}`\n"
        f"- `poetry run wayfinder path render-skill --path examples/paths/{slug}`\n\n"
        "Use the fixtures to validate output shape, null-state selection, risk-gate behavior, and host render coverage.\n"
    )


def _pipeline_agent_body(agent: ArchetypeAgent, *, archetype: str) -> str:
    if archetype == "conditional-router":
        instructions: dict[str, tuple[list[str], list[str], list[str]]] = {
            "thesis-normalizer": (
                [
                    "`inputs/thesis.md`",
                    "`inputs/mappings.yaml` when present",
                    "`policy/default.yaml`",
                ],
                [
                    "`signal_id` and threshold ladder",
                    "`time_horizon` and invalidation conditions",
                    "`unsupported_assumptions` that need validation",
                ],
                [
                    "Do not query live markets.",
                    "Do not rank or reject trades.",
                ],
            ),
            "poly-scout": (
                [
                    "the normalized thesis artifact",
                    "`policy/default.yaml`",
                ],
                [
                    "candidate market title and condition id",
                    "implied probability, spread, and liquidity score",
                    "history quality, rule clarity, and rejection reasons",
                ],
                [
                    "Reject markets that fail liquidity or spread checks.",
                    "Do not compile jobs or proxy trades.",
                ],
            ),
            "proxy-mapper": (
                [
                    "the normalized thesis artifact",
                    "`inputs/mappings.yaml`",
                    "`policy/default.yaml`",
                ],
                [
                    "direct, proxy, and relative-value expressions",
                    "expression sizing hints from the policy playbooks",
                    "dependencies on signals or market availability",
                ],
                [
                    "Do not score market quality.",
                    "Do not skip the null-state lane.",
                ],
            ),
            "qual-researcher": (
                [
                    "the normalized thesis artifact",
                    "`inputs/thesis.md`",
                    "user notes when present",
                ],
                [
                    "supporting catalysts and invalidation risks",
                    "assumptions that remain unverified",
                    "context that changes sizing confidence",
                ],
                [
                    "Prefer user-supplied material over broad web research.",
                    "Do not make execution decisions.",
                ],
            ),
            "null-skeptic": (
                [
                    "all candidate artifacts produced so far",
                    "`policy/default.yaml`",
                ],
                [
                    "ranked candidate list against the null state",
                    "clear veto reasons for weak or forced trades",
                    "the selected playbook or explicit null-state decision",
                ],
                [
                    "Always include a do-nothing lane.",
                    "Reject candidates that do not clear the null-state threshold.",
                ],
            ),
            "risk-verifier": (
                [
                    "the skeptic artifact",
                    "`policy/default.yaml`",
                    "`inputs/preferences.yaml` when present",
                ],
                [
                    "leverage, notional, and market-quality checks",
                    "the final execution mode: `armed`, `draft`, or `null`",
                    "downgrade reasons when policy limits are exceeded",
                ],
                [
                    "Do not increase risk to force an armed result.",
                    "Draft mode is preferred over live action when uncertain.",
                ],
            ),
            "job-compiler": (
                [
                    "the risk gate artifact",
                    "`policy/default.yaml`",
                ],
                [
                    "a runner-compatible job payload",
                    "poll interval, cooldown, and entry signal names",
                    "the exact mode approved by the risk gate",
                ],
                [
                    "Do not arm the job if the risk gate returned `draft` or `null`.",
                    "Write the final artifact only after validation passes.",
                ],
            ),
        }
        read_items, write_items, rules = instructions.get(
            agent.agent_id,
            ([], [], ["Stay inside your assigned phase."]),
        )
        lines = [f"# {agent.agent_id}", "", agent.description, "", "Read:"]
        lines.extend(
            [f"- {item}" for item in read_items]
            or ["- only the inputs required for your phase"]
        )
        lines.extend(
            [
                "",
                "Write:",
                f"- exactly one JSON object to `{DEFAULT_ARTIFACTS_DIR}/$RUN_ID/{agent.output_name}`",
            ]
        )
        lines.extend([f"- include {item}" for item in write_items])
        lines.extend(
            [
                "",
                "Rules:",
                "- Do not spawn other agents.",
                "- Do not compile the final answer.",
            ]
        )
        lines.extend([f"- {item}" for item in rules])
        return "\n".join(lines) + "\n"
    if archetype == "hedge-finder":
        instructions: dict[str, tuple[list[str], list[str], list[str]]] = {
            "exposure-reader": (
                [
                    "`inputs/assets.yaml` — portfolio symbols and weights",
                    "`policy/default.yaml` — portfolio series signal config",
                ],
                [
                    "resolved asset symbols and weights",
                    "the portfolio time series used for all later calculations",
                    "data freshness timestamps for every fetched source",
                ],
                [
                    "Do not estimate hedges or rank candidates.",
                    "Record source timestamps so display output can cite the run snapshot.",
                ],
            ),
            "beta-modeler": (
                [
                    "the exposure-reader artifact",
                    "`inputs/constraints.yaml` — factor universe and residual beta target",
                    "`policy/default.yaml` — lookback and decision settings",
                ],
                [
                    "portfolio factor betas and residual exposures",
                    "stability diagnostics for the lookback window",
                    "factor series timestamps reused from upstream artifacts",
                ],
                [
                    "Do not fetch fresh browser/public data.",
                    "Use the upstream run artifact as the only portfolio snapshot.",
                ],
            ),
            "hedge-searcher": (
                [
                    "the beta-modeler artifact",
                    "`inputs/constraints.yaml` — hedge limits",
                    "`policy/default.yaml` — hedge universe and market-quality rules",
                ],
                [
                    "candidate hedges with funding rates, spreads, liquidity, and timestamps",
                    "annualized funding values derived from the captured run data",
                    "rejection reasons for candidates that fail market-quality checks",
                ],
                [
                    "Do not mix live browser-fetched values into the run artifact.",
                    "For each funding rate, include the raw period rate, annualized rate, "
                    "and provider timestamp used in this run.",
                ],
            ),
            "skeptic": (
                [
                    "the hedge-searcher artifact",
                    "the optimizer phase result if present",
                    "`policy/default.yaml` — null-state rules",
                ],
                [
                    "selected hedge versus null-state comparison",
                    "materiality checks and veto reasons",
                    "the exact candidate metrics accepted for risk verification",
                ],
                [
                    "Never force a hedge if the improvement is not material.",
                    "Carry forward the exact funding, notional, and leverage inputs "
                    "from upstream artifacts.",
                ],
            ),
            "risk-verifier": (
                [
                    "the skeptic artifact",
                    "`inputs/constraints.yaml`",
                    "`policy/default.yaml` — risk limits",
                ],
                [
                    "notional, leverage, margin, spread, and liquidity checks",
                    "the final execution mode: `armed`, `draft`, or `null`",
                    "downgrade or rejection reasons when limits are exceeded",
                ],
                [
                    "Do not increase leverage or notional to force an armed result.",
                    "Use the same candidate metrics that will be shown in display output.",
                ],
            ),
            "job-compiler": (
                [
                    "the risk-verifier artifact",
                    "`policy/default.yaml` — scheduler and execution settings",
                ],
                [
                    "a runner-compatible draft or armed rebalance job",
                    "poll interval, cooldown, and invalidation logic",
                    "the exact mode approved by the risk gate",
                ],
                [
                    "Do not arm the job if the risk gate returned `draft` or `null`.",
                    "Write the job artifact before display composition.",
                ],
            ),
            "display-composer": (
                [
                    "all run artifacts written under `.wf-artifacts/$RUN_ID/`",
                    "especially `exposure_reader.json`, `beta_modeler.json`, "
                    "`hedge_search.json`, `risk_gate.json`, and `job.json`",
                ],
                [
                    "a `run_meta` object with `run_id`, `generated_at`, and "
                    "`source: \"run_snapshot\"`",
                    "a `data_freshness` object with the source/provider timestamps "
                    "used by the run",
                    "display-ready hedge rows with funding rates, notionals, leverage, "
                    "residual beta, spread, liquidity, and risk status",
                    "a `cli_summary` object using the same values the applet will render",
                ],
                [
                    "This is a presentation transform only; do not fetch or recompute "
                    "market data.",
                    "Every displayed funding rate, notional, and leverage value must "
                    "come from the existing run artifacts.",
                    "If an applet intentionally live-refetches later, label it separately "
                    "as live data and include its timestamp; do not overwrite this "
                    "run snapshot.",
                ],
            ),
        }
        read_items, write_items, rules = instructions.get(
            agent.agent_id,
            ([], [], ["Stay inside your assigned phase."]),
        )
        lines = [f"# {agent.agent_id}", "", agent.description, "", "Read:"]
        lines.extend(
            [f"- {item}" for item in read_items]
            or ["- only the inputs required for your phase"]
        )
        lines.extend(
            [
                "",
                "Write:",
                f"- exactly one JSON object to `{DEFAULT_ARTIFACTS_DIR}/$RUN_ID/{agent.output_name}`",
            ]
        )
        lines.extend([f"- include {item}" for item in write_items])
        lines.extend(
            [
                "",
                "Rules:",
                "- Do not spawn other agents.",
                "- Do not compile the final answer.",
            ]
        )
        lines.extend([f"- {item}" for item in rules])
        return "\n".join(lines) + "\n"
    if archetype == "narrative-radar":
        domain_scan_candidate_schema = (
            "an array of `candidate_theses`, each with: `thesis_id`, `label`, "
            "`domain: {domain}`, `mechanism` (causal chain), `preconditions` "
            "(list with met/unmet status), `evidence` (list — EACH entry MUST "
            "contain `source_url` with a real https:// URL returned by "
            "WebSearch/WebFetch, `source_name`, `quality` (high/medium/low), "
            "`date` in YYYY-MM-DD, `summary`), `catalysts` (list with `event` "
            "and `estimated_date` strictly in the future), `timeline_months`, "
            "`initial_confidence`, `currency_check` (object: `searched_for`, "
            "`already_happened` bool, `evidence_url`, `last_updated`), "
            "`verification_queries` (array of `{query, url, tool}` records, "
            "length >= 3), `executability` (object: `tier` = `A`|`B`|`reject`, "
            "`primary_leg` = {surface in "
            "[swap,perp,lending,vault,lp,pendle,contract,polymarket,ccxt], "
            "instrument (concrete symbol/contract/market_slug), venue "
            "(hyperliquid|binance|polymarket|base|ethereum|arbitrum|...), "
            "liquidity_check (quantified volume/OI/depth)}, `proxy_basis` = "
            "null for Tier A, or named crypto proxy + beta justification for Tier B)"
        )
        domain_scan_verification_rules = [
            "VERIFICATION PROTOCOL (mandatory — a thesis that fails any check "
            "MUST be dropped, not downgraded):",
            "1. Currency check: BEFORE writing any candidate thesis, run at "
            'least one WebSearch for `"<event label>" <current year>` to '
            "confirm the catalyst has NOT already fired, been cancelled, or "
            "been superseded. Record that search in `currency_check` and drop "
            "the thesis if `already_happened` is true.",
            "2. URL requirement: every `evidence[]` entry MUST carry "
            "`source_url` containing a real, accessible https:// URL. "
            "Human-readable source labels without URLs are disallowed and "
            "will be rejected by the downstream schema check.",
            "3. Recency: at least one `evidence[]` entry per thesis MUST have "
            "a `date` within the last 30 days relative to today AND a "
            "verifiable `source_url`.",
            "4. Tool-call floor: issue at least 3 WebSearch/WebFetch calls "
            "per candidate thesis BEFORE writing it. Your total tool_uses "
            "count must be >= 3 * number_of_theses. Self-check before "
            "returning — if the floor is not met, keep searching.",
            "5. Already-happened skepticism: for each candidate, explicitly "
            'ask yourself "has this catalyst already fired, been cancelled, '
            'or been superseded?" Answer in `currency_check.already_happened` '
            "and cite the source that confirms the answer. Any `true` here "
            "means DROP the thesis.",
            "6. Executability check: every thesis MUST declare `executability` "
            "with a concrete `primary_leg.surface` from the 10 SDK surfaces "
            "(swap/perp/lending/vault/lp/pendle/contract/polymarket/ccxt) and "
            "a specific listed instrument with quantified liquidity. Tier A = "
            "directly monetizable via an SDK surface; Tier B = crypto-adjacent "
            "proxy with declared beta; tier = `reject` if the thesis cannot be "
            "monetized with this SDK — DROP the thesis. Traditional equities, "
            "bonds, CDS, FX options via non-crypto brokers, listed equity "
            "options, commodity futures without a tokenized/perp analog, and "
            "private company exposure are all NOT executable and must be "
            "rejected at this step.",
        ]
        nr_instructions: dict[str, tuple[list[str], list[str], list[str]]] = {
            "geopolitical-analyst": (
                [
                    "`inputs/scan_config.yaml` — domain config and focus regions",
                    "`inputs/inventory.json` — existing theses in geopolitical domain (if recurring run)",
                    "`inputs/watchlist.yaml` — user-specified themes to investigate or ignore",
                    "`policy/default.yaml` — source protocol, search patterns, and `verification_protocol` thresholds",
                ],
                [
                    domain_scan_candidate_schema.replace("{domain}", "geopolitical"),
                    "an array of `evidence_updates` for existing theses: `thesis_id`, `evidence_type` (supporting/contrary), `quality` (high/medium/low), `source_url`, `source_name`, `summary`",
                    "an array of `retirement_recommendations` for theses no longer relevant",
                    "a `falsifiable_prediction` for each new thesis — specific, testable, with a deadline",
                ],
                [
                    "Follow the source protocol in policy — check think tanks (CSIS, Brookings, CFR, IISS, Chatham House, RAND), diplomatic signals, and expert commentary.",
                    "Use websearch to find long-form analysis, then webfetch to read the actual content.",
                    "Focus on structural forces building pressure, not breaking news or viral stories.",
                    "Every thesis must have a clear causal mechanism from trigger to market impact.",
                    "Rate evidence quality: high = primary source or peer-reviewed, medium = reputable journalism or expert opinion, low = commentary or speculation.",
                    "Do not generate theses about risks everyone already knows unless you have specific new evidence that the timeline is compressing.",
                    *domain_scan_verification_rules,
                ],
            ),
            "macro-strategist": (
                [
                    "`inputs/scan_config.yaml` — domain config and focus areas",
                    "`inputs/inventory.json` — existing theses in macro domain",
                    "`inputs/watchlist.yaml` — user-specified themes",
                    "`policy/default.yaml` — source protocol and `verification_protocol` thresholds",
                ],
                [
                    domain_scan_candidate_schema.replace("{domain}", "macro"),
                    "`evidence_updates` for existing macro theses",
                    "`retirement_recommendations`",
                    "`falsifiable_prediction` for each new thesis",
                ],
                [
                    "Follow the source protocol — check IMF, BIS, World Bank, central bank speeches and minutes, sovereign debt monitors.",
                    "Focus on fiscal trajectories, monetary policy divergence, trade flow shifts, and commodity supply/demand imbalances.",
                    "Prioritize structural risks over cyclical fluctuations — look for forces that could persist for quarters or years.",
                    "Quantify where possible: debt-to-GDP trajectories, rate differentials, commodity inventory levels.",
                    "The transmission mechanism from macro force to crypto/market impact must be explicit.",
                    *domain_scan_verification_rules,
                ],
            ),
            "regulatory-tracker": (
                [
                    "`inputs/scan_config.yaml` — domain config and focus areas",
                    "`inputs/inventory.json` — existing theses in regulatory domain",
                    "`inputs/watchlist.yaml` — user-specified themes",
                    "`policy/default.yaml` — source protocol and `verification_protocol` thresholds",
                ],
                [
                    domain_scan_candidate_schema.replace("{domain}", "regulatory"),
                    "`evidence_updates` for existing regulatory theses",
                    "`retirement_recommendations`",
                    "`falsifiable_prediction` for each new thesis",
                ],
                [
                    "Follow the source protocol — check government legislative trackers, regulatory body consultation papers, enforcement action databases.",
                    "Track the pipeline: proposed → committee → floor vote → signed → enforcement. Note which stage each regulatory action is at.",
                    "Look for enforcement pattern shifts that telegraph future action before it's announced.",
                    "International coordination is a strong signal — when multiple jurisdictions move on the same issue, it accelerates.",
                    "Include specific bill numbers, consultation paper references, or enforcement case IDs as evidence.",
                    *domain_scan_verification_rules,
                ],
            ),
            "tech-scout": (
                [
                    "`inputs/scan_config.yaml` — domain config and focus areas",
                    "`inputs/inventory.json` — existing theses in tech domain",
                    "`inputs/watchlist.yaml` — user-specified themes",
                    "`policy/default.yaml` — source protocol and `verification_protocol` thresholds",
                ],
                [
                    domain_scan_candidate_schema.replace("{domain}", "tech"),
                    "`evidence_updates` for existing tech theses",
                    "`retirement_recommendations`",
                    "`falsifiable_prediction` for each new thesis",
                ],
                [
                    "Follow the source protocol — check research publications (arXiv, industry labs), company roadmaps, patent filings, infrastructure investment announcements.",
                    "Focus on capability thresholds that are 6-18 months from crossing — not what's already deployed, but what's about to be.",
                    "Distinguish between hype cycles and genuine inflection points by looking at deployment timelines and infrastructure investment.",
                    "The market impact mechanism should be specific: which sector, which instruments, which direction.",
                    *domain_scan_verification_rules,
                ],
            ),
            "structural-analyst": (
                [
                    "`inputs/scan_config.yaml` — domain config and focus areas",
                    "`inputs/inventory.json` — existing theses in structural domain",
                    "`inputs/watchlist.yaml` — user-specified themes",
                    "`policy/default.yaml` — source protocol and `verification_protocol` thresholds",
                ],
                [
                    domain_scan_candidate_schema.replace("{domain}", "structural"),
                    "`evidence_updates` for existing structural theses",
                    "`retirement_recommendations`",
                    "`falsifiable_prediction` for each new thesis",
                ],
                [
                    "Follow the source protocol — check demographic data providers, energy transition trackers, trade flow databases, infrastructure investment reports.",
                    "These are the slowest-moving theses — multi-year trends. Focus on identifying inflection points where gradual change becomes sudden.",
                    "Look for tipping points: policy deadlines, infrastructure completions, demographic milestones that force markets to reprice.",
                    "Quantify the structural shift where possible: percentage of energy mix, trade flow volumes, population ratios.",
                    *domain_scan_verification_rules,
                ],
            ),
            "thesis-synthesizer": (
                [
                    "all five domain scan artifacts",
                    "`inputs/inventory.json` — the existing thesis inventory",
                    "`policy/default.yaml` — confidence update rules",
                ],
                [
                    "a merged `theses` array combining new candidates with updated existing theses",
                    "`cross_domain_reinforcement` — theses that appear independently in multiple domains (flag these as higher confidence)",
                    "`confidence_deltas` — for each existing thesis, the confidence change and reason",
                    "deduplication notes — which candidates were merged and why",
                ],
                [
                    "Deduplicate aggressively — if two domain agents found the same structural risk, merge into one thesis with evidence from both.",
                    "Apply the confidence update rules from policy: supporting evidence increases, contrary evidence decreases, no new evidence decays.",
                    "Cross-domain reinforcement is a strong signal — a thesis identified independently by geopolitical AND macro agents deserves a confidence boost.",
                    "Do not generate new theses — only merge and score what the domain agents produced.",
                ],
            ),
            "novelty-gate": (
                [
                    "the thesis synthesis artifact",
                    "`policy/default.yaml` — novelty gate thresholds",
                ],
                [
                    "a `surviving_theses` array — theses that passed the novelty check",
                    "a `killed_theses` array — theses killed with the reason (mainstream coverage count, Polymarket volume, etc.)",
                    "for each surviving thesis, a `novelty_score` (0-1) based on inverse mainstream saturation",
                ],
                [
                    "For each thesis, search major financial media (Bloomberg, Reuters, FT, WSJ, CNBC) for articles about the thesis topic in the last 30 days. Count the results.",
                    "Search Polymarket for related prediction markets. Check volume and liquidity.",
                    "Use Alpha Lab to check if the topic has high-score recent insights.",
                    "Kill theses that exceed BOTH the mainstream article threshold AND the Polymarket volume threshold in the policy.",
                    "A thesis with zero mainstream coverage and no Polymarket markets is maximally novel.",
                    'RELAXED MODE (when `scan_config.filter_mode == "relaxed"` or `novelty_gate.relaxed_pass_on_angle == true`): if headline coverage is saturated BUT the specific mechanism / specific catalyst date / specific investable instrument leg is uncrowded (zero Polymarket volume on the specific leg AND no published sell-side note on the specific angle), PASS the thesis with a note in `novelty_notes.relaxed_pass_reason` describing which leg remains uncrowded. Do NOT kill on headline saturation alone.',
                    "Use bash to run Polymarket adapter scripts for market search.",
                ],
            ),
            "pre-mortem-analyst": (
                [
                    "the novelty gate artifact (surviving theses only)",
                    "`policy/default.yaml` — pre-mortem prompt",
                ],
                [
                    "for each thesis: a `failure_scenario` (the most likely reason it's wrong), `weak_links` in the causal chain, `confidence_adjustment` (how much to reduce if the failure mode is credible)",
                    "a `verdict` per thesis: `pass`, `downgrade`, or `reject`",
                ],
                [
                    "For each thesis, assume it is wrong. Write the single most likely reason.",
                    "Identify the weakest link in the causal chain — the step most likely to not happen.",
                    "Use websearch to find published skepticism or counter-evidence.",
                    "Be genuinely adversarial — your job is to break theses, not confirm them.",
                    "A thesis with a 4-link causal chain where any link has < 50% probability should be downgraded.",
                ],
            ),
            "consensus-auditor": (
                [
                    "the pre-mortem artifact (surviving and downgraded theses)",
                    "`policy/default.yaml` — consensus audit prompt",
                ],
                [
                    "for each thesis: the `strongest_counter_argument` found, `contrarian_sources` cited, `consensus_direction` (does consensus agree or disagree with the thesis?)",
                    "a `verdict` per thesis: `pass`, `downgrade`, or `reject`",
                ],
                [
                    "Search for the strongest published argument against each thesis.",
                    "If the consensus view already accounts for this risk (e.g., it's widely discussed as a tail risk), the thesis is less novel than it appears — downgrade.",
                    "If everyone is positioned the same way on this thesis, it may be a crowded trade even if the thesis is correct — flag this.",
                    "A thesis where credible experts disagree is more interesting than one where everyone agrees.",
                    'RELAXED MODE (when `scan_config.filter_mode == "relaxed"`): if the HEADLINE is crowded but a specific leg of the trade structure (e.g. a pair\'s short leg, an options skew, a second-order equity) is uncrowded — PASS the thesis and rescope the trade to the uncrowded leg only. Record the uncrowded leg in `relaxed_leg_rescope` and do not downgrade purely on headline crowdedness.',
                ],
            ),
            "historical-analogist": (
                [
                    "the consensus audit artifact",
                    "`policy/default.yaml` — historical analog prompt",
                ],
                [
                    "for each thesis: the `closest_historical_parallel`, `what_happened` in the parallel, `base_rate` (how often do situations like this actually become dominant narratives?), `key_differences` from the historical case",
                    "a `verdict` per thesis: `pass`, `downgrade`, or `reject`",
                ],
                [
                    "Find the closest historical parallel for each thesis using websearch.",
                    "Be specific about the parallel — not just 'trade wars have happened before' but 'the 2018-2019 US-China tariff escalation is the closest analog because X, Y, Z.'",
                    "Evaluate the base rate honestly: most structural risks stay as background noise forever. How often does a situation like this actually become the dominant market narrative?",
                    "If the historical parallel faded without materializing, that's strong evidence for rejection unless the current situation has specific structural differences.",
                ],
            ),
            "portfolio-strategist": (
                [
                    "the historical analog artifact (only theses that passed all adversarial gates)",
                    "`inputs/portfolio.yaml` — user positions and instrument preferences",
                    "`policy/default.yaml` — portfolio strategy rules",
                ],
                [
                    "for each validated thesis: `instruments` mapped (Polymarket markets, Hyperliquid perps), `transmission_mechanism` (how the thesis flows into price), `trade_structure` (direction, sizing, entry triggers, stop conditions), `monitoring_triggers` (leading indicators to watch), `hedge_implications` (tail risk to existing positions)",
                    "if no instruments are available for a thesis, note `no_instruments: true` with an explanation",
                ],
                [
                    "Only process theses above the `portfolio_strategy.min_confidence` threshold from policy.",
                    "Search Polymarket for related prediction markets using bash adapter scripts.",
                    "Identify which Hyperliquid perps are directionally exposed to each thesis.",
                    "Define the transmission mechanism explicitly — from thesis event through to instrument price impact.",
                    "Include monitoring triggers: specific data points or events that would confirm the thesis is activating.",
                    "If the user has existing positions, assess which theses create tail risk for those positions.",
                    "Do not force trade ideas — if a thesis is valid but not tradeable with available instruments, say so.",
                ],
            ),
            "inventory-compiler": (
                [
                    "all previous artifacts in the pipeline",
                    "`inputs/inventory.json` — the previous inventory state",
                    "`policy/default.yaml` — confidence and status rules",
                ],
                [
                    "the updated `thesis_inventory` with confidence trajectories and evidence logs",
                    "`run_summary` — theses added, updated, killed, retired this run",
                    "`monitoring_checklist` — what to watch before next run",
                    "the standard output contract fields",
                    "a human-readable `trade_book.md` written to `"
                    + DEFAULT_ARTIFACTS_DIR
                    + "/$RUN_ID/trade_book.md` as the primary final output",
                ],
                [
                    "Merge all pipeline results into the final inventory state.",
                    "Update thesis statuses: new → active, active → escalating (if confidence > threshold), active → dormant (if confidence < threshold).",
                    "Write the inventory in a format that can be loaded as `inputs/inventory.json` for the next run.",
                    "The monitoring checklist should list specific, actionable items — not vague 'watch this space' notes.",
                    "Compile the standard output contract: signal_snapshot, selected_playbook, candidate_expressions, null_state, risk_checks, job, next_invalidation, trade_book.",
                    "TRADE BOOK FORMAT (mandatory — this is the final interpretable artifact for the user):",
                    "- Write `trade_book.md` alongside `inventory.json`. File must begin with exactly one markdown H1 header then a summary table, then a per-trade section.",
                    "- SUMMARY TABLE columns (markdown pipe table, exact order): `#`, `Thesis`, `Catalyst date`, `SDK surface`, `Instrument`, `Direction`, `Size ($)`, `Max loss ($)`, `Target PnL ($)`, `Invalidation`. One row per retained thesis ranked by final_confidence descending.",
                    "- PER-TRADE SECTION (one per retained thesis, in same order as summary table). Each uses an H2 header `## #<N> — <thesis label>` and includes ≤5 short paragraphs covering: (1) what's happening / the mechanism in 2-3 sentences, (2) the exact SDK call as a fenced code block with real parameters, (3) entry/exit/stop rules, (4) risk (max loss and what invalidates), (5) why this has edge (1-2 sentences tying to positioning gap or base rate).",
                    "- Lead the file with a one-paragraph book-level overview: total gross deployed, number of trades, earliest catalyst date, and any correlated-exposure notes.",
                    "- If the run produced ZERO retained theses, `trade_book.md` must still be written with the H1 header plus an explicit NULL STATE section explaining why.",
                    "- Prefer concrete numbers over adjectives. No emojis. No hedging language.",
                ],
            ),
            "display-composer": (
                [
                    "the inventory artifact (`inventory.json`) — full thesis inventory with all data",
                    "the novelty gate artifact — to reconstruct the funnel (surviving vs killed counts)",
                    "the pre-mortem, consensus audit, and historical analog artifacts — adversarial verdicts",
                    "the portfolio strategy artifact — trade expressions",
                    "`inputs/inventory.json` — previous state (to compute run diff)",
                ],
                [
                    "a single `display.json` artifact that the applet reads directly",
                    "the JSON must conform exactly to the display data contract (see rules below)",
                ],
                [
                    "PURPOSE: You are a display editor. Your job is to take the raw analytical artifacts and produce a polished, readable applet data bundle. You do NOT do analysis — you translate analysis into clear presentation.",
                    "",
                    "WRITING GUIDELINES — every piece of text you write will be shown directly to the user in a dashboard UI:",
                    "- `headline`: 4-8 words, no jargon. A newspaper editor would approve it. Example: 'EU budget rules could crack Italian bonds' not 'Fiscal framework enforcement creates sovereign spread widening risk'.",
                    "- `summary`: 1-2 sentences, plain English, explains the thesis to someone who doesn't follow markets. Must answer: what could happen, why it matters, and roughly when.",
                    "- `mechanism_steps`: Break the causal chain into 3-5 short steps, each one sentence. A reader should be able to follow the logic without domain expertise.",
                    "- `why_it_matters`: One sentence explaining the market impact in concrete terms — not 'risk-off' but 'crypto and equities would sell off as investors flee to cash'.",
                    "- `adversarial_summary`: For each adversarial verdict, write a one-sentence plain-English version of the finding. Not the raw data — the interpretation.",
                    "- `trade_explainer`: If there's a trade expression, explain it simply: 'Bet against the Euro via a short EUR-USD perpetual on Hyperliquid, entering when Italian bond spreads cross 200 basis points.'",
                    "- `run_diff_description`: If this is a recurring run, write one sentence summarizing what changed: 'Two new theses emerged, one is accelerating, and two stale ideas were dropped.'",
                    "",
                    "TAGS — assign 1-3 tags per thesis from this vocabulary:",
                    "  Domains: `geopolitical`, `macro`, `regulatory`, `tech`, `structural`",
                    "  Urgency: `imminent` (< 3mo), `medium-term` (3-12mo), `long-horizon` (> 12mo)",
                    "  Conviction: `high-conviction` (conf > 0.70), `moderate` (0.50-0.70), `speculative` (< 0.50)",
                    "  Special: `cross-domain` (reinforced by multiple domains), `contrarian` (goes against consensus), `catalyst-approaching` (nearest catalyst < 30 days)",
                    "",
                    "DISPLAY DATA CONTRACT — `display.json` must have exactly this shape:",
                    "{",
                    '  "run_meta": {',
                    '    "run_date": "ISO date",',
                    '    "run_id": "string",',
                    '    "domains_scanned": number,',
                    '    "is_recurring": boolean,',
                    '    "run_headline": "one-sentence summary of this run\'s outcome"',
                    "  },",
                    '  "funnel": {',
                    '    "domain_candidates": number,',
                    '    "post_synthesis": number,',
                    '    "post_novelty_gate": number,',
                    '    "post_pre_mortem": number,',
                    '    "post_consensus": number,',
                    '    "post_historical": number,',
                    '    "with_instruments": number,',
                    '    "dropoffs": [{ "stage": "string", "thesis_label": "string", "reason": "one sentence" }]',
                    "  },",
                    '  "theses": [{',
                    '    "thesis_id": "string",',
                    '    "headline": "4-8 word headline",',
                    '    "summary": "1-2 sentence plain-English summary",',
                    '    "domain": "string",',
                    '    "status": "active|escalating|dormant|new",',
                    '    "confidence": number,',
                    '    "novelty_score": number,',
                    '    "tags": ["string"],',
                    '    "mechanism_steps": ["step 1", "step 2", "step 3"],',
                    '    "why_it_matters": "one sentence",',
                    '    "prediction": "falsifiable prediction text",',
                    '    "deadline": "ISO date",',
                    '    "days_to_deadline": number,',
                    '    "days_to_nearest_catalyst": number,',
                    '    "preconditions": [{ "text": "string", "met": boolean, "date": "ISO date or null" }],',
                    '    "evidence": [{ "date": "ISO date", "type": "supporting|contrary", "quality": "high|medium|low", "source": "string", "summary": "one sentence" }],',
                    '    "confidence_history": [{ "run": "ISO date", "score": number, "reason": "short phrase" }],',
                    '    "adversarial": {',
                    '      "novelty_gate": { "verdict": "pass|fail", "summary": "one sentence" },',
                    '      "pre_mortem": { "verdict": "pass|downgrade|reject", "summary": "one sentence" },',
                    '      "consensus_audit": { "verdict": "pass|downgrade|reject", "summary": "one sentence" },',
                    '      "historical_analog": { "verdict": "pass|downgrade|reject", "summary": "one sentence", "parallel": "name of historical parallel", "base_rate": "X% of similar situations escalated" }',
                    "    },",
                    '    "portfolio": {',
                    '      "has_instruments": boolean,',
                    '      "trade_explainer": "plain-English explanation of the trade",',
                    '      "instruments": [{ "venue": "string", "type": "string", "symbol": "string", "direction": "string" }],',
                    '      "entry_trigger": "string",',
                    '      "stop_condition": "string",',
                    '      "monitoring_triggers": ["string"]',
                    "    },",
                    '    "radar_x": number,  // months to nearest catalyst, for plot positioning',
                    '    "radar_y": number   // confidence, for plot positioning',
                    "  }],",
                    '  "killed_theses": [{',
                    "    // Same full shape as surviving theses (headline, summary, domain, tags,",
                    "    // mechanism_steps, evidence, adversarial verdicts up to the kill stage),",
                    "    // PLUS these two extra fields:",
                    '    "killed_at_stage": "string",',
                    '    "kill_reason": "one sentence, plain English"',
                    "    // Include all fields the thesis had when it was killed — mechanism_steps,",
                    "    // evidence gathered so far, and adversarial verdicts for stages it passed",
                    "    // through (leave later-stage verdicts null). This lets users inspect the",
                    "    // full reasoning behind why a thesis was filtered out.",
                    "  }],",
                    '  "changes": {',
                    '    "description": "one sentence run diff summary",',
                    '    "new": [{ "thesis_id": "string", "headline": "string", "confidence": number }],',
                    '    "confidence_up": [{ "thesis_id": "string", "headline": "string", "old": number, "new": number, "reason": "short phrase" }],',
                    '    "confidence_down": [{ "thesis_id": "string", "headline": "string", "old": number, "new": number, "reason": "short phrase" }],',
                    '    "killed": [{ "thesis_id": "string", "headline": "string", "reason": "string" }],',
                    '    "escalating": [{ "thesis_id": "string", "headline": "string", "confidence": number }]',
                    "  }",
                    "}",
                    "",
                    "QUALITY CHECKS before writing the artifact:",
                    "- Every thesis has a headline that a non-expert could understand.",
                    "- Every summary avoids jargon. No 'risk-off', 'dovish pivot', 'basis trade' without explanation.",
                    "- mechanism_steps are ordered causally and each step follows logically from the previous one.",
                    "- Tags are from the allowed vocabulary only.",
                    "- radar_x = days_to_nearest_catalyst / 30 (convert to months). radar_y = confidence.",
                    "- Theses in the `theses` array are sorted by confidence descending.",
                    "- Killed theses in `killed_theses` are sorted by the stage they were killed at (earliest stage first).",
                    "- If `is_recurring` is false, the `changes` object should have empty arrays and description 'First run — no previous data to compare.'",
                    "- Do not invent data. Everything must come from the pipeline artifacts. Your job is to rewrite and format, not to analyze.",
                ],
            ),
        }
        read_items, write_items, rules = nr_instructions.get(
            agent.agent_id,
            ([], [], ["Stay inside your assigned phase."]),
        )
        lines = [f"# {agent.agent_id}", "", agent.description, "", "Read:"]
        lines.extend(
            [f"- {item}" for item in read_items]
            or ["- only the inputs required for your phase"]
        )
        lines.extend(
            [
                "",
                "Write:",
                f"- exactly one JSON object to `{DEFAULT_ARTIFACTS_DIR}/$RUN_ID/{agent.output_name}`",
            ]
        )
        lines.extend([f"- include {item}" for item in write_items])
        lines.extend(
            [
                "",
                "Rules:",
                "- Do not spawn other agents.",
                "- Do not compile the final answer.",
            ]
        )
        lines.extend([f"- {item}" for item in rules])
        return "\n".join(lines) + "\n"
    return (
        f"# {agent.agent_id}\n\n"
        f"{agent.description}\n\n"
        "Requirements:\n"
        "- Write exactly one artifact.\n"
        "- Do not spawn other agents.\n"
        "- Stay within your assigned phase.\n"
        "- Do not compile the final answer.\n"
        f"- Output path: `{DEFAULT_ARTIFACTS_DIR}/$RUN_ID/{agent.output_name}`\n"
    )


def _pipeline_validate_artifact_script() -> str:
    return (
        "#!/usr/bin/env python3\n"
        "from __future__ import annotations\n\n"
        "import json\n"
        "import re\n"
        "import sys\n"
        "from datetime import date, timedelta\n"
        "from pathlib import Path\n\n"
        "DOMAIN_SCAN_AGENTS = {\n"
        '    "geopolitical-analyst",\n'
        '    "macro-strategist",\n'
        '    "regulatory-tracker",\n'
        '    "tech-scout",\n'
        '    "structural-analyst",\n'
        "}\n"
        'HTTPS_RE = re.compile(r"^https?://", re.IGNORECASE)\n\n'
        "def _fail(msg: str) -> None:\n"
        "    raise SystemExit(f'verification_protocol failure: {msg}')\n\n"
        "def _validate_domain_scan(payload: dict, *, recency_days: int = 30, min_tool_calls: int = 3) -> None:\n"
        "    theses = payload.get('candidate_theses')\n"
        "    if not isinstance(theses, list) or not theses:\n"
        "        _fail('candidate_theses must be a non-empty array')\n"
        "    today = date.today()\n"
        "    recency_cutoff = today - timedelta(days=recency_days)\n"
        "    for idx, thesis in enumerate(theses):\n"
        "        if not isinstance(thesis, dict):\n"
        "            _fail(f'thesis[{idx}] must be an object')\n"
        "        tid = thesis.get('thesis_id') or f'index_{idx}'\n"
        "        evidence = thesis.get('evidence')\n"
        "        if not isinstance(evidence, list) or not evidence:\n"
        "            _fail(f'{tid}: evidence must be a non-empty array')\n"
        "        recent_hits = 0\n"
        "        for ev in evidence:\n"
        "            url = (ev or {}).get('source_url')\n"
        "            if not isinstance(url, str) or not HTTPS_RE.match(url):\n"
        "                _fail(f'{tid}: every evidence entry must carry an https source_url')\n"
        "            d = (ev or {}).get('date')\n"
        "            if isinstance(d, str):\n"
        "                try:\n"
        "                    if date.fromisoformat(d) >= recency_cutoff:\n"
        "                        recent_hits += 1\n"
        "                except ValueError:\n"
        "                    pass\n"
        "        if recent_hits < 1:\n"
        "            _fail(f'{tid}: at least one evidence entry must be within the last {recency_days} days')\n"
        "        cc = thesis.get('currency_check')\n"
        "        if not isinstance(cc, dict):\n"
        "            _fail(f'{tid}: currency_check object is required')\n"
        "        if cc.get('already_happened') is True:\n"
        "            _fail(f'{tid}: currency_check.already_happened is true — drop the thesis before writing')\n"
        "        cc_url = cc.get('evidence_url')\n"
        "        if not isinstance(cc_url, str) or not HTTPS_RE.match(cc_url):\n"
        "            _fail(f'{tid}: currency_check.evidence_url must be an https URL')\n"
        "        queries = thesis.get('verification_queries')\n"
        "        if not isinstance(queries, list) or len(queries) < min_tool_calls:\n"
        "            _fail(f'{tid}: verification_queries must list at least {min_tool_calls} tool calls')\n"
        "        for q in queries:\n"
        "            q_url = (q or {}).get('url')\n"
        "            if not isinstance(q_url, str) or not HTTPS_RE.match(q_url):\n"
        "                _fail(f'{tid}: each verification_queries entry needs an https url')\n"
        "        for cat in thesis.get('catalysts') or []:\n"
        "            est = (cat or {}).get('estimated_date')\n"
        "            if isinstance(est, str):\n"
        "                try:\n"
        "                    if date.fromisoformat(est) <= today:\n"
        "                        _fail(f'{tid}: catalyst estimated_date {est} is not in the future')\n"
        "                except ValueError:\n"
        "                    pass\n"
        "        exec_ = thesis.get('executability')\n"
        "        if not isinstance(exec_, dict):\n"
        "            _fail(f'{tid}: executability object is required')\n"
        "        tier = exec_.get('tier')\n"
        "        if tier not in ('A', 'B'):\n"
        "            _fail(f'{tid}: executability.tier must be A or B (got {tier!r}); reject-tier theses must be dropped upstream')\n"
        "        leg = exec_.get('primary_leg')\n"
        "        if not isinstance(leg, dict):\n"
        "            _fail(f'{tid}: executability.primary_leg object is required')\n"
        "        valid_surfaces = {'swap','perp','lending','vault','lp','pendle','contract','polymarket','ccxt'}\n"
        "        if leg.get('surface') not in valid_surfaces:\n"
        "            _fail(f'{tid}: primary_leg.surface must be one of {sorted(valid_surfaces)}')\n"
        "        for key in ('instrument', 'venue', 'liquidity_check'):\n"
        "            if not isinstance(leg.get(key), str) or not leg[key].strip():\n"
        "                _fail(f'{tid}: primary_leg.{key} must be a non-empty string')\n"
        "        if tier == 'B' and not isinstance(exec_.get('proxy_basis'), str):\n"
        "            _fail(f'{tid}: Tier B thesis must declare proxy_basis string')\n\n"
        "def main() -> int:\n"
        "    if len(sys.argv) != 3:\n"
        "        raise SystemExit('usage: validate_artifact.py <agent-id> <path>')\n"
        "    agent_id, path_value = sys.argv[1], sys.argv[2]\n"
        "    artifact_path = Path(path_value)\n"
        "    if not artifact_path.exists():\n"
        "        raise SystemExit(f'missing artifact for {agent_id}: {artifact_path}')\n"
        '    payload = json.loads(artifact_path.read_text(encoding="utf-8"))\n'
        "    if not isinstance(payload, dict):\n"
        "        raise SystemExit('artifact payload must be a JSON object')\n"
        "    if agent_id in DOMAIN_SCAN_AGENTS:\n"
        "        _validate_domain_scan(payload)\n"
        "    print(json.dumps({'ok': True, 'agent_id': agent_id, 'path': str(artifact_path)}))\n"
        "    return 0\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"
    )


def _pipeline_compile_job_script() -> str:
    return (
        "#!/usr/bin/env python3\n"
        "from __future__ import annotations\n\n"
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n\n"
        "def main() -> int:\n"
        "    if len(sys.argv) != 2:\n"
        "        raise SystemExit('usage: compile_job.py <run-dir>')\n"
        "    run_dir = Path(sys.argv[1])\n"
        "    run_dir.mkdir(parents=True, exist_ok=True)\n"
        "    output_path = run_dir / 'job.json'\n"
        "    payload = {\n"
        "        'ok': True,\n"
        "        'mode': 'draft',\n"
        "        'note': 'Replace placeholder job compilation with path-specific logic.',\n"
        "    }\n"
        "    output_path.write_text(json.dumps(payload, indent=2) + '\\n', encoding='utf-8')\n"
        "    print(json.dumps({'ok': True, 'path': str(output_path)}))\n"
        "    return 0\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"
    )


def _pipeline_inject_run_context_script() -> str:
    return (
        "#!/usr/bin/env python3\n"
        "from __future__ import annotations\n\n"
        "import json\n"
        "import os\n\n"
        "def main() -> int:\n"
        "    payload = {\n"
        "        'ok': True,\n"
        "        'run_id': os.environ.get('RUN_ID') or os.environ.get('CLAUDE_SESSION_ID') or 'unknown',\n"
        "    }\n"
        "    print(json.dumps(payload))\n"
        "    return 0\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"
    )


def _pipeline_validate_hook_script() -> str:
    return (
        "#!/usr/bin/env python3\n"
        "from __future__ import annotations\n\n"
        "import json\n\n"
        "def main() -> int:\n"
        "    print(json.dumps({'ok': True, 'validated': True}))\n"
        "    return 0\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"
    )


def _pipeline_runtime_readme() -> str:
    return (
        "# Runtime\n\n"
        "Use this directory for host-neutral runtime helpers or compiled pipeline metadata.\n"
        "Do not store mutable run artifacts here.\n"
    )


def _pipeline_artifacts_readme() -> str:
    return (
        "# Artifacts\n\n"
        "Runtime artifacts are written here per run under `$RUN_ID/`.\n"
        "These files are intentionally excluded from bundle builds.\n"
    )


def _pipeline_component_source() -> str:
    return (
        "from __future__ import annotations\n\n"
        "import json\n"
        "from pathlib import Path\n\n"
        "import yaml\n\n"
        "ROOT = Path(__file__).resolve().parents[1]\n\n"
        "def main() -> None:\n"
        "    manifest = yaml.safe_load((ROOT / 'wfpath.yaml').read_text(encoding='utf-8')) or {}\n"
        "    policy = yaml.safe_load((ROOT / 'policy' / 'default.yaml').read_text(encoding='utf-8')) or {}\n"
        "    pipeline = manifest.get('pipeline') or {}\n"
        "    summary = {\n"
        "        'slug': manifest.get('slug'),\n"
        "        'archetype': policy.get('archetype'),\n"
        "        'entry_command': pipeline.get('entry_command'),\n"
        "        'signals': sorted((policy.get('signals') or {}).keys()),\n"
        "        'playbooks': sorted((policy.get('playbooks') or {}).keys()),\n"
        "    }\n"
        "    print(json.dumps(summary, indent=2))\n\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )


def _pipeline_fixture(
    name: str,
    *,
    mode: str,
    null_selected: bool,
    archetype: str,
) -> str:
    if archetype == "conditional-router":
        fixtures = {
            "base_case": (
                'name: "base_case"\n'
                "output:\n"
                "  signal_snapshot:\n"
                "    recession_prob: 0.61\n"
                "  selected_playbook:\n"
                '    id: "risk_off"\n'
                "    score: 0.71\n"
                "  candidate_expressions:\n"
                '    - id: "direct-polymarket"\n'
                '      type: "direct_polymarket"\n'
                "      score: 0.58\n"
                '    - id: "proxy-hl-basket"\n'
                '      type: "hyperliquid_proxy"\n'
                "      score: 0.71\n"
                "  null_state:\n"
                "    selected: false\n"
                '    reason: "Proxy expression clears the null-state threshold."\n'
                "  risk_checks:\n"
                "    passed: true\n"
                '    mode: "armed"\n'
                "    leverage_ok: true\n"
                "    liquidity_ok: true\n"
                "  job:\n"
                '    mode: "armed"\n'
                "    armed: true\n"
                "    poll_every: 300\n"
                '    on_enter_signal: "entered-risk-off"\n'
                '  next_invalidation: "recession_prob < 0.55"\n'
            ),
            "null_state": (
                'name: "null_state"\n'
                "output:\n"
                "  signal_snapshot:\n"
                "    recession_prob: 0.41\n"
                "  selected_playbook:\n"
                '    id: "null-state"\n'
                "    score: 0.44\n"
                "  candidate_expressions:\n"
                '    - id: "direct-polymarket"\n'
                '      type: "direct_polymarket"\n'
                "      score: 0.40\n"
                '    - id: "proxy-hl-basket"\n'
                '      type: "hyperliquid_proxy"\n'
                "      score: 0.43\n"
                "  null_state:\n"
                "    selected: true\n"
                '    reason: "No candidate clears the minimum score threshold."\n'
                "  risk_checks:\n"
                "    passed: true\n"
                '    mode: "null"\n'
                "    leverage_ok: true\n"
                "    liquidity_ok: false\n"
                "  job:\n"
                '    mode: "null"\n'
                "    armed: false\n"
                "    poll_every: 300\n"
                '  next_invalidation: "recession_prob >= 0.60"\n'
            ),
            "risk_gate": (
                'name: "risk_gate"\n'
                "output:\n"
                "  signal_snapshot:\n"
                "    recession_prob: 0.84\n"
                "  selected_playbook:\n"
                '    id: "crash_mode"\n'
                "    score: 0.74\n"
                "  candidate_expressions:\n"
                '    - id: "proxy-hl-basket"\n'
                '      type: "hyperliquid_proxy"\n'
                "      score: 0.74\n"
                "  null_state:\n"
                "    selected: false\n"
                '    reason: "Trade edge is real, but leverage must be reduced before arming."\n'
                "  risk_checks:\n"
                "    passed: false\n"
                '    mode: "draft"\n'
                "    leverage_ok: false\n"
                "    liquidity_ok: true\n"
                '    rejection_reason: "Requested leverage exceeds policy max_leverage."\n'
                "  job:\n"
                '    mode: "draft"\n'
                "    armed: false\n"
                "    poll_every: 300\n"
                '  next_invalidation: "resize crash_mode to fit max_leverage"\n'
            ),
        }
        return fixtures[name]
    if archetype == "narrative-radar":
        fixtures = {
            "base_case": (
                'name: "base_case"\n'
                "output:\n"
                "  signal_snapshot:\n"
                "    domains_scanned: 5\n"
                "    new_theses_found: 3\n"
                "    existing_theses_updated: 2\n"
                "  selected_playbook:\n"
                '    id: "narrative-radar-generate"\n'
                "    score: 0.68\n"
                "  candidate_expressions:\n"
                '    - id: "eu-fiscal-fragmentation"\n'
                '      type: "perp-directional"\n'
                "      confidence: 0.62\n"
                "      novelty_score: 0.78\n"
                '    - id: "southeast-asia-supply-chain"\n'
                '      type: "polymarket-direct"\n'
                "      confidence: 0.55\n"
                "      novelty_score: 0.85\n"
                "  null_state:\n"
                "    selected: false\n"
                '    reason: "Two theses passed adversarial review with actionable instruments."\n'
                "  risk_checks:\n"
                "    passed: true\n"
                '    mode: "armed"\n'
                "    theses_above_confidence: 2\n"
                "    avg_novelty: 0.81\n"
                "  job:\n"
                '    mode: "armed"\n'
                "    armed: true\n"
                "    thesis_count: 2\n"
                "    monitoring_items: 4\n"
                '  next_invalidation: "Re-scan in 7 days or on major geopolitical event."\n'
                '  trade_book: ".wf-artifacts/$RUN_ID/trade_book.md"\n'
            ),
            "null_state": (
                'name: "null_state"\n'
                "output:\n"
                "  signal_snapshot:\n"
                "    domains_scanned: 5\n"
                "    new_theses_found: 4\n"
                "    existing_theses_updated: 0\n"
                "  selected_playbook:\n"
                '    id: "null-state"\n'
                "    score: 0.30\n"
                "  candidate_expressions: []\n"
                "  null_state:\n"
                "    selected: true\n"
                '    reason: "All candidate theses were killed by the novelty gate (already mainstream) or rejected by adversarial review."\n'
                "  risk_checks:\n"
                "    passed: true\n"
                '    mode: "null"\n'
                "    theses_above_confidence: 0\n"
                "  job:\n"
                '    mode: "null"\n'
                "    armed: false\n"
                "    thesis_count: 0\n"
                '  next_invalidation: "Re-scan in 7 days."\n'
                '  trade_book: ".wf-artifacts/$RUN_ID/trade_book.md"\n'
            ),
            "risk_gate": (
                'name: "risk_gate"\n'
                "output:\n"
                "  signal_snapshot:\n"
                "    domains_scanned: 5\n"
                "    new_theses_found: 2\n"
                "    existing_theses_updated: 3\n"
                "  selected_playbook:\n"
                '    id: "narrative-radar-generate"\n'
                "    score: 0.58\n"
                "  candidate_expressions:\n"
                '    - id: "ai-governance-crackdown"\n'
                '      type: "polymarket-direct"\n'
                "      confidence: 0.61\n"
                "      novelty_score: 0.72\n"
                "  null_state:\n"
                "    selected: false\n"
                '    reason: "Thesis is valid but no liquid instruments found for expression."\n'
                "  risk_checks:\n"
                "    passed: false\n"
                '    mode: "draft"\n'
                "    theses_above_confidence: 1\n"
                '    rejection_reason: "No Polymarket markets with sufficient liquidity, perp exposure too indirect."\n'
                "  job:\n"
                '    mode: "draft"\n'
                "    armed: false\n"
                "    thesis_count: 1\n"
                '  next_invalidation: "Monitor for new Polymarket markets on AI governance."\n'
                '  trade_book: ".wf-artifacts/$RUN_ID/trade_book.md"\n'
            ),
        }
        return fixtures[name]
    return (
        f'name: "{name}"\n'
        "output:\n"
        "  signal_snapshot:\n"
        '    primary_signal: "placeholder"\n'
        "  selected_playbook:\n"
        '    id: "placeholder"\n'
        "    score: 0.70\n"
        "  candidate_expressions:\n"
        "    - id: candidate-1\n"
        "      score: 0.70\n"
        "  null_state:\n"
        f"    selected: {str(null_selected).lower()}\n"
        "    reason: placeholder\n"
        "  risk_checks:\n"
        "    passed: true\n"
        f'    mode: "{mode}"\n'
        "  job:\n"
        f'    mode: "{mode}"\n'
        f"    armed: {str(mode == 'armed').lower()}\n"
        "  next_invalidation: placeholder\n"
    )


def _pipeline_eval(name: str, fixture: str, assertions: dict[str, Any]) -> str:
    lines = [f'name: "{name}"', 'type: "fixture"', f'fixture: "{fixture}"', "assert:"]
    for key, value in assertions.items():
        serialized = (
            json.dumps(value) if not isinstance(value, bool) else str(value).lower()
        )
        lines.append(f"  {key}: {serialized}")
    lines.append("")
    return "\n".join(lines)


def _pipeline_host_eval(hosts: list[str], expected_files: list[str]) -> str:
    lines = ['name: "host-render"', 'type: "host_render"', "hosts:"]
    for host in hosts:
        lines.append(f'  - "{host}"')
    lines.append("expected_files:")
    for path in expected_files:
        lines.append(f'  - "{path}"')
    lines.append("")
    return "\n".join(lines)


def init_path(
    *,
    path_dir: Path,
    slug: str,
    name: str | None = None,
    version: str = "0.1.0",
    summary: str = "",
    primary_kind: str = "bundle",
    tags: list[str] | None = None,
    with_applet: bool = False,
    with_skill: bool = True,
    template: str = "basic",
    archetype: str | None = None,
    overwrite: bool = False,
) -> PathInitResult:
    slug = slugify(slug)
    if not slug or not _SLUG_RE.fullmatch(slug):
        raise PathScaffoldError("Invalid slug (expected lowercase url-safe slug)")

    path_dir = path_dir.resolve()
    path_dir.mkdir(parents=True, exist_ok=True)

    path_name = (name or humanize_slug(slug)).strip() or slug
    template = (template or "basic").strip().lower()
    archetype = (archetype or "").strip() or None
    if template not in {"basic", "pipeline"}:
        raise PathScaffoldError("Unsupported template (expected basic or pipeline)")
    if template == "pipeline" and not archetype:
        raise PathScaffoldError("template=pipeline requires an archetype")
    primary_kind = (primary_kind or "bundle").strip()
    if template == "pipeline" and primary_kind == "bundle":
        primary_kind = "policy"
    tag_list = tags if tags is not None else [primary_kind]
    if primary_kind not in tag_list:
        tag_list = [primary_kind, *tag_list]
    if template == "pipeline" and archetype and archetype not in tag_list:
        tag_list = [archetype, *tag_list]

    if primary_kind == "strategy":
        component_kind = "strategy"
        component_path = "strategy.py"
        component_template = "components/strategy.py.tmpl"
    else:
        component_kind = "script"
        component_path = "scripts/main.py"
        component_template = "components/script.py.tmpl"

    manifest_text = _build_wfpath_yaml(
        slug=slug,
        name=path_name,
        version=version,
        summary=summary,
        primary_kind=primary_kind,
        tags=tag_list,
        component_kind=component_kind,
        component_path=component_path,
        with_applet=with_applet,
        with_skill=with_skill,
        template=template,
        archetype=archetype,
    )

    ctx: dict[str, Any] = {
        "slug": slug,
        "name": path_name,
        "version": version,
        "summary": summary.strip() or "TODO: describe what this path does.",
        "primary_kind": primary_kind,
        "component_path": component_path,
        "template": template,
        "archetype": archetype or "",
    }

    created: list[Path] = []
    overwritten: list[Path] = []
    skipped: list[Path] = []

    def write(rel_path: str, content: str) -> None:
        path = path_dir / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not overwrite:
            skipped.append(path)
            return
        if path.exists():
            overwritten.append(path)
        else:
            created.append(path)
        path.write_text(content.rstrip() + "\n", encoding="utf-8")

    write("wfpath.yaml", manifest_text)
    readme = (
        _pipeline_readme(
            name=path_name,
            slug=slug,
            summary=ctx["summary"],
            archetype=archetype,
            component_path=component_path,
        )
        if template == "pipeline" and archetype
        else _render_template(_read_template("README.md.tmpl"), ctx)
    )
    write("README.md", readme)
    component_source = (
        _pipeline_component_source()
        if template == "pipeline" and component_kind == "script"
        else _render_template(_read_template(component_template), ctx)
    )
    write(component_path, component_source)

    if with_skill:
        instructions = (
            _pipeline_instructions(slug, archetype)
            if template == "pipeline" and archetype
            else _render_template(_read_template("skill/instructions.md.tmpl"), ctx)
        )
        write("skill/instructions.md", instructions)

    if with_applet:
        write(
            "applet/applet.manifest.json",
            _render_template(_read_template("applet/applet.manifest.json.tmpl"), ctx),
        )
        write(
            "applet/dist/index.html",
            _render_template(_read_template("applet/dist/index.html.tmpl"), ctx),
        )
        write(
            "applet/dist/assets/app.js",
            _render_template(_read_template("applet/dist/assets/app.js.tmpl"), ctx),
        )

    if template == "pipeline" and archetype:
        archetype_config = get_pipeline_archetype(archetype)
        write("policy/default.yaml", _pipeline_policy_template(archetype))
        write("pipeline/graph.yaml", _pipeline_graph_text(archetype))
        write("runtime/README.md", _pipeline_runtime_readme())
        write(f"{DEFAULT_ARTIFACTS_DIR}/README.md", _pipeline_artifacts_readme())
        write("skill/references/pipeline.md", _pipeline_reference_pipeline(archetype))
        write("skill/references/signals.md", _pipeline_reference_signals())
        write("skill/references/risk.md", _pipeline_reference_risk())
        write("skill/references/examples.md", _pipeline_reference_examples(slug))
        write(
            "skill/scripts/validate_artifact.py",
            _pipeline_validate_artifact_script(),
        )
        write("skill/scripts/compile_job.py", _pipeline_compile_job_script())
        write(
            "skill/scripts/inject_run_context.py",
            _pipeline_inject_run_context_script(),
        )
        write("skill/scripts/validate_hook.py", _pipeline_validate_hook_script())
        for slot in archetype_config.input_slots:
            write(slot.path, _slot_placeholder(slot, archetype=archetype))
            write(slot.schema, _slot_schema(slot, archetype=archetype))
        for agent in archetype_config.agents:
            write(
                f"skill/agents/{agent.agent_id}.md",
                _pipeline_agent_body(agent, archetype=archetype),
            )
        write(
            "tests/fixtures/base_case.yaml",
            _pipeline_fixture(
                "base_case",
                mode="armed",
                null_selected=False,
                archetype=archetype,
            ),
        )
        write(
            "tests/fixtures/null_state.yaml",
            _pipeline_fixture(
                "null_state",
                mode="null",
                null_selected=True,
                archetype=archetype,
            ),
        )
        write(
            "tests/fixtures/risk_gate.yaml",
            _pipeline_fixture(
                "risk_gate",
                mode="draft",
                null_selected=False,
                archetype=archetype,
            ),
        )
        write(
            "tests/evals/output_shape.yaml",
            _pipeline_eval(
                "output-shape",
                "base_case",
                (
                    {
                        "null_state.selected": False,
                        "job.mode": "armed",
                        "risk_checks.mode": "armed",
                        "selected_playbook.id": "risk_off",
                    }
                    if archetype == "conditional-router"
                    else {
                        "null_state.selected": False,
                        "job.mode": "armed",
                        "risk_checks.mode": "armed",
                    }
                ),
            ),
        )
        write(
            "tests/evals/null_state.yaml",
            _pipeline_eval(
                "null-state",
                "null_state",
                (
                    {
                        "null_state.selected": True,
                        "job.mode": "null",
                        "selected_playbook.id": "null-state",
                    }
                    if archetype == "conditional-router"
                    else {
                        "null_state.selected": True,
                        "job.mode": "null",
                    }
                ),
            ),
        )
        write(
            "tests/evals/risk_gate.yaml",
            _pipeline_eval(
                "risk-gate",
                "risk_gate",
                (
                    {
                        "null_state.selected": False,
                        "job.mode": "draft",
                        "risk_checks.passed": False,
                    }
                    if archetype == "conditional-router"
                    else {
                        "null_state.selected": False,
                        "job.mode": "draft",
                    }
                ),
            ),
        )
        write(
            "tests/evals/host_render.yaml",
            _pipeline_host_eval(
                ["claude", "opencode"],
                [
                    f"install/.claude/skills/{slug}/SKILL.md",
                    f"install/.opencode/skills/{slug}/SKILL.md",
                ],
            ),
        )

    template_meta = {
        "template": template,
        "template_version": "0.1.0",
        "created_with": "wayfinder-paths",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "answers": {
            "slug": slug,
            "name": path_name,
            "version": version,
            "primary_kind": primary_kind,
            "archetype": archetype,
            "with_applet": with_applet,
            "with_skill": with_skill,
            "component_path": component_path,
        },
    }
    meta_path = path_dir / ".wayfinder" / "template.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    if meta_path.exists() and not overwrite:
        skipped.append(meta_path)
    else:
        if meta_path.exists():
            overwritten.append(meta_path)
        else:
            created.append(meta_path)
        meta_path.write_text(
            json.dumps(template_meta, indent=2, default=str) + "\n", encoding="utf-8"
        )

    return PathInitResult(
        path_dir=path_dir,
        manifest_path=path_dir / "wfpath.yaml",
        created_files=created,
        overwritten_files=overwritten,
        skipped_files=skipped,
    )
