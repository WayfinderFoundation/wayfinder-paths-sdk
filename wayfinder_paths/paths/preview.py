from __future__ import annotations

import contextlib
import json
import socket
import threading
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qsl, urlparse

import httpx

from wayfinder_paths.core.config import get_api_base_url, get_api_key
from wayfinder_paths.paths.manifest import (
    PathManifest,
    PathManifestError,
    PathPanelConfig,
)


class PathPreviewError(Exception):
    pass


# Read-only wf:fetch allowlist for --live mode: panel-facing resource ->
# (required capability, upstream API path relative to the api base). Mirrors
# the production host's pathPanelDataAllowlist.ts — keep in lockstep, and keep
# it GET/read-only: the author's key must never be able to trigger writes from
# a panel under development. Resource names are the stable panel-facing
# contract; upstream paths may name vendors (e.g. the wallet-PnL provider) and
# can change without breaking panels.
_LIVE_FETCH_ALLOWLIST: dict[str, tuple[str, str]] = {
    "/blockchain/balances/wallet/": (
        "wallet_read",
        "/blockchain/balances/wallet/",
    ),
    "/blockchain/balances/aggregated-chart": (
        "wallet_read",
        "/blockchain/balances/aggregated-chart",
    ),
    "/blockchain/balances/activity/": (
        "wallet_read",
        "/blockchain/balances/activity/",
    ),
    "/blockchain/hyperliquid/portfolio-state/": (
        "positions.read",
        "/blockchain/hyperliquid/portfolio-state/",
    ),
    "/blockchain/portfolio/balance-chart": (
        "pnl.read",
        "/blockchain/zerion/balance-chart",
    ),
    "/blockchain/portfolio/pnl": ("pnl.read", "/blockchain/zerion/pnl"),
    "/blockchain/hyperliquid/markets/": (
        "market.read",
        "/blockchain/hyperliquid/markets/",
    ),
    "/blockchain/hyperliquid/funding/": (
        "market.read",
        "/blockchain/hyperliquid/funding/",
    ),
    "/blockchain/tokens/discover/": (
        "market.read",
        "/blockchain/tokens/discover/",
    ),
    "/blockchain/tokens/security/": (
        "market.read",
        "/blockchain/tokens/security/",
    ),
}
# Same response cap as the production data proxy.
_LIVE_PROXY_MAX_BYTES = 512 * 1024
_LIVE_PROXY_TIMEOUT_S = 15.0


def _live_proxy_envelope(
    *,
    resource: str,
    params: list[tuple[str, str]],
    declared_capabilities: frozenset[str],
    api_base: str,
    api_key: str,
    fetch: object | None = None,
) -> dict:
    """Build the wf:fetch_result envelope ({ok, data} | {ok, error}) for one
    live proxied read. Enforces the same gates the production host does —
    allowlist + capability-declared-by-manifest; the grant gate stays in the
    browser inspector so authors can exercise the deny path interactively."""
    entry = _LIVE_FETCH_ALLOWLIST.get(resource)
    if entry is None:
        return {
            "ok": False,
            "error": {"code": "denied", "message": "resource not on allowlist"},
        }
    required, upstream_path = entry
    if required not in declared_capabilities:
        return {
            "ok": False,
            "error": {
                "code": "denied",
                "message": f"panel does not declare capability '{required}'",
            },
        }

    url = api_base.rstrip("/") + upstream_path
    fetcher = fetch or (
        lambda u, p: httpx.get(
            u,
            params=p,
            headers={"X-API-Key": api_key},
            timeout=_LIVE_PROXY_TIMEOUT_S,
        )
    )
    try:
        response = fetcher(url, params)
    except Exception as exc:
        return {
            "ok": False,
            "error": {"code": "upstream_error", "message": str(exc)[:200]},
        }
    if response.status_code != 200:
        return {
            "ok": False,
            "error": {
                "code": "upstream_error",
                "message": f"upstream returned {response.status_code}",
            },
        }
    body = response.content
    if len(body) > _LIVE_PROXY_MAX_BYTES:
        return {
            "ok": False,
            "error": {"code": "invalid", "message": "response exceeds size cap"},
        }
    try:
        data = json.loads(body) if body else None
    except ValueError:
        return {
            "ok": False,
            "error": {"code": "invalid", "message": "upstream returned non-JSON"},
        }
    return {"ok": True, "data": data}


@dataclass(frozen=True)
class PanelPreviewInspection:
    slug: str
    version: str
    panel: PathPanelConfig
    panel_root: Path | None  # None in --dev-server mode
    entry: str


@dataclass(frozen=True)
class PreviewInspection:
    slug: str
    name: str
    applet_manifest_path: Path
    applet_root: Path
    entry: str
    entry_path: Path


@dataclass(frozen=True)
class PreviewUrls:
    parent_url: str
    applet_url: str


def _pick_port(port: int) -> int:
    if port:
        return port
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass(frozen=True)
class _LiveProxyConfig:
    api_base: str
    api_key: str
    declared_capabilities: frozenset[str]


class _PanelHostRequestHandler(SimpleHTTPRequestHandler):
    """Static host-page server, plus (in --live mode) the /live-proxy route
    that attaches the author's API key SERVER-side. The key never reaches the
    browser; and because this handler sends no CORS headers, the cross-origin
    panel frame can't call the proxy directly — data still flows only through
    the host page's bridge, exactly like production."""

    live_proxy: _LiveProxyConfig | None = None

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.live_proxy is not None and self.path.startswith("/live-proxy"):
            self._handle_live_proxy(self.live_proxy)
            return
        super().do_GET()

    def _handle_live_proxy(self, config: _LiveProxyConfig) -> None:
        query = parse_qsl(urlparse(self.path).query)
        resource = next((v for k, v in query if k == "resource"), "")
        params = [(k, v) for k, v in query if k != "resource"]
        envelope = _live_proxy_envelope(
            resource=resource,
            params=params,
            declared_capabilities=config.declared_capabilities,
            api_base=config.api_base,
            api_key=config.api_key,
        )
        body = json.dumps(envelope).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _CorsAssetRequestHandler(SimpleHTTPRequestHandler):
    """Panel-dist asset server. The panel iframe is sandboxed WITHOUT
    allow-same-origin, so its document has an opaque origin and module
    scripts are fetched in CORS mode with Origin: null — without an ACAO
    header the browser blocks them and the panel never boots. Mirrors the
    production bundle-serving headers (decorate_ui_response)."""

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cross-Origin-Resource-Policy", "cross-origin")
        super().end_headers()


def _serve_dir(
    directory: Path,
    *,
    port: int,
    live_proxy: _LiveProxyConfig | None = None,
    cors_assets: bool = False,
) -> tuple[ThreadingHTTPServer, int]:
    handler_cls: type[SimpleHTTPRequestHandler] = SimpleHTTPRequestHandler
    if cors_assets:
        handler_cls = _CorsAssetRequestHandler
    if live_proxy:
        handler_cls = type(
            "_LivePanelHostRequestHandler",
            (_PanelHostRequestHandler,),
            {"live_proxy": live_proxy},
        )
    handler = partial(handler_cls, directory=str(directory))
    actual_port = _pick_port(port)
    server = ThreadingHTTPServer(("127.0.0.1", actual_port), handler)
    return server, actual_port


def inspect_preview_path(*, path_dir: Path) -> PreviewInspection:
    path_dir = path_dir.resolve()
    manifest_path = path_dir / "wfpath.yaml"
    if not manifest_path.exists():
        raise PathPreviewError("Missing wfpath.yaml")

    try:
        manifest = PathManifest.load(manifest_path)
    except PathManifestError as exc:
        raise PathPreviewError(str(exc)) from exc

    if not manifest.applet:
        raise PathPreviewError("This path does not declare an applet in wfpath.yaml")

    applet_root = (path_dir / manifest.applet.build_dir).resolve()
    if not applet_root.exists():
        raise PathPreviewError(f"Applet build_dir not found: {applet_root}")

    applet_manifest_path = (path_dir / manifest.applet.manifest_path).resolve()
    if not applet_manifest_path.exists():
        raise PathPreviewError(f"Applet manifest not found: {applet_manifest_path}")

    try:
        applet_manifest = json.loads(applet_manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PathPreviewError(
            f"Failed to parse applet manifest: {applet_manifest_path}"
        ) from exc

    if not isinstance(applet_manifest, dict):
        raise PathPreviewError("applet.manifest.json must be a JSON object")

    entry = str(applet_manifest.get("entry") or "").strip() or "index.html"
    entry_path = (applet_root / entry).resolve()
    if not entry_path.exists():
        raise PathPreviewError(f"Applet entry not found: {entry_path}")

    return PreviewInspection(
        slug=manifest.slug,
        name=manifest.name,
        applet_manifest_path=applet_manifest_path,
        applet_root=applet_root,
        entry=entry,
        entry_path=entry_path,
    )


def inspect_panel_preview(
    *, path_dir: Path, panel_id: str, dev_server: str | None
) -> PanelPreviewInspection:
    path_dir = path_dir.resolve()
    manifest_path = path_dir / "wfpath.yaml"
    if not manifest_path.exists():
        raise PathPreviewError("Missing wfpath.yaml")
    try:
        manifest = PathManifest.load(manifest_path)
    except PathManifestError as exc:
        raise PathPreviewError(str(exc)) from exc

    panel = next((p for p in manifest.panels if p.panel_id == panel_id), None)
    if panel is None:
        declared = ", ".join(p.panel_id for p in manifest.panels) or "(none)"
        raise PathPreviewError(
            f"Panel '{panel_id}' not found in wfpath.yaml. Declared: {declared}"
        )

    panel_root: Path | None = None
    if dev_server is None:
        panel_root = (path_dir / panel.build_dir).resolve()
        if not panel_root.exists():
            raise PathPreviewError(
                f"Panel build_dir not found: {panel_root} "
                "(build the panel, or pass --dev-server <url> for HMR)"
            )
        entry_path = (panel_root / panel.entry).resolve()
        if not entry_path.exists():
            raise PathPreviewError(f"Panel entry not found: {entry_path}")

    return PanelPreviewInspection(
        slug=manifest.slug,
        version=manifest.version,
        panel=panel,
        panel_root=panel_root,
        entry=panel.entry,
    )


def _render_host_asset(name: str, replacements: dict[str, str]) -> str:
    root = resources.files("wayfinder_paths.paths").joinpath(
        "templates", "panel_host", name
    )
    text = root.read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    return text


def preview_panel(
    *,
    path_dir: Path,
    panel_id: str,
    dev_server: str | None = None,
    live: bool = False,
    parent_port: int = 3333,
    panel_port: int = 3334,
) -> PreviewUrls:
    """Serve the panel dev sandbox: a workspace-shaped host page with a bridge
    emulator (mock context + wf:fetch fixtures) around the panel iframe. With
    --dev-server the iframe points at the author's own dev server for HMR.
    With --live, wf:fetch proxies real read-only data through the author's
    API key (attached server-side; the key never enters the browser)."""
    inspection = inspect_panel_preview(
        path_dir=path_dir, panel_id=panel_id, dev_server=dev_server
    )

    live_proxy: _LiveProxyConfig | None = None
    if live:
        api_key = get_api_key()
        if not api_key:
            raise PathPreviewError(
                "--live needs your Wayfinder API key: set WAYFINDER_API_KEY or "
                "add system.api_key to your wayfinder config"
            )
        live_proxy = _LiveProxyConfig(
            api_base=get_api_base_url(),
            api_key=api_key,
            declared_capabilities=frozenset(inspection.panel.capabilities),
        )

    with TemporaryDirectory(prefix="wfpath-panel-preview-") as tmp:
        tmp_dir = Path(tmp)
        panel_server: ThreadingHTTPServer | None = None
        panel_actual_port = panel_port

        if dev_server is not None:
            # HMR: point the iframe straight at the author's dev server. That
            # server serves same-origin content, so the vite client needs
            # allow-same-origin — relaxed ONLY in dev, flagged in the chrome.
            panel_src = dev_server.rstrip("/") + "/"
            sandbox = "allow-scripts allow-same-origin"
            dev_mode = "true"
            dev_chip = '<span class="devchip">DEV MODE — sandbox relaxed</span>'
        else:
            assert inspection.panel_root is not None
            panel_server, panel_actual_port = _serve_dir(
                inspection.panel_root, port=panel_port, cors_assets=True
            )
            panel_src = f"http://127.0.0.1:{panel_actual_port}/{inspection.entry}"
            # Production-parity isolation for the mock run.
            sandbox = "allow-scripts"
            dev_mode = "false"
            dev_chip = ""

        # Copy the static host assets into the served dir, render index.html.
        for asset in ("host.css", "host.js", "fixtures.js"):
            src = resources.files("wayfinder_paths.paths").joinpath(
                "templates", "panel_host", asset
            )
            (tmp_dir / asset).write_text(
                src.read_text(encoding="utf-8"), encoding="utf-8"
            )

        index_html = _render_host_asset(
            "index.html.tmpl",
            {
                "slug": inspection.slug,
                "panel_id": inspection.panel.panel_id,
                "panel_name": inspection.panel.name,
                "version": inspection.version,
                "capabilities_json": json.dumps(list(inspection.panel.capabilities)),
                "dev_mode": dev_mode,
                "dev_chip": dev_chip,
                "live_mode": "true" if live_proxy else "false",
                "live_chip": (
                    '<span class="livechip">LIVE DATA — your API key</span>'
                    if live_proxy
                    else ""
                ),
                "sandbox": sandbox,
                "panel_src": panel_src,
            },
        )
        (tmp_dir / "index.html").write_text(index_html, encoding="utf-8")

        parent_server, parent_actual_port = _serve_dir(
            tmp_dir, port=parent_port, live_proxy=live_proxy
        )
        parent_url = f"http://127.0.0.1:{parent_actual_port}/index.html"

        def serve(server: ThreadingHTTPServer) -> None:
            server.serve_forever(poll_interval=0.25)

        servers = [parent_server] + ([panel_server] if panel_server else [])
        for server in servers:
            threading.Thread(target=serve, args=(server,), daemon=True).start()

        try:
            mode = f"dev-server {dev_server}" if dev_server else "static dist"
            if live_proxy:
                mode += ", LIVE data via your API key"
            print(
                f"Panel preview running ({mode}):\n"
                f"  Open: {parent_url}\n(Press Ctrl+C to stop)"
            )
            while True:
                threading.Event().wait(3600)
        except KeyboardInterrupt:
            pass
        finally:
            for server in servers:
                server.shutdown()

        return PreviewUrls(
            parent_url=parent_url,
            applet_url=panel_src,
        )


def preview_path(
    *,
    path_dir: Path,
    parent_port: int = 3333,
    applet_port: int = 3334,
) -> PreviewUrls:
    inspection = inspect_preview_path(path_dir=path_dir)

    with TemporaryDirectory(prefix="wfpath-preview-") as tmp:
        tmp_dir = Path(tmp)
        parent_html = tmp_dir / "index.html"

        applet_server, applet_actual_port = _serve_dir(
            inspection.applet_root,
            port=applet_port,
        )
        applet_url = f"http://127.0.0.1:{applet_actual_port}/"

        parent_html.write_text(
            "\n".join(
                [
                    "<!doctype html>",
                    "<html>",
                    "  <head>",
                    "    <meta charset='utf-8' />",
                    "    <meta name='viewport' content='width=device-width, initial-scale=1' />",
                    f"    <title>Path Preview: {inspection.slug}</title>",
                    "    <style>",
                    "      :root { color-scheme: dark; font-family: ui-sans-serif, system-ui; }",
                    "      body { margin: 0; padding: 16px; background: #0b0f0c; color: #e7f5ea; }",
                    "      .row { display: flex; gap: 12px; flex-wrap: wrap; }",
                    "      .card { border: 1px solid rgba(255,255,255,0.12); border-radius: 16px; padding: 12px; background: rgba(255,255,255,0.04); }",
                    "      iframe { width: 100%; border: 0; }",
                    "    </style>",
                    "  </head>",
                    "  <body>",
                    "    <div class='row'>",
                    "      <div class='card' style='flex: 1 1 520px'>",
                    "        <div style='opacity:.8'>Parent shell</div>",
                    f"        <div style='font-size:18px;margin-top:6px'>{inspection.name}</div>",
                    "        <div style='opacity:.7;margin-top:10px'>Bridge: <span id='bridge'>pending</span></div>",
                    "      </div>",
                    "    </div>",
                    "    <div class='card' style='margin-top:12px'>",
                    f"      <iframe id='applet' sandbox='allow-scripts allow-forms allow-popups' src='{applet_url}{inspection.entry}'></iframe>",
                    "    </div>",
                    "    <script>",
                    "      const iframe = document.getElementById('applet');",
                    "      const bridge = document.getElementById('bridge');",
                    "      iframe.addEventListener('load', () => {",
                    "        bridge.textContent = 'loading';",
                    "        iframe.contentWindow?.postMessage({ type: 'wf:hello', version: '0.1' }, '*');",
                    "      });",
                    "      window.addEventListener('message', (event) => {",
                    "        const msg = event.data;",
                    "        if (!msg || typeof msg !== 'object') return;",
                    "        if (msg.type === 'wf:hello_ack') bridge.textContent = 'ready';",
                    "      });",
                    "    </script>",
                    "  </body>",
                    "</html>",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        parent_server, parent_actual_port = _serve_dir(tmp_dir, port=parent_port)
        parent_url = f"http://127.0.0.1:{parent_actual_port}/index.html"

        def serve(server: ThreadingHTTPServer) -> None:
            server.serve_forever(poll_interval=0.25)

        threads = [
            threading.Thread(target=serve, args=(applet_server,), daemon=True),
            threading.Thread(target=serve, args=(parent_server,), daemon=True),
        ]
        for thread in threads:
            thread.start()

        try:
            print(
                f"Path preview running:\n  Parent: {parent_url}\n  Applet: {applet_url}\n(Press Ctrl+C to stop)"
            )
            while True:
                threading.Event().wait(3600)
        except KeyboardInterrupt:
            pass
        finally:
            parent_server.shutdown()
            applet_server.shutdown()

        return PreviewUrls(parent_url=parent_url, applet_url=applet_url)
