"""Solidity compilation via py-solc-x with OpenZeppelin import support.

This module compiles a single Solidity source string (plus any OpenZeppelin
imports) using solc standard JSON input. For verification, the generated
standard-json input includes all imported OpenZeppelin sources so block
explorers can reproduce the exact bytecode without access to local files.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from posixpath import normpath
from typing import Any

from loguru import logger
from solcx import (
    compile_standard,
    get_installed_solc_versions,
    install_solc,
)

from wayfinder_paths.core.config import _project_root as _config_project_root

SOLC_VERSION = "0.8.26"
OZ_CONTRACTS_VERSION = "5.4.0"
_SOURCE_FILENAME = "Contract.sol"


def ensure_solc_installed(version: str = SOLC_VERSION) -> None:
    installed = [str(v) for v in get_installed_solc_versions()]
    if version not in installed:
        logger.info(f"Installing solc {version}...")
        install_solc(version)


def ensure_oz_installed(project_root: str | None = None) -> str:
    """Ensure ``@openzeppelin/contracts`` is available in an ignored cache dir.

    We pin to an exact OpenZeppelin Contracts version so compilation and
    Etherscan verification are deterministic.

    We intentionally **do not** install into the repository root, because that
    would create an unignored ``node_modules/`` tree and potentially a
    ``package-lock.json``. Instead we install into:

        ``<repo>/.cache/solidity/openzeppelin-<version>/node_modules/@openzeppelin/contracts``

    Returns the cached ``node_modules/`` directory path.
    """
    root = (
        Path(project_root) if project_root else (_config_project_root() or Path.cwd())
    )
    cache_root = root / ".cache" / "solidity" / f"openzeppelin-{OZ_CONTRACTS_VERSION}"
    node_modules = cache_root / "node_modules"
    oz_path = node_modules / "@openzeppelin" / "contracts"

    if oz_path.is_dir():
        return str(node_modules)

    cache_root.mkdir(parents=True, exist_ok=True)

    logger.info("Installing @openzeppelin/contracts via npm (cache)...")
    # Use --prefix so npm writes into the cache directory.
    # Avoid --save/--save-dev to keep this out of the repo.
    subprocess.run(
        [
            "npm",
            "install",
            "--prefix",
            str(cache_root),
            f"@openzeppelin/contracts@{OZ_CONTRACTS_VERSION}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    if not oz_path.is_dir():
        raise RuntimeError(
            f"npm install succeeded but {oz_path} not found. "
            "Check your npm/node installation."
        )

    return str(node_modules)


_IMPORT_RE = re.compile(
    r"""^\s*import\s+(?:[^;]*?\s+from\s+)?["']([^"']+)["']\s*;""",
    flags=re.MULTILINE,
)


def _strip_solidity_comments(source: str) -> str:
    # Remove /* ... */ then // ...
    no_block = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"//.*?$", "", no_block, flags=re.MULTILINE)


def _find_imports(source: str) -> list[str]:
    stripped = _strip_solidity_comments(source)
    return [m.group(1).strip() for m in _IMPORT_RE.finditer(stripped) if m.group(1)]


def _load_dependency_source(*, node_modules: Path, key: str) -> str:
    # Keys like "@openzeppelin/contracts/token/ERC20/ERC20.sol"
    path = node_modules / key
    if not path.is_file():
        raise FileNotFoundError(f"Import not found: {key} (looked for {path})")
    return path.read_text(encoding="utf-8", errors="replace")


def collect_sources(
    source_code: str,
    *,
    source_filename: str = _SOURCE_FILENAME,
    project_root: str | None = None,
) -> dict[str, str]:
    """Return a ``{source_key: content}`` map suitable for standard-json-input.

    The keys are stable, portable paths (e.g. ``Contract.sol`` and
    ``@openzeppelin/contracts/...``), so the resulting compiler input can be
    sent to Etherscan for verification.
    """
    root = (
        Path(project_root) if project_root else (_config_project_root() or Path.cwd())
    )

    sources: dict[str, str] = {source_filename: source_code}
    queue: list[str] = [source_filename]

    node_modules: Path | None = None

    while queue:
        cur_key = queue.pop()
        cur_source = sources[cur_key]

        for imp in _find_imports(cur_source):
            dep_key = imp.strip()

            # Resolve relative imports (e.g. "../IERC20.sol") within OZ files
            if dep_key.startswith(".") and cur_key.startswith("@openzeppelin/"):
                parent = "/".join(cur_key.split("/")[:-1])
                dep_key = normpath(f"{parent}/{dep_key}")

            if dep_key in sources:
                continue

            if dep_key.startswith("@openzeppelin/"):
                if node_modules is None:
                    node_modules = Path(ensure_oz_installed(str(root)))
                sources[dep_key] = _load_dependency_source(
                    node_modules=node_modules, key=dep_key
                )
                queue.append(dep_key)
                continue

            raise RuntimeError(
                f"Unsupported import '{imp}' in '{cur_key}'. "
                "Only @openzeppelin/* imports are supported."
            )

    return sources


def _extract_bytecode_from_artifact(artifact: dict[str, Any]) -> str:
    evm = artifact.get("evm", {})
    if not isinstance(evm, dict):
        return ""
    bytecode = evm.get("bytecode", {})
    if not isinstance(bytecode, dict):
        return ""
    obj = bytecode.get("object") or ""
    result = str(obj)
    if result and not result.startswith("0x"):
        result = "0x" + result
    return result


def _contracts_from_output(
    output: dict[str, Any],
    *,
    source_filename: str = _SOURCE_FILENAME,
) -> dict[str, Any]:
    contracts = (output.get("contracts") or {}).get(source_filename) or {}
    return contracts if isinstance(contracts, dict) else {}


def extract_abi_and_bytecode(
    output: dict[str, Any],
    *,
    contract_name: str,
    source_filename: str = _SOURCE_FILENAME,
) -> tuple[list[dict[str, Any]], str]:
    """Extract ABI + bytecode for a compiled contract from solc standard output."""
    contracts = _contracts_from_output(output, source_filename=source_filename)
    if contract_name not in contracts:
        available = list(contracts.keys())
        raise ValueError(
            f"Contract '{contract_name}' not found in compilation output. "
            f"Available: {available}"
        )

    artifact = contracts[contract_name]
    if not isinstance(artifact, dict):
        raise ValueError(f"Contract '{contract_name}' artifact is not a dict")

    abi_raw = artifact.get("abi", [])
    abi: list[dict[str, Any]] = (
        [i for i in abi_raw if isinstance(i, dict)] if isinstance(abi_raw, list) else []
    )

    bytecode = _extract_bytecode_from_artifact(artifact)
    return abi, bytecode


def compile_solidity(
    source_code: str,
    *,
    contract_name: str | None = None,
    project_root: str | None = None,
    optimize: bool = True,
    optimize_runs: int = 200,
) -> dict[str, dict[str, Any]]:
    """Compile Solidity source code (root contracts only).

    Returns ``{contract_name: {"abi": [...], "bytecode": "0x..."}}`` for
    contracts defined in the root ``source_filename`` (default: ``Contract.sol``).
    If *contract_name* is given, validates it exists in the output.
    """
    compiled = compile_solidity_standard_json(
        source_code,
        source_filename=_SOURCE_FILENAME,
        project_root=project_root,
        optimize=optimize,
        optimize_runs=optimize_runs,
    )

    output = compiled["output"]
    contracts = _contracts_from_output(output, source_filename=_SOURCE_FILENAME)

    results: dict[str, dict[str, Any]] = {}
    for name, artifact in contracts.items():
        if not isinstance(artifact, dict):
            continue
        abi_raw = artifact.get("abi", [])
        abi = [i for i in abi_raw if isinstance(i, dict)] if isinstance(abi_raw, list) else []
        bytecode = _extract_bytecode_from_artifact(artifact)
        results[str(name)] = {"abi": abi, "bytecode": bytecode}

    if contract_name and contract_name not in results:
        available = list(results.keys())
        raise ValueError(
            f"Contract '{contract_name}' not found in compilation output. "
            f"Available: {available}"
        )

    return results


def compile_solidity_standard_json(
    source_code: str,
    *,
    source_filename: str = _SOURCE_FILENAME,
    project_root: str | None = None,
    optimize: bool = True,
    optimize_runs: int = 200,
) -> dict[str, Any]:
    """Compile using standard JSON input for Etherscan verification.

    Returns ``{"input": <standard_json_input>, "output": <compiler_output>}``.
    The ``input`` dict can be submitted directly to Etherscan's
    ``solidity-standard-json-input`` verification mode.
    """
    ensure_solc_installed()

    sources = collect_sources(
        source_code, source_filename=source_filename, project_root=project_root
    )

    standard_input: dict[str, Any] = {
        "language": "Solidity",
        "sources": {k: {"content": v} for k, v in sources.items()},
        "settings": {
            "optimizer": {"enabled": optimize, "runs": optimize_runs},
            "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object"]}},
        },
    }

    output = compile_standard(standard_input, solc_version=SOLC_VERSION)

    errors = output.get("errors", [])
    real_errors = [e for e in errors if e.get("severity") == "error"]
    if real_errors:
        msgs = [e.get("formattedMessage", e.get("message", "")) for e in real_errors]
        raise RuntimeError("Solidity compilation errors:\n" + "\n".join(msgs))

    return {"input": standard_input, "output": output}
