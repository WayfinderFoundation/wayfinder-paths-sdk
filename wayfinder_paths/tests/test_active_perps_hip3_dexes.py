"""Verify ActivePerpsStrategy accepts every live HIP-3 dex in HIP3_DEXES."""

import pytest

from wayfinder_paths.core.strategies.active_perps import ActivePerpsStrategy


def _declare(ref, dexes):
    return type(
        "HIP3Strategy",
        (ActivePerpsStrategy,),
        {"REF": str(ref), "SIGNAL": "json:loads", "HIP3_DEXES": dexes},
    )


@pytest.mark.parametrize(
    "dex", ["xyz", "flx", "vntl", "hyna", "km", "abcd", "cash", "para", "mkts", "io"]
)
def test_hip3_dexes_accepts_live_dex(tmp_path, dex):
    ref = tmp_path / "ref.json"
    ref.write_text("{}")

    cls = _declare(ref, [dex])

    assert cls.HIP3_DEXES == [dex]


def test_hip3_dexes_rejects_unknown_dex(tmp_path):
    ref = tmp_path / "ref.json"
    ref.write_text("{}")

    with pytest.raises(RuntimeError, match="unknown dex 'nope'"):
        _declare(ref, ["nope"])
