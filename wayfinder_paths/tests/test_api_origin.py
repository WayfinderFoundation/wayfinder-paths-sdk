from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import httpx
import pytest
from click.testing import CliRunner
from multidict import CIMultiDict

import wayfinder_paths.core.config as config
from wayfinder_paths.core.api_origin import (
    UntrustedApiOriginError,
    is_under_api_base,
    validate_api_base_url,
)
from wayfinder_paths.core.clients.ScheduledJobsClient import ScheduledJobsClient
from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.core.utils import web3 as web3_utils
from wayfinder_paths.paths.cli import path_cli
from wayfinder_paths.paths.client import PathsApiClient, PathsApiError
from wayfinder_paths.paths.path_safety import validate_path_slug, validate_path_version

API_KEY = "wk_origin_test"

ALLOWED_URLS = [
    "https://wayfinder.ai/api/v1",
    "https://wayfinder.ai/api/v1/",
    "https://strategies.wayfinder.ai",
    "https://strategies-dev.wayfinder.ai/api/v1",
]

REFUSED_URLS = [
    "https://evil.example",
    "https://evil.example/api/v1",
    "http://wayfinder.ai/api/v1",
    "http://strategies.wayfinder.ai",
    "https://wayfinder.ai:8443/api/v1",
    "https://wayfinder.ai.evil.example/api/v1",
    "https://evilwayfinder.ai",
    "https://evil.example/wayfinder.ai",
    "https://wayfinder.ai@evil.example",
    "https://user:pass@wayfinder.ai",
    "https://evil.example\\@wayfinder.ai",
    "https://evil.example#@wayfinder.ai",
    "https://evil.example?@wayfinder.ai",
    "https://wayfinder%2eai",
    "https://wayfinder.ai%2f@evil.example",
    "https://%77ayfinder.ai",
    "https://WAYFINDER.AI",
    "https://wayfinder.ai.",
    "https://wayfınder.ai",
    "https://xn--wayfnder-xbb.ai",
    "https://wayfinder.ai/api/../../evil",
    "https://wayfinder.ai/api/%2e%2e/evil",
    "https://wayfinder.ai/api/v1?next=https://evil.example",
    "https://wayfinder.ai/api/v1#frag",
    " https://evil.example",
    "//evil.example",
    "wayfinder.ai",
    "file:///etc/passwd",
    "",
]


@pytest.fixture(autouse=True)
def isolated_origin_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for name in (
        "OPENCODE_INSTANCE_ID",
        "WAYFINDER_API_BASE_URL",
        "WAYFINDER_PATHS_API_URL",
        "WAYFINDER_CONFIG",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"system": {"api_key": API_KEY}}))
    monkeypatch.setenv("WAYFINDER_CONFIG_PATH", str(cfg_path))
    original = copy.deepcopy(config.CONFIG)
    config.set_config({})
    yield
    config.set_config(original)


@pytest.fixture
def sent_requests(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Record every request any httpx.Client would send, answering with JSON."""
    sent: list[httpx.Request] = []

    def fake_send(self, request, **kwargs):
        sent.append(request)
        return httpx.Response(
            200,
            json={"path": {"slug": "demo"}, "versions": [], "paths": []},
            request=request,
        )

    monkeypatch.setattr(httpx.Client, "send", fake_send)
    return sent


def _on_shells(monkeypatch: pytest.MonkeyPatch, pinned: str | None = None) -> None:
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "wf-opencode-test")
    if pinned:
        monkeypatch.setenv("WAYFINDER_API_BASE_URL", pinned)


@pytest.mark.parametrize("url", ALLOWED_URLS)
def test_official_origins_are_allowed(url: str) -> None:
    assert validate_api_base_url(url) == url.rstrip("/")


@pytest.mark.parametrize("url", REFUSED_URLS)
def test_other_origins_and_url_tricks_are_refused(url: str) -> None:
    with pytest.raises(UntrustedApiOriginError):
        validate_api_base_url(url)


def test_loopback_is_allowed_only_off_shells(monkeypatch: pytest.MonkeyPatch) -> None:
    assert validate_api_base_url("http://localhost:8000") == "http://localhost:8000"
    assert validate_api_base_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"

    _on_shells(monkeypatch)
    with pytest.raises(UntrustedApiOriginError):
        validate_api_base_url("http://localhost:8000")


def test_shells_pin_narrows_allowlist_to_the_instance_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on_shells(monkeypatch, pinned="https://strategies.wayfinder.ai/api/v1")

    assert validate_api_base_url("https://strategies.wayfinder.ai")
    with pytest.raises(UntrustedApiOriginError):
        validate_api_base_url("https://strategies-dev.wayfinder.ai")


def test_shells_pin_must_itself_be_official(monkeypatch: pytest.MonkeyPatch) -> None:
    _on_shells(monkeypatch, pinned="https://evil.example/api/v1")

    with pytest.raises(UntrustedApiOriginError):
        config.get_api_base_url()


def test_api_base_url_from_config_must_be_allowlisted() -> None:
    assert config.get_api_base_url() == "https://wayfinder.ai/api/v1"

    config.set_config({"system": {"api_base_url": "https://evil.example/api/v1"}})
    with pytest.raises(UntrustedApiOriginError):
        config.get_api_base_url()


def test_shells_api_base_url_comes_from_the_machine_not_the_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on_shells(monkeypatch, pinned="https://strategies.wayfinder.ai/api/v1")
    config.set_config(
        {"system": {"api_base_url": "https://strategies-dev.wayfinder.ai/api/v1"}}
    )

    assert config.get_api_base_url() == "https://strategies.wayfinder.ai/api/v1"
    assert config.get_paths_api_base_url() == "https://strategies.wayfinder.ai"


@pytest.mark.parametrize(
    "setup",
    [
        lambda mp: config.set_config(
            {"system": {"paths_api_base_url": "https://evil.example"}}
        ),
        lambda mp: mp.setenv("WAYFINDER_PATHS_API_URL", "https://evil.example"),
    ],
)
def test_paths_api_base_url_overrides_must_be_allowlisted(
    monkeypatch: pytest.MonkeyPatch, setup
) -> None:
    setup(monkeypatch)
    with pytest.raises(UntrustedApiOriginError):
        config.get_paths_api_base_url()


def _write_volume_config(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "volume-config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "system": {
                    "api_key": API_KEY,
                    "paths_api_base_url": "https://strategies-dev.wayfinder.ai",
                },
                "strategy": {"rpc_urls": {"8453": ["https://rpc.evil.example"]}},
            }
        )
    )
    return cfg_path


def test_shells_ignores_endpoint_overrides_from_config_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _on_shells(monkeypatch)
    config.load_config(_write_volume_config(tmp_path))

    assert "paths_api_base_url" not in config.CONFIG["system"]
    assert config.get_rpc_urls() == {}
    assert config.get_api_key() == API_KEY

    # In-process overrides (Gorlami forks) still apply.
    config.set_rpc_urls({"8453": ["https://fork.example"]})
    assert config.get_rpc_urls() == {"8453": ["https://fork.example"]}


def test_off_shells_keeps_endpoint_overrides_from_config_file(tmp_path: Path) -> None:
    config.load_config(_write_volume_config(tmp_path))

    assert config.get_paths_api_base_url() == "https://strategies-dev.wayfinder.ai"
    assert config.get_rpc_urls() == {"8453": ["https://rpc.evil.example"]}


@pytest.mark.parametrize(
    ("rpc", "expected"),
    [
        ("https://wayfinder.ai/api/v1/blockchain/rpc/8453/", True),
        ("https://wayfinder.ai/api/v1/blockchain/rpc/8453/0/", True),
        ("https://wayfinder.ai/api/v1.evil.example/rpc", False),
        ("https://wayfinder.ai/api/v1evil/rpc", False),
        ("https://wayfinder.ai.evil.example/api/v1/blockchain/rpc/8453/", False),
        ("https://wayfinder.ai/api/v1@evil.example/rpc", False),
        ("http://wayfinder.ai/api/v1/blockchain/rpc/8453/", False),
        ("https://wayfinder.ai/api/v1/../../evil", False),
        ("https://rpc.evil.example", False),
    ],
)
def test_api_key_is_sent_only_to_rpcs_under_the_api_base(
    rpc: str, expected: bool
) -> None:
    assert web3_utils._is_wayfinder_rpc(rpc) is expected


def test_gorlami_fork_detection_requires_the_api_origin() -> None:
    base = "https://wayfinder.ai/api/v1"
    assert web3_utils._is_gorlami_fork_rpc(f"{base}/blockchain/gorlami/fork/abc-123")
    assert not web3_utils._is_gorlami_fork_rpc(
        "https://wayfinder.ai.evil.example/api/v1/blockchain/gorlami/fork/abc"
    )
    assert not web3_utils._is_gorlami_fork_rpc(f"{base}/blockchain/gorlami/forkx/abc")


def test_is_under_api_base_compares_whole_path_segments() -> None:
    assert is_under_api_base(
        "https://wayfinder.ai/api/v1", "https://wayfinder.ai/api/v1/"
    )
    assert not is_under_api_base(
        "https://wayfinder.ai/api/v10", "https://wayfinder.ai/api/v1"
    )


def test_paths_client_sends_key_to_allowlisted_host(
    sent_requests: list[httpx.Request],
) -> None:
    client = PathsApiClient(api_base_url="https://strategies.wayfinder.ai")
    client.get_path(slug="demo")

    (request,) = sent_requests
    assert request.url.host == "strategies.wayfinder.ai"
    assert request.url.path == "/api/v1/paths/demo/"
    assert request.headers["X-API-Key"] == API_KEY


@pytest.mark.parametrize("url", REFUSED_URLS[:12])
def test_paths_client_refuses_other_hosts_before_any_request(
    url: str, sent_requests: list[httpx.Request]
) -> None:
    with pytest.raises(PathsApiError):
        PathsApiClient(api_base_url=url)
    assert sent_requests == []


def test_paths_client_does_not_follow_redirects() -> None:
    assert PathsApiClient()._client.follow_redirects is False


def test_redirect_to_another_host_keeps_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example/x"})

    monkeypatch.setattr(
        httpx.Client,
        "__init__",
        _with_transport(httpx.Client.__init__, httpx.MockTransport(handler)),
    )
    client = PathsApiClient(api_base_url="https://strategies.wayfinder.ai")
    with pytest.raises(ValueError):  # the empty 302 body is not JSON
        client.get_path(slug="demo")
    assert [r.url.host for r in sent] == ["strategies.wayfinder.ai"]


def _redirect_to_evil(sent: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.host == "evil.example":
            return httpx.Response(200, json={})
        return httpx.Response(307, headers={"Location": "https://evil.example/steal"})

    return httpx.MockTransport(handler)


async def test_wayfinder_client_drops_key_on_cross_origin_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[httpx.Request] = []
    monkeypatch.setattr(
        httpx.AsyncClient,
        "__init__",
        _with_transport(httpx.AsyncClient.__init__, _redirect_to_evil(sent)),
    )
    client = WayfinderClient()

    await client._authed_request("GET", "https://wayfinder.ai/api/v1/features/")
    await client._authed_request("GET", "https://evil.example/direct")
    await client.aclose()

    assert [(r.url.host, r.headers.get("X-API-KEY")) for r in sent] == [
        ("wayfinder.ai", API_KEY),
        ("evil.example", None),
        ("evil.example", None),
    ]


def test_scheduled_jobs_client_drops_key_on_cross_origin_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on_shells(monkeypatch, pinned="https://wayfinder.ai/api/v1")
    sent: list[httpx.Request] = []
    monkeypatch.setattr(
        httpx.Client,
        "__init__",
        _with_transport(httpx.Client.__init__, _redirect_to_evil(sent)),
    )

    ScheduledJobsClient().bulk_sync([])

    assert [(r.url.host, r.headers.get("X-API-KEY")) for r in sent] == [
        ("wayfinder.ai", API_KEY),
        ("evil.example", None),
    ]


def test_paths_client_does_not_follow_redirects_on_a_supplied_client() -> None:
    sent: list[httpx.Request] = []
    with httpx.Client(
        follow_redirects=True, transport=_redirect_to_evil(sent)
    ) as http_client:
        client = PathsApiClient(
            api_base_url="https://strategies.wayfinder.ai", client=http_client
        )
        with pytest.raises(ValueError):  # the empty 307 body is not JSON
            client.get_path(slug="demo")

    assert [r.url.host for r in sent] == ["strategies.wayfinder.ai"]


@pytest.mark.parametrize(
    "rpc",
    [
        "https://wayfinder.ai/api/v1/blockchain/rpc/8453/",
        "https://wayfinder.ai/api/v1/blockchain/gorlami/fork/abc-123",
    ],
)
async def test_wayfinder_rpc_does_not_follow_redirects_with_the_key(
    rpc: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str | None]] = []

    # Stands in for the network: answers every aiohttp request in-process.
    async def fake_network(request, handler):
        seen.append((str(request.url), request.headers.get("X-API-KEY")))
        first = len(seen) == 1

        async def read() -> bytes:
            return b'{"jsonrpc":"2.0","id":0,"result":"0x1"}'

        return SimpleNamespace(
            status=307 if first else 200,
            headers=CIMultiDict(
                {"Location": "https://evil.example/steal"} if first else {}
            ),
            method=request.method,
            url=request.url,
            _raw_cookie_headers=(),
            connection=None,
            release=lambda: None,
            close=lambda: None,
            raise_for_status=lambda: None,
            read=read,
        )

    original_init = aiohttp.ClientSession.__init__

    def init(self, *args, **kwargs):
        kwargs["middlewares"] = [fake_network]
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(aiohttp.ClientSession, "__init__", init)
    w3 = web3_utils._get_web3(rpc, 8453)
    try:
        await w3.provider.make_request("eth_chainId", [])
    finally:
        await w3.provider.disconnect()

    assert seen == [(rpc, API_KEY)]


def _with_transport(init, transport):
    def patched(self, *args, **kwargs):
        kwargs["transport"] = transport
        init(self, *args, **kwargs)

    return patched


VALID_SLUGS = ["demo", "a", "my-path-2", "x" * 255]
INVALID_SLUGS = [
    "",
    "..",
    ".",
    "../demo",
    "demo/../x",
    "demo/x",
    "demo\\x",
    "%2e%2e",
    "demo%2fx",
    "Demo",
    "-demo",
    "demo-",
    "de--mo",
    "demo?x=1",
    "demo#x",
    "demo ",
    " demo",
    "demo\n",
    "dеmo",
    "x" * 256,
]
VALID_VERSIONS = ["1.0.0", "0.1.0-rc.1", "1.2.3+build.5", "2024.1", "1.0", "1!2.0"]
INVALID_VERSIONS = [
    "",
    "..",
    "../1.0.0",
    "1.0.0/../..",
    "1.0.0/x",
    "1.0.0\\x",
    "%2e%2e",
    "1.0.0%2f..",
    "1.0.0?x",
    "1.0.0#x",
    " 1.0.0",
    "1.0.0 ",
    "1.0.0\n",
    "1..0",
    "v1.0.0/",
    "latest",
    "1." + "0" * 40,
]


@pytest.mark.parametrize("slug", VALID_SLUGS)
def test_valid_slugs_pass(slug: str) -> None:
    assert validate_path_slug(slug) == slug


@pytest.mark.parametrize("slug", INVALID_SLUGS)
def test_slug_traversal_and_tricks_are_refused(slug: str) -> None:
    with pytest.raises(ValueError):
        validate_path_slug(slug)


@pytest.mark.parametrize("version", VALID_VERSIONS)
def test_valid_versions_pass(version: str) -> None:
    assert validate_path_version(version) == version


@pytest.mark.parametrize("version", INVALID_VERSIONS)
def test_version_traversal_and_tricks_are_refused(version: str) -> None:
    with pytest.raises(ValueError):
        validate_path_version(version)


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.get_path(slug="../admin"),
        lambda c: c.get_path_version(slug="demo", version="../../x"),
        lambda c: c.download_bundle(
            slug="demo", version="1.0.0/../..", out_path=Path("unused")
        ),
        lambda c: c.create_install_intent(slug="demo/../x", version="1.0.0"),
        lambda c: c.submit_install_receipt(
            slug="%2e%2e", intent={}, signature="", runtime="sdk-cli"
        ),
        lambda c: c.emit_event(slug="demo?x", event_type="signal"),
        lambda c: c.fork_path(slug="demo", new_slug="../x"),
        lambda c: c.submit_install_heartbeat(
            installation_id="../x", heartbeat_token="t"
        ),
        lambda c: c.submit_shells_inventory_sync(
            app_name="a/../b", lockfile_present=True, paths=[]
        ),
    ],
)
def test_paths_client_refuses_bad_url_segments_before_any_request(
    call, sent_requests: list[httpx.Request]
) -> None:
    client = PathsApiClient(api_base_url="https://strategies.wayfinder.ai")
    with pytest.raises(PathsApiError):
        call(client)
    assert sent_requests == []


@pytest.mark.parametrize(
    "api_url",
    [
        "https://evil.example",
        "http://strategies.wayfinder.ai",
        "https://wayfinder.ai@evil.example",
        "https://wayfinder%2eai",
    ],
)
def test_cli_install_refuses_other_api_url_before_any_request(
    api_url: str, sent_requests: list[httpx.Request], tmp_path: Path
) -> None:
    result = CliRunner().invoke(
        path_cli,
        [
            "install",
            "--slug",
            "demo",
            "--dir",
            str(tmp_path / "paths"),
            "--api-url",
            api_url,
        ],
    )

    assert result.exit_code != 0
    assert "not an allowed Wayfinder API origin" in result.output
    assert sent_requests == []
    assert not (tmp_path / "paths").exists()


@pytest.mark.parametrize(
    ("slug", "version"),
    [("../demo", None), ("demo/x", None), ("demo", "../../etc"), ("demo", "1.0/..")],
)
def test_cli_install_refuses_bad_names_before_any_request(
    slug: str,
    version: str | None,
    sent_requests: list[httpx.Request],
    tmp_path: Path,
) -> None:
    args = ["install", "--slug", slug, "--dir", str(tmp_path / "paths")]
    if version:
        args += ["--version", version]
    result = CliRunner().invoke(path_cli, args)

    assert result.exit_code != 0
    assert "Invalid path" in result.output
    assert sent_requests == []
    assert not (tmp_path / "paths").exists()


def test_cli_install_refuses_bad_version_from_the_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sent: list[httpx.Request] = []

    def fake_send(self, request, **kwargs):
        sent.append(request)
        return httpx.Response(
            200,
            json={
                "path": {"slug": "demo", "latest_version": "../../escape"},
                "versions": [{"version": "../../escape", "bundle_sha256": "0" * 64}],
            },
            request=request,
        )

    monkeypatch.setattr(httpx.Client, "send", fake_send)
    result = CliRunner().invoke(
        path_cli, ["install", "--slug", "demo", "--dir", str(tmp_path / "paths")]
    )

    assert result.exit_code != 0
    assert "Invalid path version" in result.output
    assert [r.url.path for r in sent] == ["/api/v1/paths/demo/"]
    assert not (tmp_path / "escape").exists()


def test_cli_info_works_against_allowlisted_api_url(
    sent_requests: list[httpx.Request],
) -> None:
    result = CliRunner().invoke(
        path_cli,
        ["info", "--slug", "demo", "--api-url", "https://strategies-dev.wayfinder.ai"],
    )

    assert result.exit_code == 0, result.output
    (request,) = sent_requests
    assert str(request.url) == "https://strategies-dev.wayfinder.ai/api/v1/paths/demo/"
    assert request.headers["X-API-Key"] == API_KEY
