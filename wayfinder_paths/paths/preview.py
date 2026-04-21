from __future__ import annotations

import contextlib
import json
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from wayfinder_paths.paths.manifest import PathManifest, PathManifestError


class PathPreviewError(Exception):
    pass


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


PROXY_PREFIX = "/api/v1/delta-lab/public/"
PROXY_TIMEOUT = 10.0


class ProxyingAppletHandler(SimpleHTTPRequestHandler):
    """Static file handler that proxies `/api/v1/delta-lab/public/*` to the
    configured upstream. Proxied through the applet server so the applet's
    fetches are same-origin (no CORS preflight)."""

    upstream_base: str = ""

    def _send_cors(self) -> None:
        # The applet iframe is sandboxed without allow-same-origin, giving it
        # an opaque origin — every fetch becomes cross-origin and requires
        # CORS headers, even when hitting the iframe's own server.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self) -> None:  # noqa: N802 - http.server naming
        self.send_response(204)
        self._send_cors()
        self.end_headers()

    def end_headers(self) -> None:
        self._send_cors()
        super().end_headers()

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        if self.upstream_base and self.path.startswith(PROXY_PREFIX):
            self._proxy()
            return
        super().do_GET()

    def _proxy(self) -> None:
        upstream = self.upstream_base.rstrip("/") + self.path
        req = urllib.request.Request(
            upstream,
            headers={
                "Accept": "application/json",
                # Cloudflare blocks the default Python UA with a 1010 signature ban.
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=PROXY_TIMEOUT) as resp:
                body = resp.read()
                self.send_response(resp.status)
                self.send_header(
                    "Content-Type",
                    resp.headers.get("Content-Type", "application/json"),
                )
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
        except urllib.error.HTTPError as exc:
            body = exc.read() if hasattr(exc, "read") else b""
            self.send_response(exc.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            msg = json.dumps({"error": "proxy_error", "detail": str(exc)}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)


def _pick_port(port: int) -> int:
    if port:
        return port
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _serve_dir(directory: Path, *, port: int) -> tuple[ThreadingHTTPServer, int]:
    handler = partial(SimpleHTTPRequestHandler, directory=str(directory))
    actual_port = _pick_port(port)
    server = ThreadingHTTPServer(("127.0.0.1", actual_port), handler)
    return server, actual_port


def _serve_applet(
    directory: Path, *, port: int, upstream_base: str
) -> tuple[ThreadingHTTPServer, int]:
    # Subclass per-call to carry upstream_base; handler classes are per-request.
    class _Handler(ProxyingAppletHandler):
        pass

    _Handler.upstream_base = upstream_base
    handler = partial(_Handler, directory=str(directory))
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


def preview_path(
    *,
    path_dir: Path,
    parent_port: int = 3333,
    applet_port: int = 3334,
    api_base: str = "https://strategies.wayfinder.ai",
) -> PreviewUrls:
    inspection = inspect_preview_path(path_dir=path_dir)

    with TemporaryDirectory(prefix="wfpath-preview-") as tmp:
        tmp_dir = Path(tmp)
        parent_html = tmp_dir / "index.html"

        applet_server, applet_actual_port = _serve_applet(
            inspection.applet_root,
            port=applet_port,
            upstream_base=api_base,
        )
        applet_origin = f"http://127.0.0.1:{applet_actual_port}"
        applet_url = applet_origin + "/"
        # The applet fetches apiBase + "/api/v1/delta-lab/public/..." — route that
        # through the applet server (same-origin, no CORS preflight).
        applet_api_base_js = json.dumps(applet_origin)

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
                    "      html, body { margin: 0; padding: 0; background: #0b0f0c; color: #e7f5ea; height: 100%; }",
                    "      body { display: flex; flex-direction: column; padding: 12px; gap: 12px; box-sizing: border-box; }",
                    "      .row { display: flex; gap: 12px; flex-wrap: wrap; flex: 0 0 auto; }",
                    "      .card { border: 1px solid rgba(255,255,255,0.12); border-radius: 16px; padding: 12px; background: rgba(255,255,255,0.04); }",
                    "      .applet-card { flex: 1 1 auto; display: flex; min-height: 0; padding: 0; overflow: hidden; }",
                    "      iframe { width: 100%; height: 100%; min-height: 720px; border: 0; display: block; background: #0b0d10; border-radius: 16px; }",
                    "    </style>",
                    "  </head>",
                    "  <body>",
                    "    <div class='row'>",
                    "      <div class='card' style='flex: 1 1 520px'>",
                    "        <div style='opacity:.8'>Parent shell</div>",
                    f"        <div style='font-size:18px;margin-top:6px'>{inspection.name}</div>",
                    "        <div style='opacity:.7;margin-top:10px'>Bridge: <span id='bridge'>pending</span></div>",
                    f"        <div style='opacity:.6;margin-top:4px;font-size:12px'>Proxying {PROXY_PREFIX}* → {api_base}</div>",
                    "      </div>",
                    "    </div>",
                    "    <div class='card applet-card'>",
                    f"      <iframe id='applet' sandbox='allow-scripts allow-forms allow-popups' src='{applet_url}{inspection.entry}'></iframe>",
                    "    </div>",
                    "    <script>",
                    "      const iframe = document.getElementById('applet');",
                    "      const bridge = document.getElementById('bridge');",
                    "      iframe.addEventListener('load', () => {",
                    "        bridge.textContent = 'loading';",
                    "        iframe.contentWindow?.postMessage({ type: 'wf:hello', version: '0.1' }, '*');",
                    f"        iframe.contentWindow?.postMessage({{ type: 'wf:state', state: {{ apiBase: {applet_api_base_js} }} }}, '*');",
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
                f"Path preview running:\n  Parent: {parent_url}\n  Applet: {applet_url}\n  Proxying {PROXY_PREFIX}* → {api_base}\n(Press Ctrl+C to stop)"
            )
            while True:
                threading.Event().wait(3600)
        except KeyboardInterrupt:
            pass
        finally:
            parent_server.shutdown()
            applet_server.shutdown()

        return PreviewUrls(parent_url=parent_url, applet_url=applet_url)
