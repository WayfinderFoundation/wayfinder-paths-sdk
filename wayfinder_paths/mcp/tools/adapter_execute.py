from __future__ import annotations

import asyncio
import importlib
import inspect
from pathlib import Path
from typing import Any

from eth_account import Account

from wayfinder_paths.core.config import CONFIG
from wayfinder_paths.mcp.preview import build_adapter_execute_preview
from wayfinder_paths.mcp.state.profile_store import WalletProfileStore
from wayfinder_paths.mcp.utils import (
    err,
    find_wallet_by_label,
    ok,
    read_yaml,
    repo_root,
)

_FORBIDDEN_KWARGS = frozenset(
    {
        "config",
        "private_key",
        "private_key_hex",
        "mnemonic",
        "seed",
        "seed_phrase",
        "sign_callback",
        "signing_callback",
        "main_wallet_signing_callback",
        "strategy_wallet_signing_callback",
    }
)


def _make_sign_callback(private_key: str):
    account = Account.from_key(private_key)

    async def sign_callback(transaction: dict) -> bytes:
        signed = account.sign_transaction(transaction)
        return signed.raw_transaction

    return sign_callback


def _import_entrypoint(entrypoint: str) -> type:
    module_path, symbol = str(entrypoint).rsplit(".", 1)

    last_exc: Exception | None = None
    for candidate in (module_path, f"wayfinder_paths.{module_path}"):
        try:
            mod = importlib.import_module(candidate)
            obj = getattr(mod, symbol)
            if not isinstance(obj, type):
                raise TypeError(f"Entry point {entrypoint} is not a class")
            return obj
        except Exception as exc:
            last_exc = exc
    raise ImportError(f"Failed to import entrypoint: {entrypoint}") from last_exc


def _adapter_dir(adapter: str) -> Path:
    return repo_root() / "wayfinder_paths" / "adapters" / adapter


def _extract_mcp_methods(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = manifest.get("mcp_methods")
    if raw is None and isinstance(manifest.get("mcp"), dict):
        raw = (manifest.get("mcp") or {}).get("methods")

    if raw is None:
        return {}

    methods: dict[str, dict[str, Any]] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                methods[item.strip()] = {"name": item.strip()}
                continue
            if isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str) and name.strip():
                    methods[name.strip()] = dict(item)
    return methods


def _annotate(
    *,
    address: str,
    label: str | None,
    protocol: str,
    action: str,
    status: str,
    chain_id: int | None,
    details: dict[str, Any] | None,
) -> None:
    store = WalletProfileStore.default()
    store.annotate_safe(
        address=address,
        label=label,
        protocol=protocol,
        action=action,
        tool="adapter_execute",
        status=status,
        chain_id=chain_id,
        details=details,
    )


def _wallet_from_label(label: str) -> dict[str, Any] | None:
    want = str(label or "").strip()
    if not want:
        return None
    return find_wallet_by_label(want)


def _resolve_wallets(
    *,
    wallet_label: str | None,
    main_wallet_label: str | None,
    strategy_wallet_label: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    # Back-compat: wallet_label sets both main+strategy.
    main_label = str(main_wallet_label or wallet_label or "").strip() or None
    strat_label = str(strategy_wallet_label or wallet_label or "").strip() or None
    main_wallet = _wallet_from_label(main_label) if main_label else None
    strat_wallet = _wallet_from_label(strat_label) if strat_label else None
    label_used = strat_label or main_label
    return main_wallet, strat_wallet, label_used


def _build_config(
    *,
    main_wallet: dict[str, Any] | None,
    strategy_wallet: dict[str, Any] | None,
    config_overrides: dict[str, Any] | None,
) -> dict[str, Any]:
    cfg = dict(CONFIG)
    if config_overrides:
        cfg.update(config_overrides)

    if main_wallet:
        cfg["main_wallet"] = dict(main_wallet)
    if strategy_wallet:
        cfg["strategy_wallet"] = dict(strategy_wallet)

    return cfg


def _sign_cb_from_wallet(wallet: dict[str, Any] | None):
    if not wallet:
        return None
    pk = wallet.get("private_key") or wallet.get("private_key_hex")
    if not pk:
        return None
    return _make_sign_callback(str(pk))


def _init_kwargs_for_adapter(
    adapter_class: type,
    *,
    config: dict[str, Any],
    main_sign_cb,
    strat_sign_cb,
    extra_init_kwargs: dict[str, Any] | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"config": config}
    if extra_init_kwargs:
        kwargs.update(extra_init_kwargs)

    try:
        sig = inspect.signature(adapter_class.__init__)
    except (ValueError, TypeError):
        return kwargs

    for name, param in sig.parameters.items():
        if name in {"self", "config"}:
            continue
        if name in kwargs:
            continue

        if name.endswith("_signing_callback") or name in {
            "sign_callback",
            "signing_callback",
        }:
            if name.startswith("main_"):
                cb = main_sign_cb
            elif name.startswith("strategy_"):
                cb = strat_sign_cb
            else:
                cb = strat_sign_cb

            if cb is not None or param.default is not inspect.Parameter.empty:
                kwargs[name] = cb

    return kwargs


async def adapter_execute(
    *,
    adapter: str,
    method: str,
    wallet_label: str | None = None,
    main_wallet_label: str | None = None,
    strategy_wallet_label: str | None = None,
    args: list[Any] | None = None,
    kwargs: dict[str, Any] | None = None,
    config_overrides: dict[str, Any] | None = None,
    adapter_init_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    adapter_name = str(adapter or "").strip()
    if not adapter_name:
        return err("invalid_request", "adapter is required")

    method_name = str(method or "").strip()
    if not method_name:
        return err("invalid_request", "method is required")
    if method_name.startswith("_") or "__" in method_name:
        return err("invalid_request", "method must not be private/dunder")

    call_args = args or []
    call_kwargs = kwargs or {}
    if not isinstance(call_args, list):
        return err("invalid_request", "args must be a list")
    if not isinstance(call_kwargs, dict):
        return err("invalid_request", "kwargs must be an object")

    bad_keys = sorted(k for k in call_kwargs.keys() if str(k) in _FORBIDDEN_KWARGS)
    if bad_keys:
        return err(
            "invalid_request",
            "kwargs contains forbidden keys",
            {"forbidden_keys": bad_keys},
        )

    target = _adapter_dir(adapter_name)
    if not target.exists():
        return err("not_found", f"Unknown adapter: {adapter_name}")

    manifest_path = target / "manifest.yaml"
    if not manifest_path.exists():
        return err("not_found", f"Missing manifest.yaml for adapter: {adapter_name}")

    manifest = read_yaml(manifest_path)
    entrypoint = manifest.get("entrypoint")
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        return err("invalid_manifest", "manifest.entrypoint is required")

    allow = _extract_mcp_methods(manifest)
    if not allow:
        return err(
            "not_supported",
            f"Adapter '{adapter_name}' has no mcp_methods allowlist in manifest.yaml",
        )
    if method_name not in allow:
        return err(
            "not_allowed",
            f"Method '{method_name}' is not allowlisted for adapter '{adapter_name}'",
            {"allowed_methods": sorted(allow.keys())},
        )

    tool_input = {
        "adapter": adapter_name,
        "method": method_name,
        "wallet_label": wallet_label,
        "main_wallet_label": main_wallet_label,
        "strategy_wallet_label": strategy_wallet_label,
        "args": call_args,
        "kwargs": call_kwargs,
    }
    preview_obj = build_adapter_execute_preview(tool_input)
    preview_text = str(preview_obj.get("summary") or "").strip()

    main_wallet, strat_wallet, label_used = _resolve_wallets(
        wallet_label=wallet_label,
        main_wallet_label=main_wallet_label,
        strategy_wallet_label=strategy_wallet_label,
    )
    if (main_wallet_label or wallet_label) and not main_wallet:
        want = str(main_wallet_label or wallet_label)
        return err("not_found", f"Unknown wallet_label: {want}")
    if (strategy_wallet_label or wallet_label) and not strat_wallet:
        want = str(strategy_wallet_label or wallet_label)
        return err("not_found", f"Unknown wallet_label: {want}")

    cfg = _build_config(
        main_wallet=main_wallet,
        strategy_wallet=strat_wallet,
        config_overrides=config_overrides,
    )
    main_sign_cb = _sign_cb_from_wallet(main_wallet)
    strat_sign_cb = _sign_cb_from_wallet(strat_wallet)

    try:
        adapter_class = _import_entrypoint(str(entrypoint))
    except Exception as exc:
        return err("import_error", str(exc))

    init_kwargs = _init_kwargs_for_adapter(
        adapter_class,
        config=cfg,
        main_sign_cb=main_sign_cb,
        strat_sign_cb=strat_sign_cb,
        extra_init_kwargs=adapter_init_kwargs,
    )

    adapter_obj = None
    try:
        adapter_obj = adapter_class(**init_kwargs)
    except TypeError:
        # Best-effort fallback for adapters with non-standard init signatures.
        try:
            adapter_obj = adapter_class(config=cfg)
        except Exception as exc:
            return err("adapter_error", f"Failed to instantiate adapter: {exc}")

    chain_id = getattr(adapter_obj, "chain_id", None)
    try:
        chain_id_int = int(chain_id) if chain_id is not None else None
    except (TypeError, ValueError):
        chain_id_int = None

    try:
        fn = getattr(adapter_obj, method_name, None)
        if not callable(fn):
            return err(
                "not_supported",
                f"Adapter '{adapter_name}' does not implement method '{method_name}'",
            )

        res = fn(*call_args, **call_kwargs)
        if asyncio.iscoroutine(res):
            res = await res

        # Try to surface (success, data) tuples but don't require it.
        success = None
        output: Any = res
        if isinstance(res, tuple) and len(res) == 2 and isinstance(res[0], bool):
            success, output = res

        status = (
            "confirmed" if success is True else "failed" if success is False else "ok"
        )
        if strat_wallet and isinstance(strat_wallet.get("address"), str):
            _annotate(
                address=str(strat_wallet["address"]),
                label=label_used,
                protocol=adapter_name,
                action=method_name,
                status=status,
                chain_id=chain_id_int,
                details={"args": call_args, "kwargs": call_kwargs},
            )

        return ok(
            {
                "status": status,
                "adapter": adapter_name,
                "method": method_name,
                "wallet_label": label_used,
                "chain_id": chain_id_int,
                "preview": preview_text,
                "success": success,
                "output": output,
            }
        )
    except Exception as exc:
        if strat_wallet and isinstance(strat_wallet.get("address"), str):
            _annotate(
                address=str(strat_wallet["address"]),
                label=label_used,
                protocol=adapter_name,
                action=method_name,
                status="failed",
                chain_id=chain_id_int,
                details={"error": str(exc)},
            )
        return err("adapter_error", str(exc))
    finally:
        close = getattr(adapter_obj, "close", None)
        if adapter_obj is not None and callable(close):
            try:
                maybe = close()
                if asyncio.iscoroutine(maybe):
                    await maybe
            except Exception:
                pass
