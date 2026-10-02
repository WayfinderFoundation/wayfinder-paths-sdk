from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from wayfinder_paths.adapters.reward_participation_adapter.adapter import (
    RewardParticipationAdapter,
)
from wayfinder_paths.paths.participation import ParticipationConfig, participation_tick


async def tick() -> None:
    params = json.loads(os.environ["WAYFINDER_PATH_PARAMS"])
    config = ParticipationConfig.model_validate(params["participation"])
    dry_run = (
        os.environ.get("WAYFINDER_PATH_DRY_RUN", "1") != "0"
        or os.environ.get("WAYFINDER_JOB_MODE") != "live"
    )
    adapter = RewardParticipationAdapter(
        config=config.model_dump(mode="json"),
        risex_token=os.environ.get("RISEX_JWT"),
        imd_seat_id=params.get("imd_seat_id"),
    )
    try:
        result = await participation_tick(
            config,
            adapter,
            state_dir=Path(os.environ["WAYFINDER_PATH_STATE_DIR"]),
            now=time.time(),
            dry_run=dry_run,
        )
        print(
            "WAYFINDER_PATH_EVENT "
            + json.dumps({"type": "participation", "payload": result})
        )
    finally:
        await adapter.close()


def main() -> None:
    asyncio.run(tick())


if __name__ == "__main__":
    main()
