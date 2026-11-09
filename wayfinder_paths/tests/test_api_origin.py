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
    "https://wayfinder.ai.",
    "https://WAYFINDER.AI:8443",
    "HTTP://WAYFINDER.AI",
    "https://wayfinder.ai:0443",
    "http://wayfinder.ai:443",
    "https://[::2]",
    "https://[0:0:0:0:0:0:0:1]",
    "http://host.docker.internal.evil.example",
    "http://docker.internal",
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


LOCAL_DEV_URLS = [
    ("http://localhost:8000", "http://localhost:8000"),
    ("http://127.0.0.1:8000/", "http://127.0.0.1:8000"),
    ("http://[::1]:8000/api/v1", "http://[::1]:8000/api/v1"),
    ("https://[::1]", "https://[::1]"),
    ("http://host.docker.internal:8000", "http://host.docker.internal:8000"),
    ("https://host.docker.internal/api/v1", "https://host.docker.internal/api/v1"),
    ("http://Host.Docker.Internal:80/api", "http://host.docker.internal/api"),
    ("HTTP://LOCALHOST:8000", "http://localhost:8000"),
]


@pytest.mark.parametrize(("url", "expected"), LOCAL_DEV_URLS)
def test_local_dev_hosts_are_allowed_only_off_shells(
    url: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert validate_api_base_url(url) == expected

    _on_shells(monkeypatch)
    with pytest.raises(UntrustedApiOriginError):
        validate_api_base_url(url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://WAYFINDER.AI", "https://wayfinder.ai"),
        (
            "HTTPS://Strategies.Wayfinder.AI/api/v1/",
            "https://strategies.wayfinder.ai/api/v1",
        ),
        ("https://wayfinder.ai:443/api/v1", "https://wayfinder.ai/api/v1"),
        ("https://WAYFINDER.ai:443/", "https://wayfinder.ai"),
        ("  https://wayfinder.ai/api/v1//  ", "https://wayfinder.ai/api/v1"),
        # Only scheme and host are folded; the path keeps its case.
        ("https://WAYFINDER.AI/API/V1", "https://wayfinder.ai/API/V1"),
    ],
)
def test_host_case_and_default_port_are_normalised(url: str, expected: str) -> None:
    assert validate_api_base_url(url) == expected


def test_non_ascii_case_folding_does_not_reach_an_allowed_host() -> None:
    # U+212A KELVIN SIGN lower-cases to ASCII "k".
    with pytest.raises(UntrustedApiOriginError):
        validate_api_base_url("http://host.doc\u212aer.internal:8000")


def test_is_under_api_base_normalises_both_sides() -> None:
    assert is_under_api_base(
        "https://WAYFINDER.AI:443/api/v1/blockchain/rpc/8453",
        "https://wayfinder.ai/api/v1",
    )
    assert is_under_api_base("http://[::1]:8000/api/v1/x", "http://[::1]:8000/api/v1")
    assert not is_under_api_base("http://[::1]:8001/api/v1/x", "http://[::1]:8000")


def test_pinned_shells_url_is_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    _on_shells(monkeypatch, pinned="HTTPS://Strategies.Wayfinder.AI:443/api/v1/")
    assert config.get_api_base_url() == "https://strategies.wayfinder.ai/api/v1"
    assert validate_api_base_url("https://STRATEGIES.wayfinder.ai")


def test_error_names_value_source_allowed_hosts_and_fix(tmp_path: Path) -> None:
    cfg_path = tmp_path / "bad-config.json"
    cfg_path.write_text(
        json.dumps({"system": {"api_base_url": "https://evil.example/api/v1"}})
    )
    config.load_config(cfg_path)

    with pytest.raises(UntrustedApiOriginError) as info:
        config.get_api_base_url()

    message = str(info.value)
    assert isinstance(info.value, ValueError)
    assert info.value.value == "https://evil.example/api/v1"
    assert "'https://evil.example/api/v1'" in message
    assert f"config.json key system.api_base_url ({cfg_path})" in message
    for host in (
        "https://wayfinder.ai",
        "https://strategies.wayfinder.ai",
        "https://strategies-dev.wayfinder.ai",
        "localhost",
        "127.0.0.1",
        "[::1]",
        "host.docker.internal",
    ):
        assert host in message
    assert f"Fix: correct system.api_base_url in {cfg_path}" in message


def test_error_for_env_var_and_api_url_flag_name_their_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WAYFINDER_PATHS_API_URL", "https://evil.example")
    with pytest.raises(UntrustedApiOriginError) as info:
        config.get_paths_api_base_url()
    assert "Environment variable WAYFINDER_PATHS_API_URL" in str(info.value)
    assert "unset it" in str(info.value)

    with pytest.raises(PathsApiError) as api_info:
        PathsApiClient(api_base_url="https://evil.example")
    assert "--api-url" in str(api_info.value)
    assert "host.docker.internal" in str(api_info.value)


def test_error_on_shells_says_local_hosts_are_not_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on_shells(monkeypatch, pinned="https://strategies.wayfinder.ai/api/v1")
    with pytest.raises(UntrustedApiOriginError) as info:
        validate_api_base_url("http://localhost:8000", label="--api-url")
    message = str(info.value)
    assert "https://strategies.wayfinder.ai/api/v1" in message
    assert "local hosts are not accepted inside a Wayfinder Shells instance" in message


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


def _capture_warnings() -> tuple[list[str], int]:
    from loguru import logger

    messages: list[str] = []
    handler_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    return messages, handler_id


def test_shells_logs_one_line_naming_dropped_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from loguru import logger

    _on_shells(monkeypatch)
    cfg_path = _write_volume_config(tmp_path)
    messages, handler_id = _capture_warnings()
    try:
        config.load_config(cfg_path)
    finally:
        logger.remove(handler_id)

    (line,) = messages
    assert line.count("\n") == 1
    assert "system.paths_api_base_url, strategy.rpc_urls" in line
    assert str(cfg_path) in line
    assert "not trusted inside a Wayfinder Shells instance" in line


def test_no_warning_when_nothing_is_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from loguru import logger

    cfg_path = tmp_path / "plain.json"
    cfg_path.write_text(json.dumps({"system": {"api_key": API_KEY}}))
    messages, handler_id = _capture_warnings()
    try:
        config.load_config(cfg_path)  # off Shells
        _on_shells(monkeypatch)
        config.load_config(cfg_path)  # on Shells, nothing to drop
    finally:
        logger.remove(handler_id)
    assert messages == []


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
    with pytest.raises(PathsApiError, match="Unexpected redirect from API host"):
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
        with pytest.raises(PathsApiError, match=r"redirect from API host \(307"):
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


VALID_SLUGS = [
    "demo",
    "a",
    "my-path-2",
    "x" * 255,
    "my_path",
    "my__path",
    "my_-_path",
    "a_b-c_d",
    "path_v2",
]
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
    "_demo",
    "demo_",
    "de_--_mo",
    "de.mo",
    "demo_..",
    "x" * 254 + "_y",
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
    with pytest.raises(ValueError, match="lower-case letters, digits, '_' and '-'"):
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
            slug="demo",
            version="1.0.0/../..",
            out_path=Path("unused"),
            intent={},
            signature="",
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


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 300, 304])
def test_redirected_bundle_download_errors_without_writing_a_file(
    status: int, tmp_path: Path
) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(status, headers={"Location": "https://cdn.example/b"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = PathsApiClient(
            api_base_url="https://strategies.wayfinder.ai", client=http_client
        )
        out = tmp_path / "bundle.zip"
        with pytest.raises(PathsApiError, match="Unexpected redirect from API host"):
            client.download_bundle(
                slug="demo",
                version="1.0.0",
                out_path=out,
                intent={"slug": "demo"},
                signature="sig",
            )

    assert not out.exists()
    assert [r.url.host for r in sent] == ["strategies.wayfinder.ai"]


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.create_install_intent(slug="demo", version="1.0.0"),
        lambda c: c.emit_event(slug="demo", event_type="signal"),
        lambda c: c.list_paths(),
        lambda c: c.submit_batch_install_heartbeats(heartbeats=[], source="t"),
        lambda c: c.submit_shells_inventory_sync(
            app_name="app", lockfile_present=True, paths=[]
        ),
    ],
)
def test_keyed_calls_turn_a_redirect_into_an_explicit_error(call) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example/x"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = PathsApiClient(
            api_base_url="https://strategies.wayfinder.ai", client=http_client
        )
        with pytest.raises(PathsApiError, match=r"redirect from API host \(302"):
            call(client)


def test_cli_main_prints_a_clear_error_for_a_refused_api_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import wayfinder_paths.mcp.cli as mcp_cli

    def boom() -> None:
        config.set_config({"system": {"api_base_url": "https://evil.example"}})
        config.get_api_base_url()

    monkeypatch.setattr(mcp_cli, "_run", boom)
    with pytest.raises(SystemExit) as exit_info:
        mcp_cli.main()

    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("Error: config.json key system.api_base_url")
    assert "'https://evil.example'" in err
    assert "host.docker.internal" in err
    assert "Traceback" not in err


async def test_mcp_tool_wrapper_returns_invalid_api_url_error() -> None:
    from wayfinder_paths.mcp.utils import catch_errors

    @catch_errors
    async def tool() -> dict:
        config.get_api_base_url()
        return {"ok": True}

    @catch_errors
    def sync_tool() -> dict:
        config.get_api_base_url()
        return {"ok": True}

    config.set_config({"system": {"api_base_url": "https://evil.example"}})
    for result in (await tool(), sync_tool()):
        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_api_url"
        assert "'https://evil.example'" in result["error"]["message"]
        assert "Fix:" in result["error"]["message"]


async def test_metrics_report_skips_a_refused_api_url(
    sent_requests: list[httpx.Request],
) -> None:
    from wayfinder_paths.core.clients.MetricsClient import MetricsClient

    config.set_config({"system": {"api_base_url": "https://evil.example"}})
    await MetricsClient().report_tool(
        tool="t", success=True, code="ok", duration_ms=1.0
    )
    assert sent_requests == []


def test_mcp_server_reports_a_refused_api_url_once_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wayfinder_paths.mcp.server import report_refused_api_url

    assert report_refused_api_url() is None
    monkeypatch.setenv("WAYFINDER_PATHS_API_URL", "https://evil.example")
    message = report_refused_api_url()
    assert message is not None
    assert "Environment variable WAYFINDER_PATHS_API_URL" in message
