"""Trader Lab bulk and local analysis (optional extra: ``trader-lab``).

    from wayfinder_paths.core.trader_lab import bulk, local, features, signals

    release_dir = await bulk.download_release(datasets=["round_trips", "universe"])
    con = local.connect(release_dir)              # same views as the server
    trips = con.sql("SELECT * FROM round_trips WHERE coin = 'ETH'").df()

Modules import DuckDB lazily, so ``import wayfinder_paths`` never needs it.
"""

from wayfinder_paths.core.trader_lab import bulk, features, local, signals

__all__ = ["bulk", "features", "local", "signals"]
