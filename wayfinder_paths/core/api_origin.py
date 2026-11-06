"""Hosts the SDK may send the Wayfinder API key to.

The allowlist lives in code on purpose: a value read from ``config.json``, the
environment or the command line can only pick one of these hosts, never add one.
"""

from __future__ import annotations

import os
import re

import httpx

OFFICIAL_API_HOSTS = frozenset(
    {
        "wayfinder.ai",
        "strategies.wayfinder.ai",
        "strategies-dev.wayfinder.ai",
    }
)

# Local backend development (``http://localhost:8000``). Never accepted inside a
# Wayfinder Shells instance, where loopback ports belong to the instance's own services.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})

# Set on the machine by the Shells control plane, not read from the volume.
PINNED_API_BASE_ENV = "WAYFINDER_API_BASE_URL"

_URL_RE = re.compile(
    r"(?P<scheme>https?)://(?P<host>[a-z0-9.-]+)(?::(?P<port>[0-9]{1,5}))?"
    r"(?P<path>(?:/[A-Za-z0-9._~-]*)*)",
    re.ASCII,
)


class UntrustedApiOriginError(ValueError):
    pass


def on_shells_instance() -> bool:
    return bool(os.environ.get("OPENCODE_INSTANCE_ID"))


def _loopback_allowed() -> bool:
    return not on_shells_instance()


def _parse(url: str) -> tuple[str, str, int | None, str] | None:
    match = _URL_RE.fullmatch(url)
    if not match:
        return None
    scheme, host = match["scheme"], match["host"]
    port = int(match["port"]) if match["port"] else None
    path = match["path"].rstrip("/")
    if host.startswith(".") or host.endswith(".") or ".." in host:
        return None
    if any(segment in {".", ".."} for segment in path.split("/")):
        return None
    # Cross-check with the parser that actually sends the request.
    # Cross-check with the parser that sends the request. ``.host`` decodes
    # punycode lazily, so its IDNA ValueError surfaces inside the try.
    try:
        parsed = httpx.URL(url)
        matches = (
            parsed.scheme == scheme
            and parsed.host == host
            and parsed.port == port
            and not parsed.userinfo
            and not parsed.query
            and not parsed.fragment
        )
    except (httpx.InvalidURL, ValueError):
        return None
    return (scheme, host, port, path) if matches else None


def _is_official(parts: tuple[str, str, int | None, str]) -> bool:
    scheme, host, port, _ = parts
    return scheme == "https" and port is None and host in OFFICIAL_API_HOSTS


def pinned_api_base_url() -> str | None:
    """The instance's own API base URL on a Shells instance, else ``None``."""
    if not on_shells_instance():
        return None
    pinned = os.environ.get(PINNED_API_BASE_ENV, "").strip().rstrip("/")
    if not pinned:
        return None
    parts = _parse(pinned)
    if parts is None or not _is_official(parts):
        raise UntrustedApiOriginError(
            f"{PINNED_API_BASE_ENV} {pinned!r} is not an official Wayfinder API origin"
        )
    return pinned


def validate_api_base_url(url: str, *, label: str = "API base URL") -> str:
    """Return *url* without a trailing slash, or raise if it is not allowed.

    Allowed: ``https://<official host>[/path]`` with no userinfo, port, query
    or fragment. On a Shells instance with a pinned API URL, only that host.
    Off a Shells instance, ``http(s)://localhost[:port]`` and ``127.0.0.1``
    are also allowed for local backend development.
    """
    raw = url if isinstance(url, str) else ""
    candidate = raw.strip().rstrip("/")
    parts = _parse(candidate) if candidate else None
    if parts is not None:
        if _is_official(parts):
            pinned = pinned_api_base_url()
            if pinned is None or httpx.URL(pinned).host == parts[1]:
                return candidate
        elif parts[1] in _LOOPBACK_HOSTS and _loopback_allowed():
            return candidate
    allowed = ", ".join(f"https://{h}" for h in sorted(OFFICIAL_API_HOSTS))
    raise UntrustedApiOriginError(
        f"{label} {raw!r} is not an allowed Wayfinder API origin (allowed: {allowed})"
    )


def is_under_api_base(url: str, base_url: str) -> bool:
    """True when *url* is on *base_url*'s scheme, host and port, and under its path."""
    target = _parse(url.strip()) if isinstance(url, str) else None
    base = _parse(base_url.strip().rstrip("/")) if isinstance(base_url, str) else None
    if target is None or base is None:
        return False
    if target[:3] != base[:3]:
        return False
    base_path, target_path = base[3], target[3]
    return target_path == base_path or target_path.startswith(base_path + "/")


def drop_api_key_off_origin(request: httpx.Request, base_url: str) -> None:
    """Strip the API key from *request* unless it targets *base_url*'s origin.

    Used as an httpx request hook, which also runs for each redirect hop: httpx
    keeps custom headers such as ``X-API-KEY`` when it follows a redirect.
    """
    base = httpx.URL(base_url)
    if (request.url.scheme, request.url.host, request.url.port) != (
        base.scheme,
        base.host,
        base.port,
    ):
        request.headers.pop("X-API-KEY", None)
