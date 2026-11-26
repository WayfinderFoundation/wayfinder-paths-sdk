"""The FLOP wire encoders against the yellow paper's published vectors (flop-wire-v1)."""

import json
from pathlib import Path

import pytest
import sr25519

from wayfinder_paths.core.flop import wire

VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "flop" / "wire-format-v1.json").read_text()
)
CHANNEL = VECTORS["compute_channel_v1"]


def _leaf(version: wire.LeafVersion) -> wire.Leaf:
    inputs = CHANNEL["leaf_inputs"]
    has_policy = version >= wire.LeafVersion.V2
    is_v3 = version is wire.LeafVersion.V3
    return wire.Leaf(
        version=version,
        channel_id=bytes.fromhex(inputs["channel_id_hex"]),
        turn_index=int(inputs["turn_index"]),
        h_in=bytes.fromhex(inputs["h_in_hex"]),
        h_out=bytes.fromhex(inputs["h_out_hex"]),
        g_n=int(inputs["g_n"]),
        decode_policy_hash=bytes.fromhex(inputs["decode_policy_hash_hex"])
        if has_policy
        else None,
        h_ids=bytes.fromhex(inputs["h_ids_hex"]) if is_v3 else wire.ZERO_HASH,
        toploc_commitment_hash=bytes.fromhex(inputs["toploc_commitment_hash_hex"])
        if is_v3
        else wire.ZERO_HASH,
        miner_recv_ms=int(inputs["miner_recv_ms"]),
        miner_done_ms=int(inputs["miner_done_ms"]),
        latency_ms=int(inputs["latency_ms"]),
    )


def test_channel_id_matches_vector():
    inputs = CHANNEL["channel_id"]["inputs"]
    got = wire.channel_id(
        bytes.fromhex(inputs["genesis_hash_hex"]),
        bytes.fromhex(inputs["agent_account_id32_hex"]),
        bytes.fromhex(inputs["miner_account_id32_hex"]),
        int(inputs["nonce"]),
    )
    assert got.hex() == CHANNEL["channel_id"]["hash_hex"]


@pytest.mark.parametrize("vector", CHANNEL["leaf_versions"], ids=lambda v: v["version"])
def test_leaf_preimage_and_hash_match_vector(vector):
    leaf = _leaf(wire.LeafVersion[vector["version"]])
    assert leaf.fields_consistent()
    assert leaf.preimage().hex() == vector["preimage_hex"]
    assert leaf.hash().hex() == vector["hash_hex"]
    assert int(leaf.version) == vector["scale_tag"]


@pytest.mark.parametrize(
    "vector", VECTORS["h_ids_v1"]["vectors"], ids=lambda v: v["id"]
)
def test_h_ids_matches_vector(vector):
    got = wire.h_ids(vector["prompt_ids"], vector["generated_ids"])
    assert got.hex() == vector["hash_hex"]


def test_merkle_root_and_path_match_vector():
    merkle = CHANNEL["merkle"]
    leaves = [_leaf(wire.LeafVersion[v]).hash() for v in merkle["leaf_order"]]
    assert wire.merkle_root(leaves).hex() == merkle["root_hex"]
    path = wire.merkle_path(leaves, 2)
    assert [(s.hex(), left) for s, left in path] == [
        (p["sibling_hex"], p["sibling_is_left"]) for p in merkle["path_for_index_2"]
    ]
    assert wire.root_from_path(leaves[2], path).hex() == merkle["root_hex"]


def test_merkle_edge_cases():
    assert wire.merkle_root([]) == wire.ZERO_HASH
    leaf = wire.blake2_256(b"only")
    assert wire.merkle_root([leaf]) == leaf
    assert wire.merkle_path([leaf], 0) == []


def test_receipt_message_and_signature_match_vector():
    receipt = CHANNEL["receipt"]
    message = wire.receipt_message(
        bytes.fromhex(receipt["inputs"]["channel_id_hex"]),
        bytes.fromhex(receipt["inputs"]["final_root_hex"]),
        int(receipt["inputs"]["aggregate_gn"]),
        int(receipt["inputs"]["payable"]),
    )
    assert message.hex() == receipt["preimage_hex"]
    assert sr25519.verify(
        bytes.fromhex(receipt["signature_hex"]),
        message,
        bytes.fromhex(receipt["public_key_hex"]),
    )


def test_v3_leaf_signature_verifies_over_the_leaf_hash():
    vector = CHANNEL["v3_leaf_signature"]
    leaf = _leaf(wire.LeafVersion.V3)
    assert leaf.hash().hex() == vector["leaf_hash_hex"]
    assert sr25519.verify(
        bytes.fromhex(vector["signature_hex"]),
        leaf.hash(),
        bytes.fromhex(vector["public_key_hex"]),
    )


def test_leaf_version_field_rules():
    v3 = _leaf(wire.LeafVersion.V3)
    assert not wire.Leaf(
        **{**v3.__dict__, "version": wire.LeafVersion.V1}
    ).fields_consistent()
    assert not wire.Leaf(**{**v3.__dict__, "h_ids": wire.ZERO_HASH}).fields_consistent()


def test_integers_out_of_range_are_rejected():
    with pytest.raises(ValueError):
        wire.u32le(1 << 32)
    with pytest.raises(ValueError):
        wire.u128le(-1)


def test_force_settle_payable():
    assert (
        wire.force_settle_payable(base_per_turn=7, turn_count=3, aggregate_gn=100)
        == 121
    )


def test_session_request_message_is_compact_json_over_the_body_hash():
    message = wire.session_request_message(
        "0x" + "11" * 32,
        "POST",
        "/session/turn",
        '{"a":1}',
        "1700000000000",
        "0xabcd",
        "c",
    )
    decoded = json.loads(message)
    assert decoded[0] == "flop-session-request-v1"
    assert decoded[4] == "0x" + wire.blake2_256(b'{"a":1}').hex()
    assert b", " not in message and b": " not in message
