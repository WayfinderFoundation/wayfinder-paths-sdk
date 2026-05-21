from __future__ import annotations

import asyncio
import copy
import os
from typing import Any

import httpx
from eth_utils import to_checksum_address
from loguru import logger

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.constants.base import (
    DEFAULT_HTTP_HEADERS,
    DEFAULT_HTTP_TIMEOUT,
    MAX_UINT256,
)
from wayfinder_paths.core.constants.euler_v2_abi import (
    ACCOUNT_LENS_ABI,
    EVAULT_ABI,
    EVC_ABI,
    PERSPECTIVE_ABI,
    UTILS_LENS_ABI,
    VAULT_INFO_FULL_KEYS,
    VAULT_LENS_ABI,
)
from wayfinder_paths.core.constants.euler_v2_contracts import EULER_V2_BY_CHAIN
from wayfinder_paths.core.utils.interest import RAY
from wayfinder_paths.core.utils.tokens import ensure_allowance
from wayfinder_paths.core.utils.transaction import encode_call, send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

EULER_V3_API_BASE_URL = "https://v3.euler.finance/v3"
EULER_LABELS_BASE_URL = (
    "https://raw.githubusercontent.com/euler-xyz/euler-labels/master"
)
EULER_V3_MAX_LIMIT = 100
EULER_V3_RAW_AMOUNT_FIELDS = {
    "totalAssets": "total_assets_raw",
    "totalBorrows": "total_borrows_raw",
    "totalBorrowed": "total_borrowed_raw",
    "totalCash": "total_cash_raw",
    "totalShares": "total_shares_raw",
    "totalSupply": "total_supply_raw",
    "availableAssets": "available_assets_raw",
    "cash": "cash_raw",
    "supplyCap": "supply_cap_raw",
    "borrowCap": "borrow_cap_raw",
}
EULER_V3_APY_PERCENT_FIELDS = {
    "supplyApy": "supply_apy_decimal",
    "borrowApy": "borrow_apy_decimal",
    "apyCurrent": "apy_current_decimal",
    "apy7d": "apy_7d_decimal",
    "apy30d": "apy_30d_decimal",
    "apy90d": "apy_90d_decimal",
}


def _tuple_to_dict(value: Any, keys: list[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (list, tuple)):
        return dict(zip(keys, list(value), strict=False))
    try:
        return dict(value)
    except Exception:
        return {}


def _ltv_rows(raw: Any) -> list[Any]:
    try:
        return list(raw or [])
    except Exception:
        return []


def _clean_params(params: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in params.items() if v is not None}


def _v3_page_params(*, limit: int, offset: int) -> dict[str, int]:
    return {
        "limit": min(EULER_V3_MAX_LIMIT, max(1, int(limit))),
        "offset": max(0, int(offset)),
    }


def _csv(value: str | list[str] | tuple[str, ...] | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return ",".join(str(v) for v in value if str(v).strip())


def _dedupe_checksum(addresses: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for address in addresses:
        checksummed = to_checksum_address(str(address))
        key = checksummed.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(checksummed)
    return out


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_token(value: Any) -> Any:
    if not isinstance(value, dict):
        return value

    out = dict(value)
    if out.get("address"):
        out["address"] = to_checksum_address(str(out["address"]))
    if out.get("decimals") is not None:
        decimals = _int_or_none(out.get("decimals"))
        if decimals is not None:
            out["decimals"] = decimals
    return out


def _apply_v3_unit_fields(out: dict[str, Any]) -> None:
    for field, normalized_field in EULER_V3_RAW_AMOUNT_FIELDS.items():
        raw_value = _int_or_none(out.get(field))
        if raw_value is not None:
            out[normalized_field] = raw_value

    for field, normalized_field in EULER_V3_APY_PERCENT_FIELDS.items():
        apy = _float_or_none(out.get(field))
        if apy is not None:
            out[normalized_field] = apy / 100


def _normalize_v3_vault(value: Any, *, default_vault_type: str | None = None) -> Any:
    if not isinstance(value, dict):
        return value

    out = dict(value)
    if out.get("address"):
        out["address"] = to_checksum_address(str(out["address"]))
    if out.get("dToken"):
        out["dToken"] = to_checksum_address(str(out["dToken"]))
    if out.get("chainId") is not None:
        chain_id = _int_or_none(out.get("chainId"))
        if chain_id is not None:
            out["chain_id"] = chain_id

    vault_type = str(out.get("vaultType") or default_vault_type or "").strip()
    if vault_type:
        out["vault_type"] = vault_type

    if "asset" in out:
        asset = _normalize_token(out.get("asset"))
        out["asset"] = asset
        if isinstance(asset, dict) and asset.get("address"):
            out["underlying"] = asset["address"]

    if "shares" in out:
        shares = _normalize_token(out.get("shares"))
        if isinstance(shares, dict):
            out["shares"] = shares

    _apply_v3_unit_fields(out)
    return out


def _normalize_v3_price(value: Any) -> Any:
    if not isinstance(value, dict):
        return value

    out = dict(value)
    if out.get("address"):
        out["address"] = to_checksum_address(str(out["address"]))
    if out.get("chainId") is not None:
        chain_id = _int_or_none(out.get("chainId"))
        if chain_id is not None:
            out["chain_id"] = chain_id
    price = _float_or_none(out.get("priceUsd"))
    if price is not None:
        out["price_usd"] = price
    return out


def _normalize_v3_collateral(value: Any) -> Any:
    if not isinstance(value, dict):
        return value

    out = dict(value)
    for field in ("collateral", "asset"):
        if out.get(field):
            out[field] = to_checksum_address(str(out[field]))
    for field in (
        "borrowLTV",
        "liquidationLTV",
        "initialLiquidationLTV",
        "rampDuration",
    ):
        normalized = _int_or_none(out.get(field))
        if normalized is not None:
            out[field] = normalized
    return out


def _normalize_v3_totals(value: Any) -> Any:
    if not isinstance(value, dict):
        return value

    out = dict(value)
    current = out.get("current")
    if isinstance(current, dict):
        current = dict(current)
        _apply_v3_unit_fields(current)
        out["current"] = current

    history = out.get("history")
    if isinstance(history, list):
        normalized_history = []
        for item in history:
            if isinstance(item, dict):
                item = dict(item)
                _apply_v3_unit_fields(item)
            normalized_history.append(item)
        out["history"] = normalized_history

    _apply_v3_unit_fields(out)
    return out


def _normalize_envelope_data(data: Any, normalizer: Any | None) -> Any:
    if normalizer is None:
        return data
    if isinstance(data, list):
        return [normalizer(item) for item in data]
    if isinstance(data, dict):
        return normalizer(data)
    return data


def _euler_error_message(status_code: int, payload: Any) -> str:
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        error = payload["error"]
        code = str(error.get("code") or "HTTP_ERROR")
        message = str(error.get("message") or f"HTTP {status_code}")
        request_id = error.get("requestId")
        suffix = f" request_id={request_id}" if request_id else ""
        return f"Euler endpoint error {code}: {message}{suffix}"
    return f"Euler endpoint error HTTP_{status_code}"


def _api_envelope(
    *,
    source: str,
    endpoint: str,
    payload: dict[str, Any],
    normalizer: Any | None = None,
) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else None
    meta = payload.get("meta") if isinstance(payload, dict) else None
    return {
        "source": source,
        "endpoint": endpoint,
        "data": _normalize_envelope_data(data, normalizer),
        "meta": meta or {},
        "raw": payload,
    }


class EulerV2Adapter(BaseAdapter):
    """
    Euler v2 (EVK / eVault) adapter.

    Terminology:
    - "vault" is the market address and also the ERC-4626 share token.
    - Underlying is `vault.asset()`.
    - Variable debt token is `vault.dToken()`.
    """

    adapter_type = "EULER_V2"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        strategy_wallet_signing_callback=None,
    ) -> None:
        super().__init__("euler_v2_adapter", config or {})
        cfg = config or {}
        self.strategy_wallet_signing_callback = strategy_wallet_signing_callback

        strategy_addr = (cfg.get("strategy_wallet") or {}).get("address")
        self.strategy_wallet_address: str | None = (
            to_checksum_address(strategy_addr) if strategy_addr else None
        )

        self._asset_by_chain_vault: dict[tuple[int, str], str] = {}
        self.euler_v3_api_base_url = str(
            cfg.get("euler_v3_api_base_url") or EULER_V3_API_BASE_URL
        ).rstrip("/")
        self.euler_labels_base_url = str(
            cfg.get("euler_labels_base_url") or EULER_LABELS_BASE_URL
        ).rstrip("/")
        self.euler_v3_api_key = str(
            cfg.get("euler_v3_api_key") or os.environ.get("EULER_V3_API_KEY") or ""
        ).strip()

    @staticmethod
    def _entry(chain_id: int) -> dict[str, Any]:
        entry = EULER_V2_BY_CHAIN.get(int(chain_id))
        if not entry:
            raise ValueError(f"Unsupported Euler v2 chain_id={chain_id}")
        return entry

    @staticmethod
    def _perspective(entry: dict[str, Any], perspective: str) -> str:
        perspectives = entry.get("perspectives") or {}
        addr = perspectives.get(str(perspective))
        if not addr:
            raise ValueError(f"Unknown perspective: {perspective}")
        return to_checksum_address(str(addr))

    async def _http_get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        merged_headers = dict(DEFAULT_HTTP_HEADERS)
        if headers:
            merged_headers.update(headers)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(DEFAULT_HTTP_TIMEOUT),
            follow_redirects=True,
            headers=merged_headers,
        ) as client:
            response = await client.get(url, params=params or {})
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                try:
                    error_payload = response.json()
                except ValueError:
                    error_payload = {}
                raise ValueError(
                    _euler_error_message(response.status_code, error_payload)
                ) from exc
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Euler endpoint returned a non-object JSON payload")
        if isinstance(payload.get("error"), dict):
            raise ValueError(_euler_error_message(response.status_code, payload))
        return payload

    async def _euler_v3_get(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        headers: dict[str, str] = {}
        if self.euler_v3_api_key:
            headers["X-API-Key"] = self.euler_v3_api_key
        return await self._http_get_json(
            f"{self.euler_v3_api_base_url}{endpoint}",
            params=_clean_params(params or {}),
            headers=headers,
        )

    async def _euler_labels_get(
        self,
        *,
        chain_id: int,
        file_name: str,
    ) -> dict[str, Any] | list[Any]:
        if file_name not in {"products.json", "earn-vaults.json"}:
            raise ValueError(f"Unsupported Euler labels file: {file_name}")
        url = f"{self.euler_labels_base_url}/{int(chain_id)}/{file_name}"
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(DEFAULT_HTTP_TIMEOUT),
            follow_redirects=True,
            headers=DEFAULT_HTTP_HEADERS,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, (dict, list)):
            raise ValueError("Euler labels endpoint returned unsupported JSON")
        return payload

    async def get_protocol_contracts(
        self,
        *,
        chain_id: int,
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            return True, copy.deepcopy(self._entry(int(chain_id)))
        except Exception as exc:
            return False, str(exc)

    async def get_indexed_vaults(
        self,
        *,
        chain_id: int,
        limit: int = 100,
        offset: int = 0,
        fields: str | list[str] | tuple[str, ...] | None = None,
        sort: str | None = None,
        asset: str | None = None,
        min_tvl: float | None = None,
        max_tvl: float | None = None,
        visibility: str | None = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        """Read indexed EVK vault summaries from Euler's V3 API preview."""
        try:
            params = {
                "chainId": int(chain_id),
                **_v3_page_params(limit=limit, offset=offset),
                "fields": _csv(fields),
                "sort": sort,
                "asset": to_checksum_address(asset) if asset else None,
                "minTvl": min_tvl,
                "maxTvl": max_tvl,
                "visibility": visibility,
            }
            payload = await self._euler_v3_get("/evk/vaults", params=params)
            return True, _api_envelope(
                source="euler_v3_api",
                endpoint="/evk/vaults",
                payload=payload,
                normalizer=lambda item: _normalize_v3_vault(
                    item, default_vault_type="evk"
                ),
            )
        except Exception as exc:
            return False, str(exc)

    async def get_indexed_vault(
        self,
        *,
        chain_id: int,
        vault: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        """Read one indexed EVK vault detail from Euler's V3 API preview."""
        try:
            vault_addr = to_checksum_address(vault)
            endpoint = f"/evk/vaults/{int(chain_id)}/{vault_addr}"
            payload = await self._euler_v3_get(endpoint)
            return True, _api_envelope(
                source="euler_v3_api",
                endpoint=endpoint,
                payload=payload,
                normalizer=lambda item: _normalize_v3_vault(
                    item, default_vault_type="evk"
                ),
            )
        except Exception as exc:
            return False, str(exc)

    async def get_indexed_vault_collaterals(
        self,
        *,
        chain_id: int,
        vault: str,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[bool, dict[str, Any] | str]:
        """Read indexed collateral LTV rows for one EVK vault from Euler's V3 API."""
        try:
            vault_addr = to_checksum_address(vault)
            endpoint = f"/evk/vaults/{int(chain_id)}/{vault_addr}/collaterals"
            payload = await self._euler_v3_get(
                endpoint,
                params=_v3_page_params(limit=limit, offset=offset),
            )
            return True, _api_envelope(
                source="euler_v3_api",
                endpoint=endpoint,
                payload=payload,
                normalizer=_normalize_v3_collateral,
            )
        except Exception as exc:
            return False, str(exc)

    async def get_indexed_vault_totals(
        self,
        *,
        chain_id: int,
        vault: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        """Read indexed current and historical totals for one EVK vault."""
        try:
            vault_addr = to_checksum_address(vault)
            endpoint = f"/evk/vaults/{int(chain_id)}/{vault_addr}/totals"
            payload = await self._euler_v3_get(endpoint)
            return True, _api_envelope(
                source="euler_v3_api",
                endpoint=endpoint,
                payload=payload,
                normalizer=_normalize_v3_totals,
            )
        except Exception as exc:
            return False, str(exc)

    async def get_euler_earn_vaults(
        self,
        *,
        chain_id: int,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[bool, dict[str, Any] | str]:
        """Read indexed EulerEarn vault summaries from Euler's V3 API preview."""
        try:
            payload = await self._euler_v3_get(
                "/earn/vaults",
                params={
                    "chainId": int(chain_id),
                    **_v3_page_params(limit=limit, offset=offset),
                },
            )
            return True, _api_envelope(
                source="euler_v3_api",
                endpoint="/earn/vaults",
                payload=payload,
                normalizer=lambda item: _normalize_v3_vault(
                    item, default_vault_type="earn"
                ),
            )
        except Exception as exc:
            return False, str(exc)

    async def get_euler_earn_vault(
        self,
        *,
        chain_id: int,
        vault: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        """Read one indexed EulerEarn vault detail from Euler's V3 API preview."""
        try:
            vault_addr = to_checksum_address(vault)
            endpoint = f"/earn/vaults/{int(chain_id)}/{vault_addr}"
            payload = await self._euler_v3_get(endpoint)
            return True, _api_envelope(
                source="euler_v3_api",
                endpoint=endpoint,
                payload=payload,
                normalizer=lambda item: _normalize_v3_vault(
                    item, default_vault_type="earn"
                ),
            )
        except Exception as exc:
            return False, str(exc)

    async def resolve_vault(
        self,
        *,
        chain_id: int,
        address: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            payload = await self._euler_v3_get(
                "/resolve/vaults",
                params={
                    "chainId": int(chain_id),
                    "address": to_checksum_address(address),
                },
            )
            return True, _api_envelope(
                source="euler_v3_api",
                endpoint="/resolve/vaults",
                payload=payload,
                normalizer=lambda item: _normalize_v3_vault(item),
            )
        except Exception as exc:
            return False, str(exc)

    async def get_offchain_prices(
        self,
        *,
        chain_id: int,
        addresses: str | list[str] | tuple[str, ...],
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            if isinstance(addresses, str):
                address_list = [a.strip() for a in addresses.split(",")]
            else:
                address_list = [str(a).strip() for a in addresses]
            checked = _dedupe_checksum(address_list)
            if not checked:
                return False, "at least one token address is required"
            payload = await self._euler_v3_get(
                "/prices",
                params={
                    "chainId": int(chain_id),
                    "addresses": ",".join(checked),
                },
            )
            return True, _api_envelope(
                source="euler_v3_api",
                endpoint="/prices",
                payload=payload,
                normalizer=_normalize_v3_price,
            )
        except Exception as exc:
            return False, str(exc)

    async def get_labelled_vaults(
        self,
        *,
        chain_id: int,
        include_products: bool = True,
        include_earn: bool = True,
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            products: dict[str, Any] = {}
            earn_raw: list[Any] = []
            evk_vaults: list[Any] = []

            if include_products:
                products_payload = await self._euler_labels_get(
                    chain_id=int(chain_id), file_name="products.json"
                )
                if isinstance(products_payload, dict):
                    products = products_payload
                    for product in products.values():
                        if isinstance(product, dict):
                            evk_vaults.extend(product.get("vaults") or [])

            if include_earn:
                earn_payload = await self._euler_labels_get(
                    chain_id=int(chain_id), file_name="earn-vaults.json"
                )
                if isinstance(earn_payload, list):
                    earn_raw = earn_payload

            return True, {
                "source": "euler_labels",
                "chain_id": int(chain_id),
                "evk_vaults": _dedupe_checksum(evk_vaults),
                "earn_vaults": _dedupe_checksum(earn_raw),
                "products": products,
                "product_count": len(products),
            }
        except Exception as exc:
            return False, str(exc)

    async def _encode_data(
        self,
        *,
        chain_id: int,
        target: str,
        abi: list[dict[str, Any]],
        fn_name: str,
        args: list[Any],
        from_address: str,
    ) -> str:
        tx = await encode_call(
            target=target,
            abi=abi,
            fn_name=fn_name,
            args=args,
            from_address=from_address,
            chain_id=int(chain_id),
        )
        return str(tx.get("data") or "0x")

    async def _vault_asset(self, *, chain_id: int, vault: str) -> str:
        key = (int(chain_id), str(vault).lower())
        cached = self._asset_by_chain_vault.get(key)
        if cached:
            return cached

        async with web3_from_chain_id(int(chain_id)) as web3:
            v = web3.eth.contract(
                address=web3.to_checksum_address(vault),
                abi=EVAULT_ABI,
            )
            asset = await v.functions.asset().call(block_identifier="latest")
            asset = to_checksum_address(str(asset))
            self._asset_by_chain_vault[key] = asset
            return asset

    async def get_verified_vaults(
        self,
        *,
        chain_id: int,
        perspective: str = "governed",
        limit: int | None = None,
    ) -> tuple[bool, list[str] | str]:
        try:
            entry = self._entry(int(chain_id))
            perspective_addr = self._perspective(entry, str(perspective))

            async with web3_from_chain_id(int(chain_id)) as web3:
                p = web3.eth.contract(
                    address=web3.to_checksum_address(perspective_addr),
                    abi=PERSPECTIVE_ABI,
                )
                vaults = await p.functions.verifiedArray().call(
                    block_identifier="latest"
                )

            out = [to_checksum_address(str(v)) for v in (vaults or [])]
            if limit is not None:
                out = out[: max(0, int(limit))]
            return True, out
        except Exception as exc:
            return False, str(exc)

    async def get_vault_info_full(
        self,
        *,
        chain_id: int,
        vault: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            entry = self._entry(int(chain_id))
            vault_lens = entry["lenses"]["vault_lens"]
            vault = to_checksum_address(vault)

            async with web3_from_chain_id(int(chain_id)) as web3:
                lens = web3.eth.contract(
                    address=web3.to_checksum_address(vault_lens),
                    abi=VAULT_LENS_ABI,
                )
                info = await lens.functions.getVaultInfoFull(vault).call(
                    block_identifier="latest"
                )

            return True, _tuple_to_dict(info, VAULT_INFO_FULL_KEYS)
        except Exception as exc:
            return False, str(exc)

    async def get_all_markets(
        self,
        *,
        chain_id: int,
        perspective: str = "governed",
        limit: int | None = None,
        concurrency: int = 10,
    ) -> tuple[bool, list[dict[str, Any]] | str]:
        try:
            entry = self._entry(int(chain_id))
            vault_lens_addr = entry["lenses"]["vault_lens"]
            account_lens_addr = entry["lenses"]["account_lens"]
            utils_lens_addr = entry["lenses"]["utils_lens"]
            evc_addr = entry["evc"]
            perspective_addr = self._perspective(entry, str(perspective))

            async with web3_from_chain_id(int(chain_id)) as web3:
                perspective_c = web3.eth.contract(
                    address=web3.to_checksum_address(perspective_addr),
                    abi=PERSPECTIVE_ABI,
                )
                vaults = await perspective_c.functions.verifiedArray().call(
                    block_identifier="latest"
                )
                vaults_list = [to_checksum_address(str(v)) for v in (vaults or [])]
                if limit is not None:
                    vaults_list = vaults_list[: max(0, int(limit))]

                vault_lens = web3.eth.contract(
                    address=web3.to_checksum_address(vault_lens_addr),
                    abi=VAULT_LENS_ABI,
                )
                utils_lens = web3.eth.contract(
                    address=web3.to_checksum_address(utils_lens_addr),
                    abi=UTILS_LENS_ABI,
                )

                sem = asyncio.Semaphore(max(1, int(concurrency)))

                async def _fetch(vault: str) -> tuple[bool, Any]:
                    async with sem:
                        try:
                            info_raw = await vault_lens.functions.getVaultInfoFull(
                                vault
                            ).call(block_identifier="latest")
                            info = _tuple_to_dict(info_raw, VAULT_INFO_FULL_KEYS)

                            (
                                borrow_apy_ray,
                                supply_apy_ray,
                            ) = await utils_lens.functions.getAPYs(vault).call(
                                block_identifier="latest"
                            )

                            ltv_info: list[dict[str, Any]] = []
                            for row in _ltv_rows(info.get("collateralLTVInfo")):
                                try:
                                    if isinstance(row, dict):
                                        collateral = to_checksum_address(
                                            str(row.get("collateral"))
                                        )
                                        borrow_ltv = int(row.get("borrowLTV") or 0)
                                        liq_ltv = int(row.get("liquidationLTV") or 0)
                                        init_liq = int(
                                            row.get("initialLiquidationLTV") or 0
                                        )
                                        target_ts = int(row.get("targetTimestamp") or 0)
                                        ramp_duration = int(
                                            row.get("rampDuration") or 0
                                        )
                                    else:
                                        collateral = to_checksum_address(str(row[0]))
                                        borrow_ltv = int(row[1] or 0)
                                        liq_ltv = int(row[2] or 0)
                                        init_liq = int(row[3] or 0)
                                        target_ts = int(row[4] or 0)
                                        ramp_duration = int(row[5] or 0)
                                except Exception:
                                    continue

                                if borrow_ltv <= 0:
                                    continue
                                ltv_info.append(
                                    {
                                        "collateral": collateral,
                                        "borrow_ltv": borrow_ltv,
                                        "liquidation_ltv": liq_ltv,
                                        "initial_liquidation_ltv": init_liq,
                                        "target_timestamp": target_ts,
                                        "ramp_duration": ramp_duration,
                                    }
                                )

                            return True, {
                                "chain_id": int(chain_id),
                                "evc": to_checksum_address(str(evc_addr)),
                                "vault_lens": to_checksum_address(str(vault_lens_addr)),
                                "account_lens": to_checksum_address(
                                    str(account_lens_addr)
                                ),
                                "utils_lens": to_checksum_address(str(utils_lens_addr)),
                                "perspective": to_checksum_address(
                                    str(perspective_addr)
                                ),
                                "vault": vault,
                                "underlying": to_checksum_address(
                                    str(info.get("asset"))
                                ),
                                "share_token": vault,
                                "debt_token": to_checksum_address(
                                    str(info.get("dToken"))
                                ),
                                "supply_apy": float(int(supply_apy_ray or 0)) / RAY,
                                "borrow_apy": float(int(borrow_apy_ray or 0)) / RAY,
                                "supply_cap": int(info.get("supplyCap") or 0),
                                "borrow_cap": int(info.get("borrowCap") or 0),
                                "cash": int(info.get("totalCash") or 0),
                                "total_borrows": int(info.get("totalBorrowed") or 0),
                                "total_assets": int(info.get("totalAssets") or 0),
                                "vault_symbol": str(info.get("vaultSymbol") or ""),
                                "vault_name": str(info.get("vaultName") or ""),
                                "vault_decimals": int(info.get("vaultDecimals") or 0),
                                "asset_symbol": str(info.get("assetSymbol") or ""),
                                "asset_name": str(info.get("assetName") or ""),
                                "asset_decimals": int(info.get("assetDecimals") or 0),
                                "collateral_ltv_info": ltv_info,
                                "raw": info,
                            }
                        except Exception as exc:
                            logger.warning(
                                "Euler vault %s fetch failed: %s", vault, exc
                            )
                            return False, f"{vault}: {exc}"

                results = await asyncio.gather(
                    *[_fetch(v) for v in vaults_list], return_exceptions=False
                )

            markets = [data for ok, data in results if ok]
            errors = [data for ok, data in results if not ok]
            if errors:
                logger.warning(
                    "Euler get_all_markets: %d/%d vaults failed: %s",
                    len(errors),
                    len(vaults_list),
                    "; ".join(errors),
                )
            if not markets:
                return False, f"All vault fetches failed: {'; '.join(errors)}"
            return True, markets
        except Exception as exc:
            return False, str(exc)

    async def get_full_user_state(
        self,
        *,
        chain_id: int,
        account: str,
        include_zero_positions: bool = False,
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            entry = self._entry(int(chain_id))
            evc_addr = to_checksum_address(str(entry["evc"]))
            account_lens_addr = to_checksum_address(
                str(entry["lenses"]["account_lens"])
            )

            acct = to_checksum_address(account)

            async with web3_from_chain_id(int(chain_id)) as web3:
                lens = web3.eth.contract(
                    address=web3.to_checksum_address(account_lens_addr),
                    abi=ACCOUNT_LENS_ABI,
                )
                out = await lens.functions.getAccountEnabledVaultsInfo(
                    evc_addr, acct
                ).call(block_identifier="latest")

            # out: (evcAccountInfo, vaultAccountInfo[], accountRewardInfo[])
            evc_info = out[0] if isinstance(out, (list, tuple)) and len(out) > 0 else {}
            vault_infos = (
                out[1] if isinstance(out, (list, tuple)) and len(out) > 1 else []
            )
            reward_infos = (
                out[2] if isinstance(out, (list, tuple)) and len(out) > 2 else []
            )

            positions: list[dict[str, Any]] = []
            for vi in vault_infos or []:
                try:
                    vault_addr = to_checksum_address(
                        str(vi[2] if not isinstance(vi, dict) else vi.get("vault"))
                    )
                    asset_addr = to_checksum_address(
                        str(vi[3] if not isinstance(vi, dict) else vi.get("asset"))
                    )
                    shares = int(
                        vi[5] if not isinstance(vi, dict) else vi.get("shares") or 0
                    )
                    assets = int(
                        vi[6] if not isinstance(vi, dict) else vi.get("assets") or 0
                    )
                    borrowed = int(
                        vi[7] if not isinstance(vi, dict) else vi.get("borrowed") or 0
                    )
                    is_controller = bool(
                        vi[13] if not isinstance(vi, dict) else vi.get("isController")
                    )
                    is_collateral = bool(
                        vi[14] if not isinstance(vi, dict) else vi.get("isCollateral")
                    )
                except Exception:
                    continue

                if not include_zero_positions and not (shares or assets or borrowed):
                    continue

                positions.append(
                    {
                        "vault": vault_addr,
                        "underlying": asset_addr,
                        "shares": shares,
                        "assets": assets,
                        "borrowed": borrowed,
                        "is_controller": is_controller,
                        "is_collateral": is_collateral,
                        "raw": vi,
                    }
                )

            return True, {
                "protocol": "euler_v2",
                "chain_id": int(chain_id),
                "evc": evc_addr,
                "account": acct,
                "evc_account_info": evc_info,
                "positions": positions,
                "rewards": reward_infos,
                "raw": out,
            }
        except Exception as exc:
            return False, str(exc)

    async def _evc_batch(
        self,
        *,
        chain_id: int,
        items: list[tuple[str, str, int, str]],
        value: int | None = None,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        if not items:
            return False, "no batch items provided"

        try:
            entry = self._entry(int(chain_id))
            evc = to_checksum_address(str(entry["evc"]))

            total_value = (
                int(value) if value is not None else sum(int(i[2]) for i in items)
            )
            tx = await encode_call(
                target=evc,
                abi=EVC_ABI,
                fn_name="batch",
                args=[items],
                from_address=strategy,
                chain_id=int(chain_id),
                value=total_value,
            )
            txn_hash = await send_transaction(tx, self.strategy_wallet_signing_callback)
        except Exception as e:
            return False, str(e)
        return True, txn_hash

    async def lend(
        self,
        *,
        chain_id: int,
        vault: str,
        amount: int,
        receiver: str | None = None,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        amount = int(amount)
        if amount <= 0:
            return False, "amount must be positive"

        try:
            vault_addr = to_checksum_address(vault)
            recv = to_checksum_address(receiver) if receiver else strategy
            asset = await self._vault_asset(chain_id=int(chain_id), vault=vault_addr)

            approved = await ensure_allowance(
                token_address=asset,
                owner=strategy,
                spender=vault_addr,
                amount=amount,
                chain_id=int(chain_id),
                signing_callback=self.strategy_wallet_signing_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            data = await self._encode_data(
                chain_id=int(chain_id),
                target=vault_addr,
                abi=EVAULT_ABI,
                fn_name="deposit",
                args=[amount, recv],
                from_address=strategy,
            )
            items = [(vault_addr, strategy, 0, data)]
            return await self._evc_batch(chain_id=int(chain_id), items=items)
        except Exception as exc:
            return False, str(exc)

    async def unlend(
        self,
        *,
        chain_id: int,
        vault: str,
        amount: int = 0,
        receiver: str | None = None,
        withdraw_full: bool = False,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"

        try:
            vault_addr = to_checksum_address(vault)
            recv = to_checksum_address(receiver) if receiver else strategy

            if withdraw_full:
                async with web3_from_chain_id(int(chain_id)) as web3:
                    v = web3.eth.contract(
                        address=web3.to_checksum_address(vault_addr),
                        abi=EVAULT_ABI,
                    )
                    shares = await v.functions.balanceOf(strategy).call(
                        block_identifier="latest"
                    )
                shares = int(shares or 0)
                if shares <= 0:
                    return False, "no shares to redeem"

                data = await self._encode_data(
                    chain_id=int(chain_id),
                    target=vault_addr,
                    abi=EVAULT_ABI,
                    fn_name="redeem",
                    args=[shares, recv, strategy],
                    from_address=strategy,
                )
                items = [(vault_addr, strategy, 0, data)]
                return await self._evc_batch(chain_id=int(chain_id), items=items)

            qty = int(amount)
            if qty <= 0:
                return False, "withdraw amount must be positive"

            data = await self._encode_data(
                chain_id=int(chain_id),
                target=vault_addr,
                abi=EVAULT_ABI,
                fn_name="withdraw",
                args=[qty, recv, strategy],
                from_address=strategy,
            )
            items = [(vault_addr, strategy, 0, data)]
            return await self._evc_batch(chain_id=int(chain_id), items=items)
        except Exception as exc:
            return False, str(exc)

    async def set_collateral(
        self,
        *,
        chain_id: int,
        vault: str,
        use_as_collateral: bool = True,
        account: str | None = None,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"

        try:
            entry = self._entry(int(chain_id))
            evc = to_checksum_address(str(entry["evc"]))
            acct = to_checksum_address(account) if account else strategy
            vault_addr = to_checksum_address(vault)

            fn_name = "enableCollateral" if use_as_collateral else "disableCollateral"
            data = await self._encode_data(
                chain_id=int(chain_id),
                target=evc,
                abi=EVC_ABI,
                fn_name=fn_name,
                args=[acct, vault_addr],
                from_address=strategy,
            )
            items = [(evc, ZERO_ADDRESS, 0, data)]
            return await self._evc_batch(chain_id=int(chain_id), items=items)
        except Exception as exc:
            return False, str(exc)

    async def remove_collateral(
        self,
        *,
        chain_id: int,
        vault: str,
        account: str | None = None,
    ) -> tuple[bool, Any]:
        return await self.set_collateral(
            chain_id=int(chain_id),
            vault=str(vault),
            use_as_collateral=False,
            account=account,
        )

    async def borrow(
        self,
        *,
        chain_id: int,
        vault: str,
        amount: int,
        receiver: str | None = None,
        collateral_vaults: list[str] | None = None,
        enable_controller: bool = True,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"
        amount = int(amount)
        if amount <= 0:
            return False, "amount must be positive"

        try:
            entry = self._entry(int(chain_id))
            evc = to_checksum_address(str(entry["evc"]))
            vault_addr = to_checksum_address(vault)
            recv = to_checksum_address(receiver) if receiver else strategy

            items: list[tuple[str, str, int, str]] = []

            for cv in collateral_vaults or []:
                c_vault = to_checksum_address(cv)
                enable_collateral_data = await self._encode_data(
                    chain_id=int(chain_id),
                    target=evc,
                    abi=EVC_ABI,
                    fn_name="enableCollateral",
                    args=[strategy, c_vault],
                    from_address=strategy,
                )
                items.append((evc, ZERO_ADDRESS, 0, enable_collateral_data))

            if enable_controller:
                enable_controller_data = await self._encode_data(
                    chain_id=int(chain_id),
                    target=evc,
                    abi=EVC_ABI,
                    fn_name="enableController",
                    args=[strategy, vault_addr],
                    from_address=strategy,
                )
                items.append((evc, ZERO_ADDRESS, 0, enable_controller_data))

            borrow_data = await self._encode_data(
                chain_id=int(chain_id),
                target=vault_addr,
                abi=EVAULT_ABI,
                fn_name="borrow",
                args=[amount, recv],
                from_address=strategy,
            )
            items.append((vault_addr, strategy, 0, borrow_data))

            return await self._evc_batch(chain_id=int(chain_id), items=items)
        except Exception as exc:
            return False, str(exc)

    async def repay(
        self,
        *,
        chain_id: int,
        vault: str,
        amount: int,
        receiver: str | None = None,
        repay_full: bool = False,
    ) -> tuple[bool, Any]:
        strategy = self.strategy_wallet_address
        if not strategy:
            return False, "strategy wallet address not configured"

        try:
            vault_addr = to_checksum_address(vault)
            recv = to_checksum_address(receiver) if receiver else strategy
            qty = int(amount)
            if qty <= 0 and not repay_full:
                return False, "amount must be positive (or set repay_full=True)"

            repay_amount = MAX_UINT256 if repay_full else qty
            allowance_target = MAX_UINT256 if repay_full else qty

            asset = await self._vault_asset(chain_id=int(chain_id), vault=vault_addr)
            approved = await ensure_allowance(
                token_address=asset,
                owner=strategy,
                spender=vault_addr,
                amount=allowance_target,
                chain_id=int(chain_id),
                signing_callback=self.strategy_wallet_signing_callback,
                approval_amount=MAX_UINT256,
            )
            if not approved[0]:
                return approved

            data = await self._encode_data(
                chain_id=int(chain_id),
                target=vault_addr,
                abi=EVAULT_ABI,
                fn_name="repay",
                args=[int(repay_amount), recv],
                from_address=strategy,
            )
            items = [(vault_addr, strategy, 0, data)]
            return await self._evc_batch(chain_id=int(chain_id), items=items)
        except Exception as exc:
            return False, str(exc)
