"""Resolver tests pinned to real on-chain vectors from the 2026-06-29 factory
upgrade incident (49 pUSD stranded at an undeployed legacy address)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from wayfinder_paths.core.constants.polymarket import derive_legacy_deposit_wallet
from wayfinder_paths.core.utils import polymarket_wallet

# Incident vectors, verified live on Polygon (see PR description):
INCIDENT_OWNER = "0xbEcEcB80E7343222b7218fC2633754Bc56240Ff5"
INCIDENT_LEGACY = "0x083fc209728e610f29f770ee46e79722b4eA444e"
INCIDENT_PREDICTED = "0x0ab274fBf20Ca7c688aDAEFC93040669B1C68fdC"
# Pre-upgrade cohort: wallet deployed under the old scheme stays canonical.
COHORT2_OWNER = "0x89725d4dF2E7B2Fc8eDAA9Ae20E36B66B9bB5C03"
COHORT2_LEGACY = "0x7810Aebf0c85e394D29067c3cD62Bb89a7162acb"


@pytest.fixture(autouse=True)
def _clean_cache():
    polymarket_wallet._RESOLVED.clear()
    yield
    polymarket_wallet._RESOLVED.clear()


def _fake_batch(code_result: str, predict_result: str):
    calls: list[list[dict[str, Any]]] = []

    def fake(batch):
        calls.append(batch)
        results = []
        for item in batch:
            if item["method"] == "eth_getCode":
                results.append(code_result)
            else:
                results.append(predict_result)
        return results

    return fake, calls


def test_legacy_derivation_matches_incident_vector():
    assert derive_legacy_deposit_wallet(INCIDENT_OWNER) == INCIDENT_LEGACY
    assert derive_legacy_deposit_wallet(COHORT2_OWNER) == COHORT2_LEGACY


def test_undeployed_legacy_resolves_to_factory_prediction():
    fake, calls = _fake_batch(
        "0x", "0x" + INCIDENT_PREDICTED[2:].lower().rjust(64, "0")
    )
    with patch.object(polymarket_wallet, "_rpc_batch", fake):
        resolved = polymarket_wallet.resolve_deposit_wallet_sync(INCIDENT_OWNER)
    assert resolved == INCIDENT_PREDICTED
    assert len(calls) == 1


def test_deployed_legacy_wallet_stays_canonical():
    fake, _ = _fake_batch("0x363d3d373d", "0x" + "9" * 64)
    with patch.object(polymarket_wallet, "_rpc_batch", fake):
        resolved = polymarket_wallet.resolve_deposit_wallet_sync(COHORT2_OWNER)
    assert resolved == COHORT2_LEGACY


def test_result_is_cached_after_first_resolution():
    fake, calls = _fake_batch("0x363d3d373d", "0x" + "9" * 64)
    with patch.object(polymarket_wallet, "_rpc_batch", fake):
        first = polymarket_wallet.resolve_deposit_wallet_sync(COHORT2_OWNER)
        second = polymarket_wallet.resolve_deposit_wallet_sync(COHORT2_OWNER)
    assert first == second
    assert len(calls) == 1


def test_rpc_failure_raises_and_caches_nothing():
    def failing(_batch):
        raise RuntimeError("All Polygon RPCs failed")

    with patch.object(polymarket_wallet, "_rpc_batch", failing):
        with pytest.raises(RuntimeError, match="All Polygon RPCs failed"):
            polymarket_wallet.resolve_deposit_wallet_sync(INCIDENT_OWNER)
    assert polymarket_wallet._RESOLVED == {}


def test_zero_address_prediction_rejected():
    fake, _ = _fake_batch("0x", "0x" + "0" * 64)
    with patch.object(polymarket_wallet, "_rpc_batch", fake):
        with pytest.raises(RuntimeError, match="zero address"):
            polymarket_wallet.resolve_deposit_wallet_sync(INCIDENT_OWNER)


def test_status_reports_stranded_funds_with_discord_guidance():
    polymarket_wallet._RESOLVED[INCIDENT_OWNER] = INCIDENT_PREDICTED

    def fake(batch):
        # eth_getCode(resolved) + two balanceOf(legacy) calls
        assert batch[0]["method"] == "eth_getCode"
        assert all(item["method"] == "eth_call" for item in batch[1:])
        return ["0x", hex(49_000_000), "0x0"]

    with patch.object(polymarket_wallet, "_rpc_batch", fake):
        status = polymarket_wallet._get_deposit_wallet_status_sync(INCIDENT_OWNER)
    assert status["scheme"] == "beacon"
    assert status["deployed"] is False
    assert status["resolved_address"] == INCIDENT_PREDICTED
    stranded = status["stranded_legacy_funds"]
    assert stranded is not None
    assert stranded["legacy_address"] == INCIDENT_LEGACY
    assert stranded["pusd_raw"] == 49_000_000
    assert polymarket_wallet.POLYMARKET_RECOVERY_DISCORD_URL in stranded["message"]
    assert polymarket_wallet.POLYMARKET_RECOVERY_DISCORD_URL in status["guidance"]


def test_status_clean_beacon_wallet_has_no_banner_state():
    polymarket_wallet._RESOLVED[INCIDENT_OWNER] = INCIDENT_PREDICTED

    def fake(batch):
        return ["0x363d3d373d", "0x0", "0x0"]

    with patch.object(polymarket_wallet, "_rpc_batch", fake):
        status = polymarket_wallet._get_deposit_wallet_status_sync(INCIDENT_OWNER)
    assert status["scheme"] == "beacon"
    assert status["deployed"] is True
    assert status["stranded_legacy_funds"] is None


def test_status_short_circuits_for_legacy_canonical_wallet():
    polymarket_wallet._RESOLVED[COHORT2_OWNER] = COHORT2_LEGACY

    def must_not_call(_batch):
        raise AssertionError("no RPC expected when resolved == legacy")

    with patch.object(polymarket_wallet, "_rpc_batch", must_not_call):
        status = polymarket_wallet._get_deposit_wallet_status_sync(COHORT2_OWNER)
    assert status["scheme"] == "legacy"
    assert status["deployed"] is True
    assert status["stranded_legacy_funds"] is None
