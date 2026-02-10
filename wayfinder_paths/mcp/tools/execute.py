from __future__ import annotations

import re
from typing import Any, Literal, cast

from eth_account import Account
from eth_utils import to_checksum_address
from pydantic import BaseModel, Field, ValidationError, model_validator

from wayfinder_paths.core.clients.BRAPClient import BRAP_CLIENT
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT
from wayfinder_paths.core.constants import ZERO_ADDRESS
from wayfinder_paths.core.constants.base import GAS_ALIASES
from wayfinder_paths.core.constants.chains import CHAIN_CODE_TO_ID
from wayfinder_paths.core.constants.hyperliquid import (
    ARBITRUM_USDC_ADDRESS,
    ARBITRUM_USDC_TOKEN_ID,
    HYPERLIQUID_BRIDGE_ADDRESS,
)
from wayfinder_paths.core.utils.etherscan import get_etherscan_transaction_link
from wayfinder_paths.core.utils.tokens import (
    build_send_transaction,
    ensure_allowance,
)
from wayfinder_paths.core.utils.transaction import send_transaction
from wayfinder_paths.mcp.preview import build_execution_preview
from wayfinder_paths.mcp.state.profile_store import WalletProfileStore
from wayfinder_paths.mcp.utils import (
    err,
    find_wallet_by_label,
    normalize_address,
    ok,
    parse_amount_to_raw,
)


class ExecutionRequest(BaseModel):
    kind: Literal["swap", "send", "hyperliquid_deposit"]
    wallet_label: str = Field(..., description="config.json wallet label (e.g. main)")

    # Shared
    amount: str = Field(..., description="Human units as a string (e.g. '1000')")
    recipient: str | None = Field(
        default=None, description="Destination address (defaults to sender for swap)"
    )

    # swap-only
    from_token: str | None = Field(default=None, description="Token id/address query")
    to_token: str | None = Field(default=None, description="Token id/address query")
    slippage_bps: int = Field(default=50, description="Slippage in bps (50 = 0.50%)")
    deadline_seconds: int = Field(
        default=300, description="Best-effort TTL for quoting"
    )

    # send-only
    token: str | None = Field(
        default=None, description="Token id/address query, or 'native'"
    )
    chain_id: int | None = Field(
        default=None, description="Required when token='native'"
    )

    @model_validator(mode="after")
    def _validate_kind(self) -> ExecutionRequest:
        if not self.wallet_label.strip():
            raise ValueError("wallet_label is required")
        if self.kind == "swap":
            if not (self.from_token and self.to_token):
                raise ValueError("swap requires from_token and to_token")
            if self.slippage_bps < 0:
                raise ValueError("slippage_bps must be >= 0")
            if self.deadline_seconds <= 0:
                raise ValueError("deadline_seconds must be positive")
        if self.kind == "send":
            if not self.token:
                raise ValueError("send requires token")
            if not self.recipient:
                raise ValueError("send requires recipient")
            if str(self.token).strip().lower() == "native" and self.chain_id is None:
                raise ValueError("send requires chain_id when token='native'")
        if self.kind == "hyperliquid_deposit":
            # Hard-coded Bridge2 deposit: Arbitrum USDC -> Hyperliquid bridge address.
            # Allow callers to omit token/recipient entirely; if provided, they must match.
            if self.recipient and _addr_lower(self.recipient) != _addr_lower(
                HYPERLIQUID_BRIDGE_ADDRESS
            ):
                raise ValueError("hyperliquid_deposit recipient must be bridge address")
            if self.token and str(self.token).strip() != ARBITRUM_USDC_TOKEN_ID:
                raise ValueError(
                    f"hyperliquid_deposit token must be {ARBITRUM_USDC_TOKEN_ID}"
                )
            if self.chain_id is not None and int(self.chain_id) != 42161:
                raise ValueError(
                    "hyperliquid_deposit chain_id must be 42161 (Arbitrum)"
                )
            try:
                amt = float(self.amount)
            except (TypeError, ValueError):
                amt = None
            if amt is not None and amt < 5:
                raise ValueError(
                    "hyperliquid_deposit amount must be >= 5 USDC (deposits below are lost)"
                )
        return self


def _addr_lower(addr: str | None) -> str | None:
    if not addr:
        return None
    a = str(addr).strip()
    return a.lower() if a else None


def _chain_id_from_token(meta: dict[str, Any]) -> int | None:
    # Token payloads often include an internal/db `id` field. Only accept:
    # - meta["chain_id"] (preferred)
    # - meta["chain"]["chain_id"] / ["chainId"] / ["id"]
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


def _is_gas_token(meta: dict[str, Any]) -> bool:
    asset_id = str(meta.get("asset_id") or "").lower()
    symbol = str(meta.get("symbol") or "").lower()
    return asset_id in GAS_ALIASES or symbol in GAS_ALIASES


def _split_asset_chain(query: str) -> tuple[str, str] | None:
    q = str(query).strip()
    if not q:
        return None
    m = _ASSET_CHAIN_SPLIT_RE.match(q)
    if not m:
        return None
    return m.group("asset").lower(), m.group("chain").lower()


async def _resolve_token_meta(query: str) -> tuple[str, dict[str, Any]]:
    q = _normalize_token_query(query)
    split = _split_asset_chain(q)
    if split:
        asset, chain_code = split
        if asset in GAS_ALIASES:
            try:
                gas_meta = await TOKEN_CLIENT.get_gas_token(chain_code)
                if isinstance(gas_meta, dict) and _is_gas_token(gas_meta):
                    return q, cast(dict[str, Any], gas_meta)
            except Exception:
                pass
    meta = await TOKEN_CLIENT.get_token_details(q)
    return q, cast(dict[str, Any], meta)


def _infer_chain_code_from_query(query: str, meta: dict[str, Any]) -> str | None:
    q = str(query).strip().lower()
    if not q:
        return None

    candidates: set[str] = {str(k).lower() for k in CHAIN_CODE_TO_ID.keys()}
    addrs = meta.get("addresses") or {}
    if isinstance(addrs, dict):
        candidates.update(str(k).lower() for k in addrs.keys())

    best: str | None = None
    for code in candidates:
        if q.endswith(f"-{code}"):
            if best is None or len(code) > len(best):
                best = code
    return best


def _address_for_chain(meta: dict[str, Any], chain_code: str) -> str | None:
    addrs = meta.get("addresses") or {}
    if not isinstance(addrs, dict):
        return None
    for key, val in addrs.items():
        if str(key).lower() == chain_code and val:
            return str(val)
    return None


def _select_token_chain(
    meta: dict[str, Any], *, query: str
) -> tuple[int | None, str | None]:
    chain_id = _chain_id_from_token(meta)
    token_address = meta.get("address")

    desired_chain = _infer_chain_code_from_query(query, meta)
    if desired_chain:
        addr = _address_for_chain(meta, desired_chain)
        if addr:
            token_address = addr
        if desired_chain in CHAIN_CODE_TO_ID:
            chain_id = CHAIN_CODE_TO_ID[desired_chain]

    token_address_out = str(token_address).strip() if token_address else None

    # Native gas tokens (e.g. ETH) may come back from the API with a null
    # address.  Normalize to ZERO_ADDRESS so swap/quote paths treat them the
    # same as the send path (which already does this explicitly).
    if not token_address_out and _is_gas_token(meta):
        token_address_out = ZERO_ADDRESS

    return chain_id, token_address_out


def _sanitize_for_json(obj: Any) -> Any:
    if hasattr(obj, "hex") and callable(obj.hex):
        return obj.hex()
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(v) for v in obj]
    return obj


def _compact_quote(
    quote_data: dict[str, Any], best_quote: dict[str, Any] | None
) -> dict[str, Any]:
    result: dict[str, Any] = {}

    # Extract provider list from quotes. BRAP quotes may appear as either:
    # 1) {"quotes": [...], "best_quote": {...}}
    # 2) {"quotes": {"all_quotes": [...], "best_quote": {...}, "quote_count": N}}
    all_quotes: list[dict[str, Any]] = []
    raw_quotes = quote_data.get("quotes", [])
    quote_count = None

    if isinstance(raw_quotes, list):
        all_quotes = [q for q in raw_quotes if isinstance(q, dict)]
    elif isinstance(raw_quotes, dict):
        nested = raw_quotes.get("all_quotes") or raw_quotes.get("quotes") or []
        if isinstance(nested, list):
            all_quotes = [q for q in nested if isinstance(q, dict)]
        qc = raw_quotes.get("quote_count")
        try:
            quote_count = int(qc) if qc is not None else None
        except (TypeError, ValueError):
            quote_count = None

    providers: list[str] = []
    seen: set[str] = set()
    for q in all_quotes:
        p = q.get("provider")
        if not p:
            continue
        p_str = str(p)
        if p_str in seen:
            continue
        seen.add(p_str)
        providers.append(p_str)

    if providers:
        result["providers"] = providers
    result["quote_count"] = quote_count if quote_count is not None else len(all_quotes)

    if isinstance(best_quote, dict):
        result["best"] = {
            "provider": best_quote.get("provider"),
            "input_amount": best_quote.get("input_amount"),
            "output_amount": best_quote.get("output_amount"),
            "input_usd": best_quote.get("input_amount_usd"),
            "output_usd": best_quote.get("output_amount_usd"),
        }
        fee = best_quote.get("fee_estimate")
        if isinstance(fee, dict):
            result["best"]["fee_usd"] = fee.get("fee_total_usd")
        quote_inner = best_quote.get("quote", {})
        if isinstance(quote_inner, dict):
            route = quote_inner.get("route", [])
            if isinstance(route, list):
                result["best"]["route"] = [
                    r.get("protocol")
                    for r in route
                    if isinstance(r, dict) and r.get("protocol")
                ]
            steps = quote_inner.get("includedSteps", [])
            if isinstance(steps, list) and not result["best"].get("route"):
                result["best"]["route"] = [
                    s.get("tool")
                    for s in steps
                    if isinstance(s, dict) and s.get("tool")
                ]

    return result


def _make_sign_callback(private_key: str):
    account = Account.from_key(private_key)

    async def sign_callback(transaction: dict) -> bytes:
        signed = account.sign_transaction(transaction)
        return signed.raw_transaction

    return sign_callback


async def _broadcast(
    sign_callback,
    tx: dict[str, Any],
    *,
    chain_id: int,
) -> tuple[bool, dict[str, Any]]:
    try:
        txn_hash = await send_transaction(tx, sign_callback, wait_for_receipt=True)
        result: dict[str, Any] = {"txn_hash": txn_hash, "chain_id": chain_id}
        explorer_link = get_etherscan_transaction_link(chain_id, txn_hash)
        if explorer_link:
            result["explorer_url"] = explorer_link
        return True, result
    except Exception as e:
        return False, {"error": _sanitize_for_json(str(e)), "chain_id": chain_id}


async def _ensure_allowance(
    *,
    sign_callback,
    chain_id: int,
    token_address: str,
    owner: str,
    spender: str,
    amount: int,
) -> tuple[bool, dict[str, Any] | None]:
    sent_ok, txn_hash = await ensure_allowance(
        token_address=token_address,
        owner=owner,
        spender=spender,
        amount=amount,
        chain_id=chain_id,
        signing_callback=sign_callback,
    )
    if not txn_hash:
        return sent_ok, None
    result: dict[str, Any] = {"txn_hash": txn_hash, "chain_id": chain_id}
    explorer_link = get_etherscan_transaction_link(chain_id, txn_hash)
    if explorer_link:
        result["explorer_url"] = explorer_link
    return sent_ok, result


def _annotate_profile(
    *,
    address: str,
    label: str,
    protocol: str,
    action: str,
    status: str,
    chain_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    store = WalletProfileStore.default()
    store.annotate_safe(
        address=address,
        label=label,
        protocol=protocol,
        action=action,
        tool="execute",
        status=status,
        chain_id=chain_id,
        details=details,
    )


async def execute(
    *,
    kind: Literal["swap", "send", "hyperliquid_deposit"],
    wallet_label: str,
    amount: str,
    # Shared optional
    recipient: str | None = None,
    # swap-only
    from_token: str | None = None,
    to_token: str | None = None,
    slippage_bps: int = 50,
    deadline_seconds: int = 300,
    # send-only
    token: str | None = None,
    chain_id: int | None = None,
    # Control
    force: bool = False,
) -> dict[str, Any]:
    request_data = {
        "kind": kind,
        "wallet_label": wallet_label,
        "amount": amount,
        "recipient": recipient,
        "from_token": from_token,
        "to_token": to_token,
        "slippage_bps": slippage_bps,
        "deadline_seconds": deadline_seconds,
        "token": token,
        "chain_id": chain_id,
    }
    try:
        req = ExecutionRequest.model_validate(request_data)
    except ValidationError as exc:
        # Extract serializable error details (exc.errors() contains raw exception objects that can't be JSON-serialized)
        error_details = [
            {"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()
        ]
        return err(
            "invalid_request", "execute.request validation failed", error_details
        )

    tool_input = {
        "request": req.model_dump(mode="json"),
    }
    preview_obj = build_execution_preview(tool_input)
    preview_text = str(preview_obj.get("summary") or "").strip()

    w = find_wallet_by_label(req.wallet_label)
    if not w:
        return err("not_found", f"Unknown wallet_label: {req.wallet_label}")

    sender = normalize_address(w.get("address"))
    pk = (
        (w.get("private_key") or w.get("private_key_hex"))
        if isinstance(w, dict)
        else None
    )
    if not sender or not pk:
        response = err(
            "invalid_wallet",
            "Wallet must include address and private_key_hex in config.json (local dev only)",
            {"wallet_label": req.wallet_label},
        )
        return response

    sign_callback = _make_sign_callback(pk)

    if req.kind == "swap":
        rcpt = normalize_address(req.recipient) or sender
        response: dict[str, Any] = {
            "kind": "swap",
            "sender": sender,
            "recipient": rcpt,
            "preview": preview_text,
            "effects": {},
        }
        try:
            from_q, from_meta = await _resolve_token_meta(str(req.from_token))
            to_q, to_meta = await _resolve_token_meta(str(req.to_token))
        except Exception as exc:  # noqa: BLE001
            response = err("token_error", str(exc))
            return response

        from_chain_id, from_token_addr = _select_token_chain(from_meta, query=from_q)
        to_chain_id, to_token_addr = _select_token_chain(to_meta, query=to_q)
        if from_chain_id is None or to_chain_id is None:
            response = err(
                "invalid_token",
                "Could not resolve chain_id for one or more tokens",
                {"from_chain_id": from_chain_id, "to_chain_id": to_chain_id},
            )
            return response
        if not from_token_addr or not to_token_addr:
            response = err(
                "invalid_token",
                "Could not resolve token address for one or more tokens",
                {
                    "from_token_address": from_token_addr,
                    "to_token_address": to_token_addr,
                },
            )
            return response

        decimals = int(from_meta.get("decimals") or 18)
        try:
            amount_raw = parse_amount_to_raw(req.amount, decimals)
        except ValueError as exc:
            response = err("invalid_amount", str(exc))
            return response

        try:
            quote_data = await BRAP_CLIENT.get_quote(
                from_token=from_token_addr,
                to_token=to_token_addr,
                from_chain=from_chain_id,
                to_chain=to_chain_id,
                from_wallet=sender,
                from_amount=str(amount_raw),
            )
        except Exception as exc:  # noqa: BLE001
            response = err("quote_error", str(exc))
            return response

        # BRAP quote responses have historically appeared in two shapes:
        # 1) {"quotes": [...], "best_quote": {...}}
        # 2) {"quotes": {"all_quotes": [...], "best_quote": {...}, "quote_count": N}}
        best_quote = None
        if isinstance(quote_data, dict):
            if isinstance(quote_data.get("best_quote"), dict):
                best_quote = quote_data.get("best_quote")
            else:
                quotes_block = quote_data.get("quotes")
                if isinstance(quotes_block, dict) and isinstance(
                    quotes_block.get("best_quote"), dict
                ):
                    best_quote = quotes_block.get("best_quote")

        if not isinstance(best_quote, dict):
            response = err(
                "quote_error", "No best_quote returned", {"quote": quote_data}
            )
            return response

        calldata = best_quote.get("calldata") or {}
        if not isinstance(calldata, dict) or not calldata:
            response = err(
                "quote_error", "best_quote missing calldata", {"best_quote": best_quote}
            )
            return response

        swap_tx = dict(calldata)
        swap_tx["chainId"] = int(from_chain_id)
        swap_tx["from"] = to_checksum_address(sender)
        if "value" in swap_tx:
            swap_tx["value"] = int(swap_tx["value"])

        token_addr = from_token_addr
        spender = swap_tx.get("to")
        approve_amount = (
            best_quote.get("input_amount")
            or best_quote.get("inputAmount")
            or best_quote.get("amount1")
            or best_quote.get("amount")
        )

        if (
            token_addr
            and isinstance(token_addr, str)
            and token_addr.strip()
            and token_addr.lower() != ZERO_ADDRESS.lower()
            and spender
            and approve_amount is not None
        ):
            try:
                need = int(approve_amount)
            except Exception:
                need = int(amount_raw)
            ok_allow, approval_tx = await _ensure_allowance(
                sign_callback=sign_callback,
                chain_id=int(from_chain_id),
                token_address=token_addr,
                owner=to_checksum_address(sender),
                spender=to_checksum_address(str(spender)),
                amount=need,
            )
            if approval_tx:
                response["effects"]["approval"] = approval_tx
            if not ok_allow:
                response["status"] = "failed"
                response["raw"] = _compact_quote(quote_data, None)
                return ok(response)

        sent_ok, sent = await _broadcast(
            sign_callback, swap_tx, chain_id=int(from_chain_id)
        )
        response["effects"]["swap"] = sent

        status = "confirmed" if sent_ok else "failed"
        response["status"] = status
        response["raw"] = _compact_quote(quote_data, best_quote)

        _annotate_profile(
            address=sender,
            label=req.wallet_label,
            protocol="brap",
            action="swap",
            status=status,
            chain_id=int(from_chain_id),
            details={
                "from_token": str(req.from_token),
                "to_token": str(req.to_token),
                "amount": req.amount,
            },
        )

        return ok(response)

    if req.kind == "send":
        recipient = normalize_address(req.recipient)
        token_q = str(req.token or "").strip()
        is_native = token_q.lower() == "native"
        response: dict[str, Any] = {
            "kind": req.kind,
            "sender": sender,
            "recipient": recipient,
            "preview": preview_text,
            "effects": {},
        }

        if is_native:
            chain_id = int(req.chain_id or 0)
            if chain_id <= 0:
                response = err(
                    "invalid_request", "chain_id must be provided for native sends"
                )
                return response
            token_address = ZERO_ADDRESS
            decimals = 18
            token_meta = None
        else:
            try:
                token_meta = await TOKEN_CLIENT.get_token_details(token_q)
            except Exception as exc:  # noqa: BLE001
                response = err("token_error", str(exc))
                return response

            token_address = str(token_meta.get("address") or "").strip()
            chain_id = _chain_id_from_token(token_meta)
            if not token_address or chain_id is None:
                response = err(
                    "invalid_token",
                    "Token missing address/chain_id",
                    {"token": token_meta},
                )
                return response
            decimals = int(token_meta.get("decimals") or 18)

        try:
            amount_raw = parse_amount_to_raw(req.amount, decimals)
        except ValueError as exc:
            response = err("invalid_amount", str(exc))
            return response

        transaction = await build_send_transaction(
            from_address=sender,
            to_address=recipient,
            token_address=token_address,
            chain_id=int(chain_id),
            amount=int(amount_raw),
        )

        sent_ok, sent = await _broadcast(
            sign_callback, transaction, chain_id=int(chain_id)
        )
        label = "send_native" if is_native else "send_erc20"
        response["effects"][label] = sent

        status = "confirmed" if sent_ok else "failed"
        response["status"] = status
        response["raw"] = {"transaction": transaction}
        if token_meta:
            response["raw"]["token"] = token_meta

        _annotate_profile(
            address=sender,
            label=req.wallet_label,
            protocol="balance",
            action=label,
            status=status,
            chain_id=int(chain_id),
            details={"recipient": recipient, "amount": req.amount, "token": token_q},
        )

        return ok(response)

    if req.kind == "hyperliquid_deposit":
        recipient = normalize_address(HYPERLIQUID_BRIDGE_ADDRESS)
        chain_id = 42161
        token_address = ARBITRUM_USDC_ADDRESS
        decimals = 6
        response: dict[str, Any] = {
            "kind": req.kind,
            "sender": sender,
            "recipient": recipient,
            "preview": preview_text,
            "effects": {},
        }

        try:
            amount_raw = parse_amount_to_raw(req.amount, decimals)
        except ValueError as exc:
            response = err("invalid_amount", str(exc))
            return response

        transaction = await build_send_transaction(
            from_address=sender,
            to_address=recipient,
            token_address=token_address,
            chain_id=chain_id,
            amount=int(amount_raw),
        )

        sent_ok, sent = await _broadcast(sign_callback, transaction, chain_id=chain_id)
        response["effects"]["deposit"] = sent

        status = "confirmed" if sent_ok else "failed"
        response["status"] = status
        response["raw"] = {"transaction": transaction}

        _annotate_profile(
            address=sender,
            label=req.wallet_label,
            protocol="hyperliquid",
            action="hyperliquid_deposit",
            status=status,
            chain_id=chain_id,
            details={"recipient": recipient, "amount": req.amount},
        )

        return ok(response)

    return err("invalid_request", f"Unknown kind: {req.kind}")
