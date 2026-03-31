from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

from wayfinder_paths.core.config import get_remote_wallet_policy_ttl_setting

WAYFINDER_TIMEBOUND_RULE_SUFFIX = " [wayfinder-timebound]"
TIMEBOUND_POLICY_ERROR = (
    "Time-bound policy management requires dict-based Privy rules; "
    "string-based policies are not supported."
)

PolicyRule = dict[str, Any]
PolicyEntry = str | PolicyRule
PolicyList = list[PolicyEntry]


def _copy_rule(rule: PolicyRule) -> PolicyRule:
    return copy.deepcopy(rule)


def _isoformat_timestamp(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _normalize_rule_name(name: Any) -> str:
    normalized = str(name or "").strip()
    return normalized or "Allow rule"


def _mark_managed_rule_name(name: Any) -> str:
    normalized = _normalize_rule_name(name)
    if normalized.endswith(WAYFINDER_TIMEBOUND_RULE_SUFFIX):
        return normalized
    return f"{normalized}{WAYFINDER_TIMEBOUND_RULE_SUFFIX}"


def _strip_managed_rule_name(name: Any) -> str:
    normalized = _normalize_rule_name(name)
    if normalized.endswith(WAYFINDER_TIMEBOUND_RULE_SUFFIX):
        normalized = normalized[: -len(WAYFINDER_TIMEBOUND_RULE_SUFFIX)].rstrip()
    return normalized or "Allow rule"


def _is_managed_timebound_condition(condition: Any) -> bool:
    if not isinstance(condition, dict):
        return False
    return (
        condition.get("field_source") == "system"
        and condition.get("field") == "current_unix_timestamp"
        and condition.get("operator") == "lt"
    )


def _make_timebound_condition(expires_at_unix: int) -> dict[str, str]:
    return {
        "field_source": "system",
        "field": "current_unix_timestamp",
        "operator": "lt",
        "value": str(expires_at_unix),
    }


def require_dict_policy_rules(policies: PolicyList | list[Any]) -> list[PolicyRule]:
    if not isinstance(policies, list):
        raise TypeError(TIMEBOUND_POLICY_ERROR)
    if any(not isinstance(rule, dict) for rule in policies):
        raise TypeError(TIMEBOUND_POLICY_ERROR)
    return [_copy_rule(rule) for rule in policies]


def strip_managed_timebound_rules(policies: list[PolicyRule]) -> list[PolicyRule]:
    stripped: list[PolicyRule] = []
    for original_rule in policies:
        rule = _copy_rule(original_rule)
        conditions = rule.get("conditions")
        if isinstance(conditions, list):
            rule["conditions"] = [
                copy.deepcopy(condition)
                for condition in conditions
                if not _is_managed_timebound_condition(condition)
            ]
        rule["name"] = _strip_managed_rule_name(rule.get("name"))
        stripped.append(rule)
    return stripped


def build_policy_status(
    *,
    expires_at_unix: int | None,
    effective_ttl_seconds: int,
    ttl_source: str,
    source: str,
    now_unix: int | None = None,
) -> dict[str, Any]:
    now = int(datetime.now(UTC).timestamp()) if now_unix is None else now_unix
    remaining_seconds = None
    if expires_at_unix is not None:
        remaining_seconds = max(0, expires_at_unix - now)
    return {
        "time_bound": expires_at_unix is not None,
        "effective_ttl_seconds": effective_ttl_seconds,
        "ttl_source": ttl_source,
        "expires_at": _isoformat_timestamp(expires_at_unix),
        "remaining_seconds": remaining_seconds,
        "source": source,
    }


def apply_timebound_rules(
    policies: list[PolicyRule],
    *,
    ttl_seconds: int,
    ttl_source: str,
    source: str,
    now_unix: int | None = None,
) -> tuple[list[PolicyRule], dict[str, Any]]:
    base_rules = strip_managed_timebound_rules(policies)
    now = int(datetime.now(UTC).timestamp()) if now_unix is None else now_unix

    if ttl_seconds <= 0:
        return base_rules, build_policy_status(
            expires_at_unix=None,
            effective_ttl_seconds=0,
            ttl_source=ttl_source,
            source=source,
            now_unix=now,
        )

    expires_at_unix = now + ttl_seconds
    managed_rules: list[PolicyRule] = []
    applied = False
    for original_rule in base_rules:
        rule = _copy_rule(original_rule)
        if str(rule.get("action") or "").upper() == "ALLOW":
            conditions = rule.get("conditions")
            if not isinstance(conditions, list):
                conditions = []
            rule["conditions"] = [
                copy.deepcopy(condition) for condition in conditions
            ] + [_make_timebound_condition(expires_at_unix)]
            rule["name"] = _mark_managed_rule_name(rule.get("name"))
            applied = True
        managed_rules.append(rule)

    return managed_rules, build_policy_status(
        expires_at_unix=expires_at_unix if applied else None,
        effective_ttl_seconds=ttl_seconds,
        ttl_source=ttl_source,
        source=source,
        now_unix=now,
    )


def preview_timebound_rules(
    policies: PolicyList | list[Any],
    *,
    now_unix: int | None = None,
) -> tuple[list[PolicyRule], dict[str, Any]]:
    ttl_seconds, ttl_source = get_remote_wallet_policy_ttl_setting()
    rule_policies = require_dict_policy_rules(policies)
    return apply_timebound_rules(
        rule_policies,
        ttl_seconds=ttl_seconds,
        ttl_source=ttl_source,
        source="preview",
        now_unix=now_unix,
    )


def extract_policy_rules(payload: Any) -> list[PolicyRule]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return require_dict_policy_rules(payload)
    if not isinstance(payload, dict):
        raise TypeError(TIMEBOUND_POLICY_ERROR)

    for key in ("rules", "policy", "override_policy"):
        if key in payload:
            return extract_policy_rules(payload.get(key))

    policies = payload.get("policies")
    if isinstance(policies, list):
        if not policies:
            return []
        if all(isinstance(item, dict) and "rules" in item for item in policies):
            extracted_rules: list[PolicyRule] = []
            for policy in policies:
                extracted_rules.extend(extract_policy_rules(policy))
            return extracted_rules
        if all(isinstance(item, dict) for item in policies):
            return require_dict_policy_rules(policies)

    if {"method", "action"}.issubset(payload.keys()):
        return require_dict_policy_rules([payload])

    return []


def extract_managed_expiry_from_rules(policies: list[PolicyRule]) -> int | None:
    expiries: list[int] = []
    for rule in policies:
        if not isinstance(rule, dict):
            continue
        for condition in (
            rule.get("conditions", [])
            if isinstance(rule.get("conditions"), list)
            else []
        ):
            if not _is_managed_timebound_condition(condition):
                continue
            value = condition.get("value")
            try:
                expiries.append(int(str(value)))
            except (TypeError, ValueError):
                continue
    if not expiries:
        return None
    return min(expiries)


def build_remote_policy_status(
    policy_payload: Any,
    *,
    now_unix: int | None = None,
) -> dict[str, Any]:
    ttl_seconds, ttl_source = get_remote_wallet_policy_ttl_setting()
    effective_ttl_seconds = 0 if ttl_source == "disabled" else ttl_seconds
    rules = extract_policy_rules(policy_payload)
    expires_at_unix = extract_managed_expiry_from_rules(rules)
    return build_policy_status(
        expires_at_unix=expires_at_unix,
        effective_ttl_seconds=effective_ttl_seconds,
        ttl_source=ttl_source,
        source="remote",
        now_unix=now_unix,
    )
