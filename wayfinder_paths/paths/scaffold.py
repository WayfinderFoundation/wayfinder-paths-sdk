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
        for field in STANDARD_OUTPUT_CONTRACT:
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
        "import sys\n"
        "from pathlib import Path\n\n"
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
