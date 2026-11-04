"""TraderLabClient: URLs, params, error envelopes, polling, verified downloads."""

from __future__ import annotations

import hashlib
import importlib
import json

import httpx
import pytest

from wayfinder_paths.core.clients.trader_lab_types import TraderLabAPIError
from wayfinder_paths.core.clients.TraderLabClient import TraderLabClient

module = importlib.import_module("wayfinder_paths.core.clients.TraderLabClient")
BASE = "https://x/api/v1"


def envelope(data, **extra):
    return {
        "release": "2026-10-30",
        "as_of": "2026-10-29",
        "rules_version": "r",
        "data": data,
        **extra,
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(module, "get_api_base_url", lambda: BASE)
    monkeypatch.setattr(module, "POLL_INTERVAL_SECONDS", 0)
    c = TraderLabClient()
    c.headers["X-API-KEY"] = "wk_test"
    seen: list[httpx.Request] = []
    routes: dict[tuple[str, str], object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        route = routes.get((request.method, request.url.path))
        if callable(route):
            return route(request)
        if route is None:
            return httpx.Response(404, json={"detail": "Not found."})
        return route

    c.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    c.routes, c.seen = routes, seen
    return c


async def test_leaderboard_params_and_envelope(client):
    client.routes[("GET", "/api/v1/trader-lab/leaderboard/")] = httpx.Response(
        200, json=envelope([{"address": "0xa"}], page={"next": None, "limit": 10})
    )
    out = await client.leaderboard(
        bucket="dip_buyer", sort="-net_mtm", include_hft=True, limit=10
    )
    assert out["data"] == [{"address": "0xa"}]
    params = dict(client.seen[-1].url.params)
    assert params == {
        "bucket": "dip_buyer",
        "sort": "-net_mtm",
        "include_hft": "true",
        "limit": "10",
    }
    assert client.seen[-1].headers["X-API-KEY"] == "wk_test"


async def test_leaderboard_validates_locally(client):
    with pytest.raises(ValueError):
        await client.leaderboard(bucket="whales")
    with pytest.raises(ValueError):
        await client.leaderboard(sort="address")
    assert client.seen == []


async def test_iter_leaderboard_follows_cursors(client):
    pages = iter(
        [
            envelope(
                [{"address": "0x1"}, {"address": "0x2"}],
                page={"next": "c2", "limit": 2},
            ),
            envelope([{"address": "0x3"}], page={"next": None, "limit": 2}),
        ]
    )
    client.routes[("GET", "/api/v1/trader-lab/leaderboard/")] = (
        lambda r: httpx.Response(200, json=next(pages))
    )
    rows = await client.iter_leaderboard(max_rows=10)
    assert [r["address"] for r in rows] == ["0x1", "0x2", "0x3"]
    assert client.seen[1].url.params["cursor"] == "c2"


async def test_wallet_lowercases_address(client):
    client.routes[("GET", "/api/v1/trader-lab/wallets/0xabc/")] = httpx.Response(
        200, json=envelope({})
    )
    await client.wallet("0xABC")
    assert client.seen[-1].url.path.endswith("/wallets/0xabc/")


@pytest.mark.parametrize(
    "status,body,code",
    [
        (
            400,
            {
                "error": {
                    "code": "rejected",
                    "message": "Function 'read_csv' is not allowed",
                }
            },
            "rejected",
        ),
        (429, {"detail": "Not enough API credits for this request."}, "429"),
        (400, {"sql": ["a string of at most 20,000 bytes"]}, "invalid_request"),
    ],
)
async def test_error_envelopes(client, status, body, code):
    client.routes[("POST", "/api/v1/trader-lab/query/")] = httpx.Response(
        status, json=body
    )
    with pytest.raises(TraderLabAPIError) as exc:
        await client.query(sql="SELECT 1")
    assert exc.value.code == code and exc.value.status == status


async def test_query_body_and_validation(client):
    client.routes[("POST", "/api/v1/trader-lab/query/")] = httpx.Response(
        200, json=envelope({"rows": []})
    )
    await client.query(recipe="bars", params={"coin": "ETH"})
    assert json.loads(client.seen[-1].content) == {
        "mode": "sync",
        "recipe": "bars",
        "params": {"coin": "ETH"},
    }
    with pytest.raises(ValueError):
        await client.query()
    with pytest.raises(ValueError):
        await client.query(sql="SELECT 1", recipe="bars")
    with pytest.raises(ValueError):
        await client.query(sql="SELECT 1", mode="stream")


async def test_wait_for_query_polls_until_done(client):
    states = iter(["queued", "running", "done"])
    client.routes[("GET", "/api/v1/trader-lab/query/7/")] = lambda r: httpx.Response(
        200, json={"release": "x", "data": {"id": 7, "status": next(states)}}
    )
    out = await client.wait_for_query(7)
    assert out["data"]["status"] == "done" and len(client.seen) == 3


async def test_dataset_download_verifies_sha256(client, tmp_path):
    payload = b"PAR1" * 1000
    client.routes[
        ("GET", "/api/v1/trader-lab/datasets/leaderboard/files/part.parquet")
    ] = httpx.Response(200, content=payload)
    good = hashlib.sha256(payload).hexdigest()
    dest = await client.download_dataset_file(
        "leaderboard", "part.parquet", tmp_path / "a.parquet", sha256=good
    )
    assert dest.read_bytes() == payload
    with pytest.raises(TraderLabAPIError) as exc:
        await client.download_dataset_file(
            "leaderboard", "part.parquet", tmp_path / "b.parquet", sha256="0" * 64
        )
    assert exc.value.code == "checksum_mismatch"
    assert (
        not (tmp_path / "b.parquet").exists()
        and not (tmp_path / "b.parquet.part").exists()
    )


async def test_download_error_is_typed(client, tmp_path):
    client.routes[("GET", "/api/v1/trader-lab/datasets/x/files/part.parquet")] = (
        httpx.Response(403, json={"detail": "This endpoint needs an API key"})
    )
    with pytest.raises(TraderLabAPIError) as exc:
        await client.download_dataset_file("x", "part.parquet", tmp_path / "x.parquet")
    assert exc.value.status == 403
