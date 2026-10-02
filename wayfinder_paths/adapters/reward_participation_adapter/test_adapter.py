from collections.abc import AsyncIterator

import pytest

from wayfinder_paths.adapters.reward_participation_adapter.adapter import (
    RewardParticipationAdapter,
)


class TestRewardParticipationAdapter:
    @pytest.fixture
    async def adapter(self) -> AsyncIterator[RewardParticipationAdapter]:
        adapter = RewardParticipationAdapter()
        yield adapter
        await adapter.close()

    def test_init(self, adapter: RewardParticipationAdapter) -> None:
        assert adapter.adapter_type == "REWARD_PARTICIPATION"
        assert adapter.name == "reward_participation_adapter"
