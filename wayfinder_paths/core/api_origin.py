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

# Local backend development (``http://localhost:8000``, or the host machine from
# inside a dev container). Never accepted inside a Wayfinder Shells instance, where
# loopback ports belong to the instance's own services.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "host.docker.internal"})

# Set on the machine by the Shells control plane, not read from the volume.
PINNED_API_BASE_ENV = "WAYFINDER_API_BASE_URL"

_URL_RE = re.compile(
    r"(?P<scheme>https?)://(?P<host>[a-z0-9.-]+|\[::1\])(?::(?P<port>[0-9]{1,5}))?"
    r"(?P<path>(?:/[A-Za-z0-9._~-]*)*)",
    re.ASCII,
)
# Splits off scheme and authority so only they are case-folded, never the path.
_AUTHORITY_RE = re.compile(
    r"(?P<scheme>[A-Za-z]+)://(?P<authority>[^/?#]*)(?P<rest>.*)", re.S
)
_DEFAULT_PORTS = {"https": ":443", "http": ":80"}


class UntrustedApiOriginError(ValueError):
    """A configured API URL is not one the SDK will send the API key to.

    ``value`` is the URL as given, ``source`` where it came from (a config.json
    key, an environment variable or ``--api-url``).
    """

    def __init__(self, value: object, *, source: str, fix: str | None = None):
        self.value = value
        self.source = source
        super().__init__(
            f"{source} {value!r} is not an allowed Wayfinder API origin. "
            f"Allowed: {_describe_allowed()}. "
            f"Fix: {fix or 'use one of the allowed URLs, or remove the setting to use the default'}."
        )


def on_shells_instance() -> bool:
    return bool(os.environ.get("OPENCODE_INSTANCE_ID"))


def _loopback_allowed() -> bool:
    return not on_shells_instance()


def _describe_allowed() -> str:
    official = ", ".join(f"https://{h}" for h in sorted(OFFICIAL_API_HOSTS))
    if on_shells_instance():
        pinned = os.environ.get(PINNED_API_BASE_ENV, "").strip()
        scope = f"only {pinned!r} ({PINNED_API_BASE_ENV})" if pinned else official
        return (
            f"{scope}; local hosts are not accepted inside a Wayfinder Shells instance"
        )
    local = ", ".join(sorted(_LOOPBACK_HOSTS))
    return (
        f"{official} (optionally with a path such as /api/v1); for local development "
        f"http:// or https:// on {local}, any port"
    )


def _normalise(url: str) -> str:
    """Lower-case the scheme and host and drop a default port.

    Only ASCII is folded: ``str.lower`` maps some non-ASCII letters (the Kelvin
    sign, for one) onto ASCII ones, which would let a look-alike host through.
    """
    match = _AUTHORITY_RE.fullmatch(url)
    if not match or not (match["scheme"] + match["authority"]).isascii():
        return url
    scheme, authority = match["scheme"].lower(), match["authority"].lower()
    default_port = _DEFAULT_PORTS.get(scheme)
    if default_port and authority.endswith(default_port):
        authority = authority[: -len(default_port)]
    return f"{scheme}://{authority}{match['rest']}"


def _unparse(parts: tuple[str, str, int | None, str]) -> str:
    scheme, host, port, path = parts
    return f"{scheme}://{host}{f':{port}' if port is not None else ''}{path}"


def _parse(url: str) -> tuple[str, str, int | None, str] | None:
    url = _normalise(url)
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
    # Cross-check with the parser that sends the request. ``.host`` decodes
    # punycode lazily, so its IDNA ValueError surfaces inside the try.
    try:
        parsed = httpx.URL(url)
        matches = (
            parsed.scheme == scheme
            and parsed.host == host.strip("[]")
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
    raw = os.environ.get(PINNED_API_BASE_ENV, "")
    pinned = raw.strip().rstrip("/")
    if not pinned:
        return None
    parts = _parse(pinned)
    if parts is None or not _is_official(parts):
        raise UntrustedApiOriginError(
            raw,
            source=f"Environment variable {PINNED_API_BASE_ENV}",
            fix="this is set by the Shells control plane; contact Wayfinder support",
        )
    return _unparse(parts)


def validate_api_base_url(
    url: str, *, label: str = "API base URL", fix: str | None = None
) -> str:
    """Return *url* normalised, or raise :class:`UntrustedApiOriginError`.

    Normalised means: surrounding whitespace and trailing slashes removed, scheme
    and host lower-cased, a default port (``:443``/``:80``) dropped.

    Allowed: ``https://<official host>[/path]`` with no userinfo, port, query
    or fragment. On a Shells instance with a pinned API URL, only that host.
    Off a Shells instance, ``http(s)://`` on localhost, 127.0.0.1, ``[::1]`` and
    host.docker.internal (any port) are also allowed for local development.

    *label* names where *url* came from and *fix* says how to correct it; both
    end up in the error message.
    """
    raw = url if isinstance(url, str) else ""
    candidate = raw.strip().rstrip("/")
    parts = _parse(candidate) if candidate else None
    if parts is not None:
        if _is_official(parts):
            pinned = pinned_api_base_url()
            if pinned is None or httpx.URL(pinned).host == parts[1]:
                return _unparse(parts)
        elif parts[1] in _LOOPBACK_HOSTS and _loopback_allowed():
            return _unparse(parts)
    raise UntrustedApiOriginError(url, source=label, fix=fix)


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
