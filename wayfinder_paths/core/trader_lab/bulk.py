"""Download a Trader Lab release to a local cache (billed per byte; see the
using-trader-lab skill). Files are sha256-verified and re-downloaded only
when missing or changed, so repeated calls are free."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from wayfinder_paths.core.clients.TraderLabClient import (
    TRADER_LAB_CLIENT,
    TraderLabClient,
)
from wayfinder_paths.core.trader_lab._safety import (
    DATASET,
    UnsafeReleaseData,
    check_file_path,
    check_release,
    inside,
)

MANIFEST = "manifest.json"


def cache_root() -> Path:
    override = os.environ.get("WAYFINDER_TRADER_LAB_CACHE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "wayfinder" / "trader-lab"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def plan(listing: dict[str, Any], datasets: list[str] | None, dest: Path) -> list[dict]:
    """Files to fetch: those missing locally or whose sha256 differs."""
    wanted = set(datasets) if datasets else None
    todo = []
    for ds in listing["data"]["datasets"]:
        if not isinstance(ds.get("name"), str) or not DATASET.match(ds["name"]):
            raise UnsafeReleaseData(f"unexpected dataset name {ds.get('name')!r}")
        if wanted is not None and ds["name"] not in wanted:
            continue
        for f in ds["files"]:
            name, _file = check_file_path(f.get("path"))
            if name != ds["name"]:
                raise UnsafeReleaseData(f"{f['path']} is not in dataset {ds['name']}")
            local = inside(dest, dest / f["path"])
            if local.exists() and f.get("sha256") and _sha256(local) == f["sha256"]:
                continue
            todo.append({"dataset": ds["name"], **f})
    if wanted:
        known = {ds["name"] for ds in listing["data"]["datasets"]}
        missing = wanted - known
        if missing:
            raise ValueError(
                f"Unknown datasets: {sorted(missing)}; available: {sorted(known)}"
            )
    return todo


def estimated_bytes(listing: dict[str, Any], datasets: list[str] | None = None) -> int:
    wanted = set(datasets) if datasets else None
    return sum(
        f.get("bytes", 0)
        for ds in listing["data"]["datasets"]
        if wanted is None or ds["name"] in wanted
        for f in ds["files"]
    )


async def download_release(
    datasets: list[str] | None = None,
    *,
    dest: Path | None = None,
    client: TraderLabClient | None = None,
    max_bytes: int | None = 2 * 1024**3,
) -> Path:
    """Download (or refresh) the current release into
    ``<cache>/R=<release>/`` and return that directory.

    datasets: names to fetch (default: all). max_bytes: refuse a download
    bigger than this (default 2 GB; None = no limit) so a script can't
    run up an unexpected bill.
    """
    client = client or TRADER_LAB_CLIENT
    listing = await client.datasets()
    release = check_release(listing.get("release"))
    directory = (dest or cache_root()) / f"R={release}"
    todo = plan(listing, datasets, directory)
    total = sum(f.get("bytes", 0) for f in todo)
    if max_bytes is not None and total > max_bytes:
        raise ValueError(
            f"Download would fetch {total / 1e9:.2f} GB (> max_bytes); "
            "pass fewer datasets or a higher max_bytes"
        )
    for f in todo:
        name, file = check_file_path(f["path"])
        await client.download_dataset_file(
            name,
            file,
            inside(directory, directory / name / file),
            sha256=f.get("sha256"),
        )
    meta = {
        "release": release,
        "as_of": listing.get("as_of"),
        "rules_version": listing.get("rules_version"),
        **{
            k: listing["data"].get(k)
            for k in ("views_sql", "features_sql", "schema_version")
        },
        "datasets": [ds["name"] for ds in listing["data"]["datasets"]],
    }
    directory.mkdir(parents=True, exist_ok=True)
    (directory / MANIFEST).write_text(json.dumps(meta, indent=1))
    return directory
