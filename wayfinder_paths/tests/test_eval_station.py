from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]


def load_eval_station():
    path = REPO / "scripts" / "eval_station.py"
    spec = importlib.util.spec_from_file_location("eval_station", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_station"] = module
    spec.loader.exec_module(module)
    return module


def test_eval_station_candidate_command_is_top_level_and_natural() -> None:
    station = load_eval_station()

    command = station.build_candidate_command(
        "/bin/opencode",
        "wayfinder/deepseek-v4-pro",
        "Question text",
        title="eval-title",
        directory="/tmp/eval-workspace",
    )

    assert "--agent" not in command
    assert command[:4] == ["/bin/opencode", "run", "-m", "wayfinder/deepseek-v4-pro"]
    assert command[4:6] == ["--dir", "/tmp/eval-workspace"]
    assert command[-1] == "Question text"
    assert "FINAL ANSWER" not in " ".join(command)
    assert "tool calls" not in " ".join(command).lower()
    assert "timeout" not in " ".join(command).lower()


def test_eval_station_judge_prompt_receives_only_outputs_not_speed_metadata() -> None:
    station = load_eval_station()

    prompt = station.build_judge_prompt(
        "Rubric text",
        "Question text",
        "Answer A text",
        "Answer B text",
    )

    assert "Question text" in prompt
    assert "Answer A text" in prompt
    assert "Answer B text" in prompt
    assert "Rubric text" in prompt
    for forbidden in (
        "duration",
        "speed",
        "token",
        "cost",
        "variant identity",
        "seconds",
    ):
        assert forbidden not in prompt.lower()


def test_eval_station_records_duration_outside_judge_prompt() -> None:
    station_text = (REPO / "scripts" / "eval_station.py").read_text("utf-8")

    assert "duration_seconds" in station_text
    assert "write_markdown_report" in station_text
    assert "build_judge_prompt(" in station_text
    assert (
        "duration_seconds"
        not in station_text.split("def build_judge_prompt", 1)[1].split(
            "\ndef copy_workspace", 1
        )[0]
    )


def test_eval_station_flags_checkpoint_final_answers_without_judge_metadata() -> None:
    station = load_eval_station()

    issues = station.detect_final_answer_issues(
        "Good setup. Continue if you have next steps.\n<userSuggestions />"
    )

    assert {issue["code"] for issue in issues} == {"CHECKPOINT_CONTINUATION"}
    assert (
        station.detect_final_answer_issues(
            "Authoritative verdict first.\n<userSuggestions />"
        )
        == []
    )
    assert "final_answer_issues" in (REPO / "scripts" / "eval_station.py").read_text(
        "utf-8"
    )


def test_eval_station_exports_wayfinder_key_for_candidate_provider() -> None:
    station_text = (REPO / "scripts" / "eval_station.py").read_text("utf-8")

    assert "def resolve_wayfinder_model_env" in station_text
    assert "def force_eval_wayfinder_api_env" in station_text
    assert "WAYFINDER_API_KEY" in station_text
    assert "system.api_key." in station_text
    assert "system.api_key/system.dev_api_key" not in station_text
    assert "resolve_wayfinder_model_env(candidate_model, env)" in station_text


def test_primary_schedule_reads_use_timezone_aware_sports_snapshot() -> None:
    for path in (
        REPO / ".opencode" / "agents" / "wayfinder.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "wayfinder.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_workpack_challenger"
        / "wayfinder.md",
    ):
        text = path.read_text("utf-8")
        assert "what games are on tonight?" in text
        assert "pass `timezone` to the scoreboard call" in text
        assert "inspect `dateContext`" in text
        assert "`dateContext.truncated` is true" in text
        assert "count games from the rows you will show" in text


def test_eval_station_materializes_isolated_variant_workspaces() -> None:
    station_text = (REPO / "scripts" / "eval_station.py").read_text("utf-8")
    config = yaml.safe_load(
        (REPO / "evals" / "stations" / "sports_graphs.yaml").read_text()
    )

    assert "TemporaryDirectory" in station_text
    assert "copy_workspace(root, workspace)" in station_text
    assert "apply_variant_overlays(workspace, variant)" in station_text
    assert config["candidate_model"] == "wayfinder/deepseek-v4-pro"
    assert config["judge_model"] == "openai/gpt-5.5"
    assert {v["id"] for v in config["variants"]} == {
        "pre_sports_baseline",
        "sports_current",
        "sports_workpack_challenger",
    }


def test_eval_station_overlay_can_copy_content_from_workspace_file(
    tmp_path: Path,
) -> None:
    station = load_eval_station()
    workspace = tmp_path / "repo"
    source = workspace / ".opencode" / "agents" / "wayfinder-baseline.md"
    target = workspace / ".opencode" / "agents" / "wayfinder.md"
    source.parent.mkdir(parents=True)
    source.write_text("baseline prompt")
    target.write_text("current prompt")

    station.apply_variant_overlays(
        workspace,
        {
            "id": "pre_sports_baseline",
            "overlays": [
                {
                    "path": ".opencode/agents/wayfinder.md",
                    "content_from": ".opencode/agents/wayfinder-baseline.md",
                }
            ],
        },
    )

    assert target.read_text() == "baseline prompt"


def test_eval_station_can_patch_workspace_mcp_url(tmp_path: Path) -> None:
    station = load_eval_station()
    workspace = tmp_path / "repo"
    config_path = workspace / ".opencode" / "opencode.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        __import__("json").dumps(
            {
                "mcp": {
                    "wayfinder": {
                        "type": "remote",
                        "url": "http://127.0.0.1:8010/mcp",
                        "enabled": True,
                    }
                }
            }
        )
    )

    station.configure_workspace_mcp_url(workspace, "http://127.0.0.1:8011/mcp")

    data = __import__("json").loads(config_path.read_text())
    assert data["mcp"]["wayfinder"] == {
        "type": "remote",
        "url": "http://127.0.0.1:8011/mcp",
        "enabled": True,
    }


def test_eval_station_patches_root_opencode_config_used_by_isolated_workspace(
    tmp_path: Path,
) -> None:
    station = load_eval_station()
    workspace = tmp_path / "repo"
    root_config = workspace / "opencode.json"
    nested_config = workspace / ".opencode" / "opencode.json"
    nested_config.parent.mkdir(parents=True)
    root_config.write_text(
        __import__("json").dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "agent": {},
                "instructions": ["AGENTS.md"],
            }
        )
    )
    nested_config.write_text(
        __import__("json").dumps(
            {
                "model": "wayfinder/deepseek-v4-pro",
                "default_agent": "wayfinder",
                "permission": {"wayfinder_core_runner_status": "allow"},
                "mcp": {
                    "wayfinder": {
                        "type": "remote",
                        "url": "http://127.0.0.1:8010/mcp",
                        "enabled": True,
                    }
                },
                "agent": {
                    "wayfinder": {
                        "permission": {
                            "wayfinder_sports_snapshot": "allow",
                            "wayfinder_polymarket_read": "allow",
                        }
                    }
                },
            }
        )
    )

    station.configure_workspace_mcp_url(workspace, "http://127.0.0.1:8011/mcp")

    for config_path in (root_config, nested_config):
        data = __import__("json").loads(config_path.read_text())
        assert data["model"] == station.DEFAULT_CANDIDATE_MODEL
        assert data["default_agent"] == "wayfinder"
        assert data["mcp"]["wayfinder"] == {
            "type": "remote",
            "url": "http://127.0.0.1:8011/mcp",
            "enabled": True,
        }
    root_data = __import__("json").loads(root_config.read_text())
    assert root_data["permission"]["wayfinder_core_runner_status"] == "allow"
    assert root_data["agent"]["wayfinder"]["permission"][
        "wayfinder_sports_snapshot"
    ] == ("allow")
    assert root_data["agent"]["wayfinder"]["permission"][
        "wayfinder_polymarket_read"
    ] == ("allow")


def test_eval_station_mcp_url_flag_and_env_are_documented() -> None:
    station = load_eval_station()
    station_text = (REPO / "scripts" / "eval_station.py").read_text("utf-8")

    assert station.DEFAULT_MCP_URL_ENV == "WAYFINDER_EVAL_MCP_URL"
    assert "--mcp-url" in station_text
    assert "configure_workspace_mcp_url(workspace, mcp_url)" in station_text
    assert "os.environ.get(DEFAULT_MCP_URL_ENV)" in station_text
    assert "--lookup-diagnostics" in station_text
    assert "if args.lookup_diagnostics" in station_text


def test_eval_station_extracts_polymarket_search_calls_from_logs() -> None:
    station = load_eval_station()
    log = (
        "\x1b[0m⚙ \x1b[0mwayfinder_polymarket_read "
        '{"action":"search","query":"openai anthropic ipo first","limit":10}\n'
        "⚙ wayfinder_polymarket_read "
        '{"action":"get_market","market_slug":"will-anthropic-or-openai-ipo-first"}\n'
        "⚙ wayfinder_polymarket_read "
        '{"action":"search","query":"openai anthropic ipo first","limit":10}\n'
        "⚙ wayfinder_polymarket_read "
        '{"action":"search","query":"world cup winner 2026","status":"all","sort":"liquidity"}\n'
    )

    calls = station.extract_polymarket_search_calls(log)

    assert calls == [
        {
            "query": "openai anthropic ipo first",
            "limit": 10,
            "sort": "trending",
            "status": "active",
            "candidate_limit": 10,
        },
        {
            "query": "world cup winner 2026",
            "limit": 10,
            "sort": "liquidity",
            "status": "all",
            "candidate_limit": 10,
        },
    ]


def test_eval_station_formats_lookup_diagnostics_in_report(tmp_path: Path) -> None:
    station = load_eval_station()
    report = {
        "station": "demo",
        "started_at": "2026-06-18T00:00:00Z",
        "candidate_model": "wayfinder/deepseek-v4-pro",
        "judge_model": "openai/gpt-5.5",
        "questions": [
            {
                "id": "q1",
                "text": "question",
                "variants": {
                    "sports_current": {
                        "status": "ok",
                        "duration_seconds": 1.2,
                        "answer_path": str(tmp_path / "answer.md"),
                        "lookup_diagnostics": {
                            "searchCallCount": 1,
                            "replayCount": 1,
                            "newLookupFlowObserved": True,
                            "replays": [
                                {
                                    "query": "world cup winner 2026",
                                    "ok": True,
                                    "mode": "expanded",
                                    "confidence": "high",
                                    "eventHydrations": ["world-cup-winner"],
                                    "directHydrations": [],
                                    "top": [{"slug": "will-france-win"}],
                                }
                            ],
                        },
                    }
                },
                "judgments": [],
            }
        ],
    }
    out = tmp_path / "report.md"

    station.write_markdown_report(report, out)

    text = out.read_text()
    assert "Lookup diagnostics:" in text
    assert "newLookupFlow=yes" in text
    assert "expanded/high" in text
    assert "world cup winner 2026" in text


def test_eval_station_does_not_copy_local_secret_config_into_workspaces() -> None:
    station = load_eval_station()
    station_text = (REPO / "scripts" / "eval_station.py").read_text("utf-8")

    assert "config.json" in station.WORKSPACE_IGNORE_NAMES
    assert ".env" in station.WORKSPACE_IGNORE_NAMES
    assert 'env["WAYFINDER_CONFIG_PATH"] = str(default_config)' in station_text


def test_eval_station_forces_config_api_key_and_clears_local_paths_env(
    tmp_path: Path,
) -> None:
    station = load_eval_station()
    config = {
        "system": {
            "api_base_url": "https://wayfinder.ai/api/v1",
            "api_key": "prod-key",
            "dev_api_base_url": "http://127.0.0.1:8000/api/v1",
            "dev_api_key": "dev-key",
        }
    }
    (tmp_path / "config.json").write_text(__import__("json").dumps(config))
    env = {
        "WAYFINDER_CONFIG_PATH": "/tmp/old-config.json",
        "WAYFINDER_CONFIG": "/tmp/other-config.json",
        "WAYFINDER_API_KEY": "old-key",
        "WAYFINDER_PATHS_API_URL": "http://127.0.0.1:8000",
    }

    station.force_eval_wayfinder_api_env(tmp_path, env)

    assert env["WAYFINDER_CONFIG_PATH"] == str(tmp_path / "config.json")
    assert env["WAYFINDER_API_KEY"] == "prod-key"
    assert "WAYFINDER_API_BASE_URL" not in env
    assert "WAYFINDER_CONFIG" not in env
    assert "WAYFINDER_PATHS_API_URL" not in env


def test_eval_station_workspace_copy_keeps_live_runtime_assets(tmp_path: Path) -> None:
    station = load_eval_station()
    source = tmp_path / "source"
    destination = tmp_path / "dest"

    for path, text in {
        ".claude/skills/using-sports-data/SKILL.md": "sports skill",
        ".opencode/skills/quant-desk/path/opencode.json": "{}",
        ".opencode/agents/wayfinder.md": "agent",
        "scripts/eval_station.py": "script",
        "wayfinder_paths/quant/event_sim.py": "sim",
        ".wayfinder_runs/README.md": "runs readme",
        ".wayfinder_runs/sports/futures_worldcup_outright.json": "{}",
        ".wayfinder_runs/evals/round_questions.txt": "question",
        ".wayfinder_runs/packs/index.jsonl": "old pack index",
        ".wayfinder_runs/packs/sports/surface/old.json": "{}",
        ".wayfinder_runs/eval_station/old/report.json": "{}",
        ".wayfinder_runs/.scratch/tmp.py": "scratch",
        ".wayfinder_runs/scratch/tmp.py": "scratch",
        "config.json": "{}",
        ".env": "export SECRET=1",
    }.items():
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    (source / ".git").mkdir()
    (source / ".venv").mkdir()

    station.copy_workspace(source, destination)

    for kept in (
        ".claude/skills/using-sports-data/SKILL.md",
        ".opencode/skills/quant-desk/path/opencode.json",
        ".opencode/agents/wayfinder.md",
        "scripts/eval_station.py",
        "wayfinder_paths/quant/event_sim.py",
        ".wayfinder_runs/README.md",
        ".wayfinder_runs/sports/futures_worldcup_outright.json",
        ".wayfinder_runs/evals/round_questions.txt",
    ):
        assert (destination / kept).exists()

    for omitted in (
        "config.json",
        ".env",
        ".git",
        ".venv",
        ".wayfinder_runs/eval_station",
        ".wayfinder_runs/.scratch",
        ".wayfinder_runs/scratch",
        ".wayfinder_runs/packs",
    ):
        assert not (destination / omitted).exists()


def test_eval_station_initializes_empty_pack_store(tmp_path: Path) -> None:
    station = load_eval_station()
    workspace = tmp_path / "repo"
    old_pack = (
        workspace / ".wayfinder_runs" / "packs" / "sports" / "surface" / "old.json"
    )
    old_pack.parent.mkdir(parents=True)
    old_pack.write_text("{}")

    station.initialize_pack_store(workspace)

    assert not old_pack.exists()
    assert (workspace / ".wayfinder_runs" / "packs" / "index.jsonl").exists()
    assert (workspace / ".wayfinder_runs" / "packs" / "index.jsonl").read_text() == ""


def test_eval_station_challenger_overlay_mentions_workpacks() -> None:
    config = yaml.safe_load(
        (REPO / "evals" / "stations" / "sports_graphs.yaml").read_text()
    )
    challenger = next(
        v for v in config["variants"] if v["id"] == "sports_workpack_challenger"
    )
    overlay_text = "\n".join(
        (REPO / str(overlay["content_from"])).read_text("utf-8")
        for overlay in challenger["overlays"]
        if overlay.get("content_from")
    )

    for needle in (
        "surfacePack",
        "contextPack",
        "featurePack",
        "analysisPack",
        "decisionPack",
        "validationReport",
        "SPORTS_SCAN",
        "rehydrate",
    ):
        assert needle in overlay_text


def test_primary_eval_variants_have_workspace_write_permission() -> None:
    for path in (
        REPO / ".opencode" / "agents" / "wayfinder.md",
        REPO / ".opencode" / "agents" / "wayfinder-baseline.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "wayfinder.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_workpack_challenger"
        / "wayfinder.md",
    ):
        frontmatter = path.read_text("utf-8").split("---", 2)[1]
        assert "write: allow" in frontmatter, path


def test_eval_station_current_variant_uses_snapshot_overlays() -> None:
    config = yaml.safe_load(
        (REPO / "evals" / "stations" / "sports_graphs.yaml").read_text()
    )
    current = next(v for v in config["variants"] if v["id"] == "sports_current")

    assert all("content_from" in overlay for overlay in current["overlays"])
    assert all(
        str(overlay["content_from"]).startswith("evals/agent_overlays/sports_current/")
        for overlay in current["overlays"]
    )


def test_eval_station_uses_same_prompt_for_all_variants() -> None:
    config = yaml.safe_load(
        (REPO / "evals" / "stations" / "sports_graphs.yaml").read_text()
    )

    assert len({question["text"] for question in config["questions"]}) == len(
        config["questions"]
    )
    for question in config["questions"]:
        text = question["text"].lower()
        assert "workpack" not in text
        assert "packref" not in text


def test_eval_station_uses_mixed_market_prompt_set() -> None:
    config = yaml.safe_load(
        (REPO / "evals" / "stations" / "sports_graphs.yaml").read_text()
    )

    questions = {
        question["id"]: question["text"].lower() for question in config["questions"]
    }

    assert set(questions) == {
        "anthropic_openai_ipo_edge",
        "hype_spcx_short_setup",
        "world_cup_countries_edge",
        "rays_nationals_game_lines",
    }
    assert (
        questions["anthropic_openai_ipo_edge"]
        == "do we think openai or anthropic will ipo first?"
    )
    assert "anthropic-or-openai-ipo-first" not in questions["anthropic_openai_ipo_edge"]
    assert "hype and spcx" in questions["hype_spcx_short_setup"]
    assert "coutnries to win the world cup" in questions["world_cup_countries_edge"]
    assert "rays nationls game tomorrow" in questions["rays_nationals_game_lines"]


def test_smoke_mlb_tonight_station_is_minimal_three_way() -> None:
    config = yaml.safe_load(
        (REPO / "evals" / "stations" / "smoke_mlb_tonight.yaml").read_text()
    )

    assert config["name"] == "smoke_mlb_tonight"
    assert [question["id"] for question in config["questions"]] == ["mlb_games_tonight"]
    assert config["questions"][0]["text"] == "what mlb games are on tonight?"
    assert [variant["id"] for variant in config["variants"]] == [
        "pre_sports_baseline",
        "sports_current",
        "sports_workpack_challenger",
    ]
    assert config["judge_pairs"] == [
        ["pre_sports_baseline", "sports_current"],
        ["sports_current", "sports_workpack_challenger"],
        ["pre_sports_baseline", "sports_workpack_challenger"],
    ]


def test_eval_judge_rubric_supports_mixed_market_domains() -> None:
    rubric = (REPO / "scripts" / "eval_sports_ab_judge.md").read_text("utf-8")

    for needle in (
        "market and sports edge answer quality",
        "IPO prediction markets",
        "HYPE/SPCX short setup",
        "price-action thesis",
        "bounded historical analog/event-study",
        "forward horizons, sample size",
        "`adjacent / needs verification`",
        "World Cup countries/outrights",
        "Specific MLB game lines",
        "bounded grounding pass",
        "Compact board-first surfaces",
        "full payout matrices only matter for\n   shortlisted or non-standard markets",
        "settlement, exit-before-close/mark-to-market, relative value, or\n   arb/conversion",
        "binary EV on partial 50/50",
        "do not require a full payout matrix inline, a bespoke\n  script, or a backtest",
        "progress-checkpoint\n  answers",
        "trailing `<userSuggestions>` block",
    ):
        assert needle in rubric


def test_market_intel_trade_setup_prompt_overlays() -> None:
    primary_paths = [
        REPO / ".opencode" / "agents" / "wayfinder.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "wayfinder.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_workpack_challenger"
        / "wayfinder.md",
    ]
    for path in primary_paths:
        text = path.read_text("utf-8")
        assert "price action has been wild" in text
        assert "sample size" in text
        assert "forward horizons" in text
        assert "adjacent / needs verification" in text

    research_paths = [
        REPO / ".opencode" / "agents" / "wayfinder-research.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "wayfinder-research.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_workpack_challenger"
        / "wayfinder-research.md",
    ]
    for path in research_paths:
        text = path.read_text("utf-8")
        assert "wild price action" in text
        assert "bounded historical analog / event-study" in text
        assert "raw row dumps" in text
        assert "adjacent / needs verification" in text

    quant_paths = [
        REPO / ".opencode" / "agents" / "wayfinder-quant.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "wayfinder-quant.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_workpack_challenger"
        / "wayfinder-quant.md",
    ]
    for path in quant_paths:
        text = path.read_text("utf-8")
        assert "Market-intel historical analog / event-study" in text
        assert "Default forward horizons" in text
        assert "sample size" in text
        assert "Do not overfit filters" in text


def test_simple_prediction_market_fast_edge_prompt_guards() -> None:
    primary_paths = [
        REPO / ".opencode" / "agents" / "wayfinder.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "wayfinder.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_workpack_challenger"
        / "wayfinder.md",
    ]
    for path in primary_paths:
        text = path.read_text("utf-8")
        assert "FAST_EDGE" in text
        assert "Do **not** run local scripts" in text
        assert "`WATCH`/`NEEDS_REPAIR`" in text
        assert "Never emit a progress checkpoint" in text

    research_paths = [
        REPO / ".opencode" / "agents" / "wayfinder-research.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "wayfinder-research.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_workpack_challenger"
        / "wayfinder-research.md",
    ]
    for path in research_paths:
        text = path.read_text("utf-8")
        assert "simple one-market non-sports edge check" in text
        assert "Do not write or run scripts" in text
        assert "do not continue into repair/debug loops" in text
        assert "Never return a progress checkpoint" in text
        assert "Never return a progress checkpoint, `<userSuggestions>`" not in text

    quant_paths = [
        REPO / ".opencode" / "agents" / "wayfinder-quant.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "wayfinder-quant.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_workpack_challenger"
        / "wayfinder-quant.md",
    ]
    for path in quant_paths:
        text = path.read_text("utf-8")
        assert "do not write/run generated scripts" in text
        assert "Script repair budget" in text
        assert "Do not inspect helper source" in text


def test_eval_overlays_include_chart_repair_and_source_quality_guards() -> None:
    primary_paths = [
        REPO / ".opencode" / "agents" / "wayfinder.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "wayfinder.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_workpack_challenger"
        / "wayfinder.md",
    ]
    for path in primary_paths:
        text = path.read_text("utf-8")
        assert "wayfinder_visual_add_workspace_chart_series" in text
        assert "include_health=true" in text
        assert "provider-confirmed replacement" in text
        assert "Verify the returned `chart_validation`" in text

    research_paths = [
        REPO / ".opencode" / "agents" / "wayfinder-research.md",
        REPO / "evals" / "agent_overlays" / "sports_current" / "wayfinder-research.md",
        REPO
        / "evals"
        / "agent_overlays"
        / "sports_workpack_challenger"
        / "wayfinder-research.md",
    ]
    for path in research_paths:
        text = path.read_text("utf-8")
        assert (
            "`provider_api`, `primary_source`, `fetched_article`, `search_snippet`, or `social`"
            in text
        )
        assert (
            '"sourceType": "provider_api|primary_source|fetched_article|search_snippet|social"'
            in text
        )
        assert (
            "Only `provider_api` and `primary_source` claims may be placed in `verifiedMetrics`"
            in text
        )


def test_eval_judge_prompt_conflict_is_removed() -> None:
    rubric = (REPO / "scripts" / "eval_sports_ab_judge.md").read_text("utf-8")
    judge = (REPO / ".opencode" / "agents" / "wayfinder-eval-judge.md").read_text(
        "utf-8"
    )

    assert "Grounded judge mode" in rubric
    assert "read-only validation tools" in rubric
    assert "Do not run a full competing model" in rubric
    assert "Judge ONLY from the answer texts" not in rubric
    assert "do not use\ntools" not in rubric.lower()

    assert "Ground Yourself" in judge
    assert "Then STOP researching" in judge
    assert "Do not use runtime metadata" in judge
    assert "compact executable board" in judge
    assert "resolution profile" in judge
    assert "binary probability/EV math" in judge


def test_eval_judge_fallback_is_explicit_only() -> None:
    wrapper = (REPO / "scripts" / "eval_judge.sh").read_text("utf-8")
    agent = (REPO / ".opencode" / "agents" / "wayfinder-eval-judge.md").read_text(
        "utf-8"
    )

    assert "JUDGE_ALLOW_FALLBACK" in wrapper
    assert 'JUDGE_ATTEMPTS="${JUDGE_ATTEMPTS:-1}"' in wrapper
    assert "exit 1" in wrapper
    assert "JUDGE_ALLOW_FALLBACK=1" in wrapper
    assert "degrades to `JUDGE_FALLBACK_MODEL`" not in agent
    assert "unless fallback is explicitly enabled" in agent


def test_eval_judge_has_read_only_validation_tools() -> None:
    judge = (REPO / ".opencode" / "agents" / "wayfinder-eval-judge.md").read_text(
        "utf-8"
    )

    for allowed in (
        "wayfinder_polymarket_read: allow",
        "wayfinder_hyperliquid_search_hip4: allow",
        "wayfinder_hyperliquid_search_market: allow",
        "wayfinder_hyperliquid_search_mid_prices: allow",
        "wayfinder_sports_snapshot: allow",
        "wayfinder_core_web_search: allow",
        "wayfinder_research_*: allow",
    ):
        assert allowed in judge

    for forbidden in (
        "wayfinder_polymarket_place",
        "wayfinder_hyperliquid_place",
        "wayfinder_core_run_script: allow",
        "write: allow",
    ):
        assert forbidden not in judge


def test_primary_prompts_use_actual_opencode_tool_names() -> None:
    for path in (
        REPO / ".opencode" / "agents" / "wayfinder.md",
        REPO / ".opencode" / "agents" / "wayfinder-baseline.md",
    ):
        text = path.read_text("utf-8")
        assert "`polymarket_read" not in text
        assert " / `polymarket_read`" not in text
        assert "wayfinder_polymarket_read" in text
