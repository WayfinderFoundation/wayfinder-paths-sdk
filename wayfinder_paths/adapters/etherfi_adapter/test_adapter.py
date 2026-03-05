import pytest

from wayfinder_paths.adapters.etherfi_adapter.adapter import EtherfiAdapter
from wayfinder_paths.core.constants.chains import CHAIN_ID_ETHEREUM


def test_adapter_type():
    assert EtherfiAdapter.adapter_type == "ETHERFI"


def test_get_weeth_address_mainnet():
    assert (
        EtherfiAdapter.get_weeth_address(CHAIN_ID_ETHEREUM)
        == "0xCd5fE23C85820F7B72D0926FC9b05b43E359b7ee"
    )


@pytest.mark.asyncio
async def test_unsupported_chain_returns_error():
    adapter = EtherfiAdapter(config={})
    ok, err = await adapter.get_pos(
        account="0x0000000000000000000000000000000000000000",
        chain_id=0,
    )
    assert ok is False
    assert isinstance(err, str) and err
