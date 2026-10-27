from pathlib import Path

import httpx
import pytest

from wayfinder_paths.paths.client import PathsApiClient, PathsApiError

INTENT = {"intent_id": "i-1", "path_slug": "demo", "version": "1.0.0"}
SIGNED_URL = (
    "https://storage.googleapis.com/private/file?generation=123&X-Goog-Signature=x"
)


def _client(monkeypatch, api, storage=None) -> PathsApiClient:
    client_type = httpx.Client
    monkeypatch.setattr(
        "wayfinder_paths.paths.client.get_api_key", lambda: "wk_test_only"
    )
    if storage is not None:
        monkeypatch.setattr(
            "wayfinder_paths.paths.client.httpx.Client",
            lambda **kw: client_type(transport=httpx.MockTransport(storage), **kw),
        )
    return PathsApiClient(
        api_base_url="https://api.test",
        client=client_type(
            transport=httpx.MockTransport(api), cookies={"session": "not-for-storage"}
        ),
    )


def _download(client: PathsApiClient, target: Path) -> Path:
    return client.download_bundle(
        slug="demo", version="1.0.0", out_path=target, intent=INTENT, signature="sig"
    )


def test_download_posts_the_intent_and_fetches_storage_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage_calls: list[httpx.Request] = []

    def storage(request: httpx.Request) -> httpx.Response:
        storage_calls.append(request)
        for header in ("authorization", "x-api-key", "cookie", "proxy-authorization"):
            assert header not in request.headers
        return httpx.Response(200, content=b"harmless-test-bundle")

    def api(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/paths/demo/versions/1.0.0/download/"
        assert request.headers["x-api-key"] == "wk_test_only"
        assert b'"signature":"sig"' in request.content.replace(b" ", b"")
        # The intent travels in the body, never in the URL.
        assert not request.url.query
        return httpx.Response(200, json={"downloadUrl": SIGNED_URL})

    target = tmp_path / "bundle.zip"
    _download(_client(monkeypatch, api, storage), target)
    assert target.read_bytes() == b"harmless-test-bundle"
    assert len(storage_calls) == 1
    assert not (tmp_path / "bundle.zip.part").exists()


@pytest.mark.parametrize(
    "url",
    [
        "http://storage.googleapis.com/file",
        "https://evil.test/file",
        "https://storage.googleapis.com.evil.test/file",
        "https://token@storage.googleapis.com/file",
        "https://storage.googleapis.com:444/file",
        "",
        None,
    ],
)
def test_storage_destination_is_constrained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url
) -> None:
    client = _client(
        monkeypatch, lambda request: httpx.Response(200, json={"downloadUrl": url})
    )
    with pytest.raises(PathsApiError, match="destination"):
        _download(client, tmp_path / "bundle.zip")
    assert not (tmp_path / "bundle.zip").exists()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": SIGNED_URL}),
        httpx.Response(
            200, content=b"PK-old-bundle", headers={"content-type": "application/zip"}
        ),
        httpx.Response(403, json={"code": "paths_installs_disabled"}),
        httpx.Response(404, json={"detail": "Not found."}),
    ],
)
def test_backend_must_answer_with_a_signed_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> None:
    """Redirects are not followed and bundle bytes from the API are refused:
    bytes only ever come from the private bucket."""
    client = _client(monkeypatch, lambda request: response)
    with pytest.raises(PathsApiError):
        _download(client, tmp_path / "bundle.zip")
    assert not (tmp_path / "bundle.zip").exists()


@pytest.mark.parametrize("status", [302, 403, 404, 500])
def test_storage_failure_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    client = _client(
        monkeypatch,
        lambda request: httpx.Response(200, json={"downloadUrl": SIGNED_URL}),
        lambda request: httpx.Response(
            status, headers={"location": "https://evil.test/"}
        ),
    )
    with pytest.raises(PathsApiError, match="Artifact download failed"):
        _download(client, tmp_path / "bundle.zip")
    assert list(tmp_path.iterdir()) == []


def test_oversized_download_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PathsApiClient, "MAX_BUNDLE_BYTES", 10)
    client = _client(
        monkeypatch,
        lambda request: httpx.Response(200, json={"downloadUrl": SIGNED_URL}),
        lambda request: httpx.Response(200, content=b"x" * 11),
    )
    with pytest.raises(PathsApiError, match="size limit"):
        _download(client, tmp_path / "bundle.zip")
    assert list(tmp_path.iterdir()) == []
