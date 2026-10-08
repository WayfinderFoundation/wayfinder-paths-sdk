from pathlib import Path

import httpx
import pytest

from wayfinder_paths.paths.client import PathsApiClient, PathsApiError


@pytest.mark.parametrize(
    "state", [None, "review", "processing", "approved", "hidden", "rejected"]
)
def test_version_lookup_requires_explicit_public_approval(state: str | None) -> None:
    api = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"version": {"version": "1.0.0", "status": state}}
            )
        )
    )
    client = PathsApiClient(api_base_url="https://api.test", client=api)
    with pytest.raises(PathsApiError, match="approved"):
        client.get_path_version(slug="dependency", version="1.0.0")


def test_version_lookup_cannot_substitute_an_approved_version() -> None:
    api = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"version": {"version": "2.0.0", "status": "public"}}
            )
        )
    )
    client = PathsApiClient(api_base_url="https://api.test", client=api)
    with pytest.raises(PathsApiError, match="approved"):
        client.get_path_version(slug="dependency", version="1.0.0")


@pytest.mark.parametrize("status", [401, 403, 404, 429, 503])
def test_version_lookup_fails_closed_on_registry_error(status: int) -> None:
    api = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json={"error": "unavailable"})
        )
    )
    client = PathsApiClient(api_base_url="https://api.test", client=api)
    with pytest.raises(PathsApiError):
        client.get_path_version(slug="dependency", version="1.0.0")


def test_version_lookup_fails_closed_on_timeout() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("registry unavailable", request=request)

    client = PathsApiClient(
        api_base_url="https://api.test",
        client=httpx.Client(transport=httpx.MockTransport(timeout)),
    )
    with pytest.raises(PathsApiError, match="Cannot verify"):
        client.get_path_version(slug="dependency", version="1.0.0")


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"{}", b'{"version": null}'])
def test_version_lookup_fails_closed_on_malformed_response(body: bytes) -> None:
    api = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    )
    client = PathsApiClient(api_base_url="https://api.test", client=api)
    with pytest.raises(PathsApiError):
        client.get_path_version(slug="dependency", version="1.0.0")


def test_download_descriptor_uses_a_fresh_uncredentialed_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client_type = httpx.Client
    calls: list[httpx.Request] = []

    def storage(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert "authorization" not in request.headers
        assert "x-api-key" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(200, content=b"harmless-test-bundle")

    def api(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "wk_test_only"
        assert request.url.params["download_url"] == "1"
        return httpx.Response(
            200,
            json={
                "downloadUrl": "https://storage.googleapis.com/private/file?generation=123"
            },
        )

    api_client = client_type(
        transport=httpx.MockTransport(api), cookies={"session": "not-for-storage"}
    )
    monkeypatch.setattr(
        "wayfinder_paths.paths.client.get_api_key", lambda: "wk_test_only"
    )
    monkeypatch.setattr(
        "wayfinder_paths.paths.client.httpx.Client",
        lambda **kw: client_type(transport=httpx.MockTransport(storage), **kw),
    )
    client = PathsApiClient(api_base_url="https://api.test", client=api_client)
    target = tmp_path / "bundle.zip"
    client.download_bundle(slug="demo", version="1.0.0", out_path=target)
    assert target.read_bytes() == b"harmless-test-bundle"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://storage.googleapis.com/file",
        "https://evil.test/file",
        "https://storage.googleapis.com.evil.test/file",
        "https://token@storage.googleapis.com/file",
        "https://storage.googleapis.com:444/file",
    ],
)
def test_storage_destination_is_constrained(tmp_path: Path, url: str) -> None:
    api = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"downloadUrl": url})
        )
    )
    client = PathsApiClient(api_base_url="https://api.test", client=api)
    with pytest.raises(PathsApiError, match="destination"):
        client.download_bundle(
            slug="demo", version="1.0.0", out_path=tmp_path / "bundle.zip"
        )


def test_download_supports_old_backend_during_sdk_first_rollout(tmp_path: Path) -> None:
    api = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=b"old-bundle", headers={"content-type": "application/zip"}
            )
        )
    )
    client = PathsApiClient(api_base_url="https://api.test", client=api)
    target = client.download_bundle(
        slug="demo", version="1.0.0", out_path=tmp_path / "bundle.zip"
    )
    assert target.read_bytes() == b"old-bundle"
