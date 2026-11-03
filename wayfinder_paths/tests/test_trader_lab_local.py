"""wayfinder_paths.core.trader_lab: downloads, local views, signals."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

duckdb = pytest.importorskip("duckdb")

from wayfinder_paths.core.trader_lab import bulk, local, signals  # noqa: E402

# The server's views.sql, verbatim (services/vault-backend/.../sql/views.sql):
# the local connection must accept exactly what the release publishes.
FIXTURES = Path(__file__).parent / "fixtures"
VIEWS_SQL = (FIXTURES / "trader_lab_views.sql").read_text()
FEATURES_SQL = (FIXTURES / "trader_lab_features.sql").read_text()


def _write(con, sql: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY ({sql}) TO '{path}' (FORMAT parquet)")


@pytest.fixture
def release_dir(tmp_path) -> Path:
    d = tmp_path / "R=2026-10-30"
    con = duckdb.connect()
    _write(
        con,
        """SELECT * FROM (VALUES
             (DATE '2026-08-31', '0xa', 1, 3), (DATE '2026-08-31', '0xb', 60, 1),
             (DATE '2026-09-30', '0xb', 2, 2)) t(as_of, address, rank_sharpe, rank_net)""",
        d / "cohort_membership" / "part.parquet",
    )
    _write(
        con,
        """SELECT '0xa' AS address, 'ETH' AS coin, 1 AS side, 25.0 AS net,
                  TIMESTAMPTZ '2026-10-01 08:00:00+00' AS t0,
                  TIMESTAMPTZ '2026-10-01 14:00:00+00' AS t1""",
        d / "round_trips" / "part.parquet",
    )
    con.close()
    (d / "manifest.json").write_text(
        json.dumps(
            {
                "release": "2026-10-30",
                "views_sql": VIEWS_SQL,
                "features_sql": "SELECT 1",
                "datasets": ["cohort_membership", "round_trips", "fills", "universe"],
            }
        )
    )
    return d


def test_connect_creates_views_for_downloaded_files_only(release_dir):
    assert local.available_views(release_dir) == ["cohort_membership", "round_trips"]
    con = local.connect(release_dir)
    assert con.sql("SELECT sum(net) FROM round_trips").fetchone()[0] == 25.0
    with pytest.raises(duckdb.CatalogException):
        con.sql("SELECT * FROM fills")


def test_pit_cohort(release_dir):
    con = local.connect(release_dir)
    assert signals.pit_cohort(con, "2026-08-31") == ["0xa"]
    assert signals.pit_cohort(con, "2026-08-31", selector="all") == ["0xa", "0xb"]
    assert signals.pit_cohort(con, "2026-08-31", selector="top50_net") == ["0xa", "0xb"]
    assert [str(v) for v in signals.vintages(con)] == ["2026-08-31", "2026-09-30"]
    with pytest.raises(ValueError):
        signals.pit_cohort(con, "2026-08-31", selector="best")


def test_trips_to_weights_copies_late_and_caps_gross():
    index = pd.date_range("2026-10-01", periods=24, freq="1h", tz="UTC")
    trips = pd.DataFrame(
        {
            "coin": ["ETH", "BTC", "ETH"],
            "side": [1, -1, 1],
            "t0": pd.to_datetime(
                ["2026-10-01 02:00", "2026-10-01 02:00", "2026-10-01 03:00"], utc=True
            ),
            "t1": pd.to_datetime(
                ["2026-10-01 06:00", "2026-10-01 04:00", "2026-10-01 05:00"], utc=True
            ),
        }
    )
    w = signals.trips_to_weights(trips, index, delay=pd.Timedelta(hours=1))
    assert w.loc["2026-10-01 02:00", "ETH"] == 0  # not yet: copied an hour late
    at4 = w.loc["2026-10-01 04:00"]
    assert at4["ETH"] / abs(at4["BTC"]) == pytest.approx(2.0)
    assert w.abs().sum(axis=1).max() == pytest.approx(1.0)
    assert w.loc["2026-10-01 07:00"].abs().sum() == 0
    with pytest.raises(ValueError):
        signals.trips_to_weights(trips.drop(columns="side"), index)
    with pytest.raises(ValueError):
        signals.trips_to_weights(trips, index.tz_localize(None))


class FakeClient:
    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.downloads: list[str] = []

    async def datasets(self):
        by_ds: dict[str, list] = {}
        for path, data in self.files.items():
            by_ds.setdefault(path.split("/")[0], []).append(
                {
                    "path": path,
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        return {
            "release": "2026-10-30",
            "as_of": "2026-10-29",
            "rules_version": "r",
            "data": {
                "datasets": [
                    {"name": k, "rows": 1, "files": v} for k, v in sorted(by_ds.items())
                ],
                "views_sql": VIEWS_SQL,
                "features_sql": "SELECT 1",
                "schema_version": 1,
            },
        }

    async def download_dataset_file(self, name, file, dest, *, sha256=None):
        self.downloads.append(f"{name}/{file}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.files[f"{name}/{file}"])
        return dest


async def test_download_release_caches_and_guards_size(tmp_path):
    client = FakeClient(
        {"round_trips/part.parquet": b"a" * 100, "universe/part.parquet": b"b" * 10}
    )
    d = await bulk.download_release(["round_trips"], dest=tmp_path, client=client)
    assert d == tmp_path / "R=2026-10-30"
    assert client.downloads == ["round_trips/part.parquet"]
    meta = json.loads((d / "manifest.json").read_text())
    assert meta["views_sql"] == VIEWS_SQL and meta["release"] == "2026-10-30"
    await bulk.download_release(["round_trips"], dest=tmp_path, client=client)
    assert client.downloads == ["round_trips/part.parquet"]  # sha matched: no refetch
    with pytest.raises(ValueError, match="max_bytes"):
        await bulk.download_release(dest=tmp_path / "x", client=client, max_bytes=50)
    with pytest.raises(ValueError, match="Unknown datasets"):
        await bulk.download_release(["fills"], dest=tmp_path, client=client)


def test_cache_root_override(monkeypatch, tmp_path):
    monkeypatch.setenv("WAYFINDER_TRADER_LAB_CACHE", str(tmp_path))
    assert bulk.cache_root() == tmp_path


def test_features_apply_runs_release_sql_on_points(release_dir):
    from wayfinder_paths.core.trader_lab import features

    meta = json.loads((release_dir / "manifest.json").read_text())
    meta["features_sql"] = "SELECT coin, count(*) AS n FROM points GROUP BY coin"
    (release_dir / "manifest.json").write_text(json.dumps(meta))
    con = local.connect(release_dir)
    points = pd.DataFrame(
        {
            "coin": ["ETH", "ETH", "BTC"],
            "ts": pd.to_datetime(["2026-10-01"] * 3, utc=True),
        }
    )
    out = features.apply(con, points, release_dir).set_index("coin")["n"].to_dict()
    assert out == {"ETH": 1, "BTC": 1}  # points are de-duplicated


# ---------------------------------------------------------------- untrusted input


def _listing(path: str, release: str = "2026-10-30") -> dict:
    return {
        "release": release,
        "data": {
            "datasets": [
                {
                    "name": path.split("/")[0] or "x",
                    "files": [{"path": path, "bytes": 1}],
                }
            ]
        },
    }


@pytest.mark.parametrize(
    "path",
    [
        "round_trips/../../../etc/x.parquet",
        "../outside/part.parquet",
        "/abs/part.parquet",
        "round_trips/sub/part.parquet",
        "round_trips/part.sh",
    ],
)
def test_plan_rejects_paths_outside_the_cache(tmp_path, path):
    with pytest.raises(ValueError):
        bulk.plan(_listing(path), None, tmp_path)


@pytest.mark.parametrize("release", ["../x", "2026-10-30/../../x", "", None])
async def test_download_rejects_bad_release_ids(tmp_path, release):
    client = FakeClient({"round_trips/part.parquet": b"a"})
    listing = await client.datasets()
    listing["release"] = release

    async def datasets():
        return listing

    client.datasets = datasets
    with pytest.raises(ValueError):
        await bulk.download_release(dest=tmp_path, client=client)
    assert client.downloads == []


def test_manifest_sql_is_not_executed(release_dir):
    meta = json.loads((release_dir / "manifest.json").read_text())
    meta["views_sql"] = (
        "CREATE OR REPLACE VIEW x AS SELECT * FROM read_parquet('a'); "
        "COPY (SELECT 1) TO '/tmp/pwned.csv';"
    )
    meta["datasets"] = ["round_trips", "x'; DROP", "../etc"]
    (release_dir / "manifest.json").write_text(json.dumps(meta))
    assert local.available_views(release_dir) == ["round_trips"]
    local.connect(release_dir).sql("SELECT count(*) FROM round_trips").fetchone()


def test_paths_with_quotes_are_escaped(tmp_path, release_dir):
    odd = tmp_path / "it's here"
    release_dir.rename(odd)
    assert local.connect(odd).sql("SELECT count(*) FROM round_trips").fetchone()[0] == 1


def test_real_features_sql_passes_the_check():
    from wayfinder_paths.core.trader_lab._safety import check_select

    assert check_select(FEATURES_SQL)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('/etc/passwd')",
        "COPY (SELECT 1) TO '/tmp/x'",
        "SELECT 1; SELECT 2",
        "SELECT * FROM secrets",
        "SELECT getenv('HOME')",
        "INSTALL httpfs",
        "SELECT * FROM glob('/*')",
    ],
)
def test_untrusted_features_sql_is_refused(release_dir, sql):
    from wayfinder_paths.core.trader_lab import features

    meta = json.loads((release_dir / "manifest.json").read_text())
    meta["features_sql"] = sql
    (release_dir / "manifest.json").write_text(json.dumps(meta))
    with pytest.raises(ValueError):
        features.features_sql(release_dir)
