from __future__ import annotations

import re
from typing import Any, cast

from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.constants.base import NATIVE_COINGECKO_IDS, NATIVE_GAS_SYMBOLS
from wayfinder_paths.core.constants.chains import CHAIN_CODE_TO_ID
from wayfinder_paths.core.utils.token_refs import (
    looks_like_evm_address,
    parse_token_id_to_chain_and_address,
)
from wayfinder_paths.core.utils.tokens import get_token_decimals, is_native_token

_SIMPLE_CHAIN_SUFFIX_RE = re.compile(r"^[a-z0-9]+\s+[a-z0-9-]+$", re.IGNORECASE)
_ASSET_CHAIN_SPLIT_RE = re.compile(
    r"^(?P<asset>[a-z0-9]+)[- _](?P<chain>[a-z0-9-]+)$", re.IGNORECASE
)


def _normalize_token_query(query: str) -> str:
    q = " ".join(str(query).strip().split())
    if not q or "-" in q or "_" in q:
        return q
    if not _SIMPLE_CHAIN_SUFFIX_RE.match(q):
        return q
    asset, chain_code = q.rsplit(" ", 1)
    if chain_code.lower() in CHAIN_CODE_TO_ID:
        return f"{asset}-{chain_code}"
    return q


def _is_eth_like_token(meta: dict[str, Any]) -> bool:
    asset_id = str(meta.get("asset_id") or "").lower()
    symbol = str(meta.get("symbol") or "").lower()
    return asset_id in NATIVE_COINGECKO_IDS or symbol in NATIVE_GAS_SYMBOLS


def _split_asset_chain(query: str) -> tuple[str, str] | None:
    q = str(query).strip()
    if not q:
        return None
    m = _ASSET_CHAIN_SPLIT_RE.match(q)
    if not m:
        return None
    return m.group("asset").lower(), m.group("chain").lower()


def _chain_id_from_meta(meta: dict[str, Any]) -> int | None:
    if meta.get("chain_id") is not None:
        try:
            return int(meta.get("chain_id"))
        except (TypeError, ValueError):
            return None

    chain = meta.get("chain") or {}
    if not isinstance(chain, dict):
        return None

    for key in ("chain_id", "chainId", "id"):
        if chain.get(key) is None:
            continue
        try:
            return int(chain.get(key))
        except (TypeError, ValueError):
            return None

    return None


def _infer_chain_code_from_query(query: str, meta: dict[str, Any]) -> str | None:
    q = str(query).strip().lower()
    if not q:
        return None

    candidates: set[str] = {str(k).lower() for k in CHAIN_CODE_TO_ID.keys()}

    addrs = meta.get("addresses") or {}
    if isinstance(addrs, dict):
        candidates.update(str(k).lower() for k in addrs.keys())

    chain_addrs = meta.get("chain_addresses") or {}
    if isinstance(chain_addrs, dict):
        candidates.update(str(k).lower() for k in chain_addrs.keys())

    best: str | None = None
    for code in candidates:
        if q.endswith(f"-{code}"):
            if best is None or len(code) > len(best):
                best = code
    return best


def _address_for_chain(meta: dict[str, Any], chain_code: str) -> str | None:
    addrs = meta.get("addresses") or {}
    if isinstance(addrs, dict):
        for key, val in addrs.items():
            if str(key).lower() == chain_code and val:
                return str(val)

    chain_addrs = meta.get("chain_addresses") or {}
    if isinstance(chain_addrs, dict):
        for key, val in chain_addrs.items():
            if str(key).lower() != chain_code:
                continue
            if isinstance(val, dict):
                addr = val.get("address")
                if addr:
                    return str(addr)

    return None


def _normalize_token_address(token_address: str | None) -> str | None:
    if not token_address:
        return None
    addr = str(token_address).strip()
    if not addr:
        return None
    return ZERO_ADDRESS if is_native_token(addr) else addr


def _select_chain_and_address(
    meta: dict[str, Any], *, query: str
) -> tuple[int | None, str | None]:
    chain_id = _chain_id_from_meta(meta)
    token_address = meta.get("address")

    desired_chain = _infer_chain_code_from_query(query, meta)
    if desired_chain:
        addr = _address_for_chain(meta, desired_chain)
        if addr:
            token_address = addr
        if desired_chain in CHAIN_CODE_TO_ID:
            chain_id = CHAIN_CODE_TO_ID[desired_chain]

    token_address_out = _normalize_token_address(
        str(token_address).strip() if token_address else None
    )
    if token_address_out is None and _is_eth_like_token(meta):
        token_address_out = ZERO_ADDRESS

    return chain_id, token_address_out


class TokenResolver:
    _token_details_cache: dict[str, dict[str, Any]] = {}
    _gas_token_cache: dict[str, dict[str, Any]] = {}

    @classmethod
    def _token_cache_key(cls, query: str, chain_id: int | None) -> str:
        return f"{int(chain_id)}:{query}" if chain_id is not None else query

    @classmethod
    async def _get_token_details_cached(
        cls, query: str, *, chain_id: int | None
    ) -> dict[str, Any]:
        key = cls._token_cache_key(query, chain_id)
        cached = cls._token_details_cache.get(key)
        if cached:
            return cached

        meta = await TOKEN_CLIENT.get_token_details(query, chain_id=chain_id)
        meta_out = cast(dict[str, Any], meta)
        cls._token_details_cache[key] = meta_out
        return meta_out

    @classmethod
    async def _get_gas_token_cached(cls, chain_code: str) -> dict[str, Any]:
        key = str(chain_code).strip().lower()
        cached = cls._gas_token_cache.get(key)
        if cached:
            return cached
        meta = await TOKEN_CLIENT.get_gas_token(key)
        meta_out = cast(dict[str, Any], meta)
        cls._gas_token_cache[key] = meta_out
        return meta_out

    @staticmethod
    def _validate_chain_id_hint(chain_id: int | None, *, query: str) -> int:
        if chain_id is None:
            raise ValueError(f"chain_id is required to resolve: {query}")
        chain_id_i = int(chain_id)
        if chain_id_i <= 0:
            raise ValueError(f"chain_id is required to resolve: {query}")
        return chain_id_i

    @classmethod
    async def resolve_token(
        cls, query: str, *, chain_id: int | None = None
    ) -> tuple[int, str]:
        q_raw = str(query or "").strip()
        if not q_raw:
            raise ValueError("token query is required")

        if q_raw.lower() == "native":
            return cls._validate_chain_id_hint(chain_id, query=q_raw), ZERO_ADDRESS

        parsed_chain_id, parsed_address = parse_token_id_to_chain_and_address(q_raw)
        if parsed_chain_id is not None and parsed_address is not None:
            addr = _normalize_token_address(parsed_address)
            if not addr:
                raise ValueError(f"Cannot resolve token: {query}")
            return int(parsed_chain_id), addr

        if looks_like_evm_address(q_raw):
            chain_id_i = cls._validate_chain_id_hint(chain_id, query=q_raw)
            addr = _normalize_token_address(q_raw)
            if not addr:
                raise ValueError(f"Cannot resolve token: {query}")
            return chain_id_i, addr

        q = _normalize_token_query(q_raw)
        meta: dict[str, Any] | None = None

        split = _split_asset_chain(q)
        if split:
            asset, chain_code = split
            if asset in NATIVE_COINGECKO_IDS or asset in NATIVE_GAS_SYMBOLS:
                try:
                    gas_meta = await cls._get_gas_token_cached(chain_code)
                    if isinstance(gas_meta, dict) and _is_eth_like_token(gas_meta):
                        meta = gas_meta
                except Exception:
                    meta = None

        if meta is None:
            meta = await cls._get_token_details_cached(q, chain_id=chain_id)

        chain_out, addr_out = _select_chain_and_address(meta, query=q)
        if chain_out is None or not addr_out:
            raise ValueError(f"Cannot resolve token: {query}")
        return int(chain_out), str(addr_out)

    @classmethod
    async def resolve_token_meta(
        cls, query: str, *, chain_id: int | None = None
    ) -> dict[str, Any]:
        q_raw = str(query or "").strip()
        if not q_raw:
            raise ValueError("token query is required")

        if q_raw.lower() == "native":
            chain_id_i = cls._validate_chain_id_hint(chain_id, query=q_raw)
            return {
                "token_id": "native",
                "asset_id": "native",
                "symbol": "NATIVE",
                "decimals": 18,
                "chain_id": chain_id_i,
                "address": ZERO_ADDRESS,
                "metadata": {"source": "native"},
            }

        parsed_chain_id, parsed_address = parse_token_id_to_chain_and_address(q_raw)
        if parsed_chain_id is not None and parsed_address is not None:
            addr = _normalize_token_address(parsed_address)
            if not addr:
                raise ValueError(f"Cannot resolve token: {query}")
            decimals = await get_token_decimals(addr, int(parsed_chain_id))
            return {
                "token_id": q_raw,
                "symbol": q_raw,
                "decimals": int(decimals),
                "chain_id": int(parsed_chain_id),
                "address": addr,
                "metadata": {"source": "local"},
            }

        if looks_like_evm_address(q_raw):
            chain_id_i = cls._validate_chain_id_hint(chain_id, query=q_raw)
            addr = _normalize_token_address(q_raw)
            if not addr:
                raise ValueError(f"Cannot resolve token: {query}")
            decimals = await get_token_decimals(addr, int(chain_id_i))
            return {
                "token_id": q_raw,
                "symbol": q_raw,
                "decimals": int(decimals),
                "chain_id": int(chain_id_i),
                "address": addr,
                "metadata": {"source": "address"},
            }

        q = _normalize_token_query(q_raw)
        meta: dict[str, Any] | None = None

        split = _split_asset_chain(q)
        if split:
            asset, chain_code = split
            if asset in {"eth", "ethereum"}:
                try:
                    gas_meta = await cls._get_gas_token_cached(chain_code)
                    if isinstance(gas_meta, dict) and _is_eth_like_token(gas_meta):
                        meta = gas_meta
                except Exception:
                    meta = None

        if meta is None:
            meta = await cls._get_token_details_cached(q, chain_id=chain_id)

        chain_out, addr_out = _select_chain_and_address(meta, query=q)
        if chain_out is None or not addr_out:
            raise ValueError(f"Cannot resolve token: {query}")

        meta_out = dict(meta)
        meta_out["chain_id"] = int(chain_out)
        meta_out["address"] = str(addr_out)
        meta_out.setdefault("metadata", {})
        if isinstance(meta_out["metadata"], dict):
            meta_out["metadata"].setdefault("source", "api")  # type: ignore[union-attr]
            meta_out["metadata"].setdefault("query_normalized", q)  # type: ignore[union-attr]
        return meta_out
