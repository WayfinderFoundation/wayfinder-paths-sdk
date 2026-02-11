#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent


def _confirm(prompt: str, *, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{prompt} [{suffix}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("Please answer 'y' or 'n'.")


def _run(cmd: list[str]) -> None:
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    parsed = json.loads(path.read_text())
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{path} must be a JSON object at the top level.")
    return parsed


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def _require_python312() -> None:
    if sys.version_info[:2] == (3, 12):
        return
    python312 = shutil.which("python3.12")
    if python312:
        os.execv(python312, [python312, *sys.argv])
    raise RuntimeError(
        "Python 3.12 is required. Install it and re-run with `python3.12 scripts/setup.py`."
    )


def _find_poetry() -> str | None:
    if poetry := shutil.which("poetry"):
        return poetry
    candidates = [
        Path.home() / ".local" / "bin" / "poetry",
        Path.home() / ".poetry" / "bin" / "poetry",
        Path.home() / "AppData" / "Roaming" / "Python" / "Scripts" / "poetry",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _install_poetry(*, non_interactive: bool) -> str:
    if non_interactive:
        raise RuntimeError(
            "Poetry is not installed. Install it first (https://python-poetry.org/docs/#installation) "
            "or re-run without --non-interactive."
        )
    if not _confirm("Poetry not found. Install it now?"):
        raise RuntimeError("Poetry is required to continue.")

    if shutil.which("pipx"):
        _run(["pipx", "install", "poetry"])
    else:
        print("Downloading Poetry installer...")
        script = urllib.request.urlopen("https://install.python-poetry.org").read()
        print("Running Poetry installer...")
        subprocess.run([sys.executable, "-"], input=script, check=True)

    if not (poetry := _find_poetry()):
        raise RuntimeError(
            "Poetry was installed, but `poetry` is not on PATH.\n"
            "Try restarting your terminal, or add ~/.local/bin to PATH."
        )
    return poetry


def _ensure_config(*, api_key: str | None) -> None:
    config_path = REPO_ROOT / "config.json"
    template_path = REPO_ROOT / "config.example.json"

    config = _read_json(config_path) or _read_json(template_path) or {}
    system = config.get("system", {})
    if not isinstance(system, dict):
        system = {}

    if api_key:
        system["api_key"] = api_key
    system.setdefault("api_base_url", "https://wayfinder.ai/api")
    config["system"] = system

    if "strategy" not in config:
        template = _read_json(template_path) or {}
        if isinstance(template.get("strategy"), dict):
            config["strategy"] = template["strategy"]

    _write_json(config_path, config)
    print("Wrote config.json")


def _get_wallet_labels() -> set[str]:
    config = _read_json(REPO_ROOT / "config.json") or {}
    wallets = config.get("wallets", [])
    if not isinstance(wallets, list):
        return set()
    return {w.get("label") for w in wallets if isinstance(w, dict) and w.get("label")}


def _discover_strategies() -> list[str]:
    strategies_dir = REPO_ROOT / "wayfinder_paths" / "strategies"
    if not strategies_dir.exists():
        return []
    return sorted(
        d.name for d in strategies_dir.iterdir() if (d / "strategy.py").exists()
    )


def _ensure_wallets(poetry: str, *, non_interactive: bool, mnemonic: bool) -> None:
    existing = _get_wallet_labels()

    if "main" not in existing:
        if non_interactive or _confirm("Create a local dev wallet (label: main)?"):
            cmd = [poetry, "run", "python", "scripts/make_wallets.py", "-n", "1"]
            if mnemonic:
                cmd.append("--mnemonic")
            _run(cmd)
            existing = _get_wallet_labels()

    missing = [s for s in _discover_strategies() if s not in existing]
    if missing:
        print(f"Creating wallets for {len(missing)} strategies: {', '.join(missing)}")
        for name in missing:
            cmd = [poetry, "run", "python", "scripts/make_wallets.py", "--label", name]
            if mnemonic:
                cmd.append("--mnemonic")
            _run(cmd)


def _ensure_mcp_json() -> None:
    mcp_path = REPO_ROOT / ".mcp.json"
    mcp = _read_json(mcp_path)
    if not mcp:
        raise RuntimeError("Missing .mcp.json (expected in repo root).")

    wayfinder = mcp.get("mcpServers", {}).get("wayfinder")
    if not isinstance(wayfinder, dict):
        raise RuntimeError(".mcp.json is missing 'mcpServers.wayfinder'.")

    wayfinder["command"] = "poetry"
    _write_json(mcp_path, mcp)
    print("Updated .mcp.json")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap Wayfinder Paths (Poetry + config.json + Claude MCP)."
    )
    parser.add_argument("--api-key", help="Wayfinder API key")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail instead of prompting for installs / secrets.",
    )
    parser.add_argument(
        "--mnemonic",
        action="store_true",
        help=(
            "Generate and use a BIP-39 mnemonic for deterministic local wallets. "
            "This persists wallet_mnemonic in config.json and derives MetaMask-style EVM accounts."
        ),
    )
    args = parser.parse_args()

    os.chdir(REPO_ROOT)
    _require_python312()

    api_key = (args.api_key or os.environ.get("WAYFINDER_API_KEY") or "").strip()
    if not api_key and not args.non_interactive:
        try:
            api_key = getpass.getpass("Enter your Wayfinder API key (wk_...): ").strip()
        except EOFError:
            # Non-interactive terminal (e.g., Claude Code) - skip API key prompt
            print("Skipping API key prompt (non-interactive terminal).")
            print("You can add your API key to config.json later under system.api_key")
            api_key = ""

    poetry = _find_poetry() or _install_poetry(non_interactive=args.non_interactive)
    _run([poetry, "install"])

    _ensure_config(api_key=api_key or None)
    _ensure_mcp_json()
    _ensure_wallets(poetry, non_interactive=args.non_interactive, mnemonic=args.mnemonic)

    config = _read_json(REPO_ROOT / "config.json") or {}
    if not config.get("system", {}).get("api_key"):
        print(
            "Note: system.api_key is not set. Update config.json or export WAYFINDER_API_KEY."
        )

    print("\nSetup complete.")
    print(
        "- Open Claude Code in this repo and enable the project MCP server when prompted."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
