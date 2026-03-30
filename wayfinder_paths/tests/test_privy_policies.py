from __future__ import annotations

import copy

import pytest

import wayfinder_paths.core.config as config
from wayfinder_paths.core.utils.privy_policies import (
    TIMEBOUND_POLICY_ERROR,
    WAYFINDER_TIMEBOUND_RULE_SUFFIX,
    apply_timebound_rules,
    build_remote_policy_status,
    extract_policy_rules,
    preview_timebound_rules,
    strip_managed_timebound_rules,
)


@pytest.fixture
def restore_global_config() -> None:
    original = copy.deepcopy(config.CONFIG)
    yield
    config.set_config(original)


def _base_rules() -> list[dict]:
    return [
        {
            "name": "Allow router swap",
            "method": "eth_signTransaction",
            "action": "ALLOW",
            "conditions": [
                {
                    "field_source": "ethereum_transaction",
                    "field": "to",
                    "operator": "eq",
                    "value": "0xrouter",
                }
            ],
        },
        {
            "name": "Block export",
            "method": "exportPrivateKey",
            "action": "DENY",
            "conditions": [],
        },
    ]


def test_apply_timebound_rules_wraps_allow_only() -> None:
    managed_rules, policy_status = apply_timebound_rules(
        _base_rules(),
        ttl_seconds=3600,
        ttl_source="config",
        source="preview",
        now_unix=1_000,
    )

    allow_rule = managed_rules[0]
    deny_rule = managed_rules[1]

    assert allow_rule["name"].endswith(WAYFINDER_TIMEBOUND_RULE_SUFFIX)
    assert allow_rule["conditions"][-1] == {
        "field_source": "system",
        "field": "current_unix_timestamp",
        "operator": "lt",
        "value": "4600",
    }
    assert deny_rule["name"] == "Block export"
    assert deny_rule["conditions"] == []
    assert policy_status["time_bound"] is True
    assert policy_status["effective_ttl_seconds"] == 3600
    assert policy_status["ttl_source"] == "config"
    assert policy_status["remaining_seconds"] == 3600


def test_strip_and_reapply_timebound_rules_is_idempotent() -> None:
    managed_once, _ = apply_timebound_rules(
        _base_rules(),
        ttl_seconds=3600,
        ttl_source="config",
        source="preview",
        now_unix=500,
    )
    stripped = strip_managed_timebound_rules(managed_once)
    managed_twice, _ = apply_timebound_rules(
        stripped,
        ttl_seconds=3600,
        ttl_source="config",
        source="preview",
        now_unix=500,
    )

    assert managed_once == managed_twice


def test_preview_timebound_rules_uses_config_defaults(
    restore_global_config: None,
) -> None:
    config.set_config({})

    managed_rules, policy_status = preview_timebound_rules(
        _base_rules(),
        now_unix=10_000,
    )

    assert managed_rules[0]["name"].endswith(WAYFINDER_TIMEBOUND_RULE_SUFFIX)
    assert policy_status["effective_ttl_seconds"] == 3600
    assert policy_status["ttl_source"] == "built_in_default"
    assert policy_status["remaining_seconds"] == 3600
    assert policy_status["expires_at"] == "1970-01-01T03:46:40+00:00"


def test_preview_timebound_rules_zero_disables_wrapping(
    restore_global_config: None,
) -> None:
    config.set_config({"system": {"remote_wallet_policy": {"default_ttl_seconds": 0}}})

    managed_rules, policy_status = preview_timebound_rules(
        _base_rules(),
        now_unix=123,
    )

    assert managed_rules == _base_rules()
    assert policy_status == {
        "time_bound": False,
        "effective_ttl_seconds": 0,
        "ttl_source": "disabled",
        "expires_at": None,
        "remaining_seconds": None,
        "source": "preview",
    }


def test_preview_timebound_rules_rejects_string_based_policies() -> None:
    with pytest.raises(TypeError, match=TIMEBOUND_POLICY_ERROR):
        preview_timebound_rules(["legacy string policy"], now_unix=0)


def test_extract_policy_rules_flattens_multiple_wrapped_policies() -> None:
    first_rule, second_rule = _base_rules()

    extracted = extract_policy_rules(
        {
            "policies": [
                {"rules": [first_rule]},
                {"rules": [second_rule]},
            ]
        }
    )

    assert extracted == [first_rule, second_rule]


def test_build_remote_policy_status_reads_live_expiry(
    restore_global_config: None,
) -> None:
    config.set_config(
        {"system": {"remote_wallet_policy": {"default_ttl_seconds": 900}}}
    )
    managed_rules, _ = apply_timebound_rules(
        _base_rules(),
        ttl_seconds=900,
        ttl_source="config",
        source="preview",
        now_unix=5_000,
    )

    policy_status = build_remote_policy_status(
        {"rules": managed_rules},
        now_unix=5_100,
    )

    assert policy_status == {
        "time_bound": True,
        "effective_ttl_seconds": 900,
        "ttl_source": "config",
        "expires_at": "1970-01-01T01:38:20+00:00",
        "remaining_seconds": 800,
        "source": "remote",
    }
