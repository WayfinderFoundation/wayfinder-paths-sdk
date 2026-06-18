from __future__ import annotations

import json
from pathlib import Path

import pytest

from wayfinder_paths.core import packs


def _surface_pack() -> dict:
    return {
        "packType": "surfacePack",
        "domain": "sports",
        "intent": "test",
        "observedAt": "2099-06-17T15:30:00Z",
        "scope": {"event": "worldcup"},
        "summary": "test surface",
        "payload": {"markets": [{"id": "m1", "ask": 0.42}]},
        "reusePolicy": {
            "canReuseFor": ["analysis", "final_answer"],
            "mustRehydrateBefore": ["execute", "place_order", "recommend_buy"],
            "ttlSeconds": 60,
        },
        "lineage": {"createdBy": "test", "consumedPacks": [], "refreshedFields": []},
    }


def test_write_pack_creates_path_and_index_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(packs.PACKS_ROOT_ENV, str(tmp_path / "packs"))

    ref = packs.write_pack(_surface_pack())

    assert ref["packType"] == "surfacePack"
    path = Path(ref["path"])
    assert path.exists()
    index = tmp_path / "packs" / "index.jsonl"
    assert index.exists()
    assert json.loads(index.read_text().splitlines()[0])["packId"] == ref["packId"]


def test_read_pack_by_id_and_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(packs.PACKS_ROOT_ENV, str(tmp_path / "packs"))
    ref = packs.write_pack(_surface_pack())

    by_id = packs.read_pack(str(ref["packId"]))
    by_path = packs.read_pack(str(ref["path"]))

    assert by_id["packId"] == ref["packId"]
    assert by_path["packId"] == ref["packId"]


def test_latest_pack_filters_by_domain_type_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(packs.PACKS_ROOT_ENV, str(tmp_path / "packs"))
    ref = packs.write_pack(_surface_pack())
    pack = packs.read_pack(str(ref["packId"]))

    latest = packs.latest_pack(
        domain="sports",
        pack_type="surfacePack",
        scope_hash=pack["scopeHash"],
    )

    assert latest is not None
    assert latest["packId"] == ref["packId"]


def test_pack_schema_requires_valid_until_or_ttl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(packs.PACKS_ROOT_ENV, str(tmp_path / "packs"))
    pack = _surface_pack()
    pack["reusePolicy"].pop("ttlSeconds")

    with pytest.raises(ValueError, match="validUntil"):
        packs.write_pack(pack)


def test_surface_pack_requires_rehydrate_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(packs.PACKS_ROOT_ENV, str(tmp_path / "packs"))
    pack = _surface_pack()
    pack["reusePolicy"].pop("mustRehydrateBefore")

    with pytest.raises(ValueError, match="mustRehydrateBefore"):
        packs.write_pack(pack)


def test_mark_pack_stale_updates_file_and_latest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(packs.PACKS_ROOT_ENV, str(tmp_path / "packs"))
    ref = packs.write_pack(_surface_pack())

    packs.mark_pack_stale(str(ref["packId"]), reason="test")
    stale = packs.read_pack(str(ref["packId"]))

    assert stale["stale"] is True
    assert packs.latest_pack(domain="sports", pack_type="surfacePack") is None


def test_pack_ref_is_compact_and_contains_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(packs.PACKS_ROOT_ENV, str(tmp_path / "packs"))
    ref = packs.write_pack(_surface_pack())

    assert set(ref) == {
        "packId",
        "packType",
        "domain",
        "path",
        "observedAt",
        "validUntil",
        "summary",
    }
