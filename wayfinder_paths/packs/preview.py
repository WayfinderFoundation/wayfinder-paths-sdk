from __future__ import annotations

import contextlib
import socket
import threading
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from wayfinder_paths.packs.manifest import PackManifest, PackManifestError


class PackPreviewError(Exception):
    pass


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


def _serve_dir(directory: Path, *, port: int) -> tuple[ThreadingHTTPServer, int]:
    handler = partial(SimpleHTTPRequestHandler, directory=str(directory))
    actual_port = _pick_port(port)
    server = ThreadingHTTPServer(("127.0.0.1", actual_port), handler)
    return server, actual_port


def preview_pack(
    *,
    pack_dir: Path,
    parent_port: int = 3333,
    applet_port: int = 3334,
) -> PreviewUrls:
    pack_dir = pack_dir.resolve()
    manifest_path = pack_dir / "wfpack.yaml"
    if not manifest_path.exists():
        raise PackPreviewError("Missing wfpack.yaml")

    try:
        manifest = PackManifest.load(manifest_path)
    except PackManifestError as exc:
        raise PackPreviewError(str(exc)) from exc

    if not manifest.applet:
        raise PackPreviewError("This pack does not declare an applet in wfpack.yaml")

    applet_root = (pack_dir / manifest.applet.build_dir).resolve()
    if not applet_root.exists():
        raise PackPreviewError(f"Applet build_dir not found: {applet_root}")

    with TemporaryDirectory(prefix="wfpack-preview-") as tmp:
        tmp_dir = Path(tmp)
        parent_html = tmp_dir / "index.html"

        applet_server, applet_actual_port = _serve_dir(applet_root, port=applet_port)
        applet_url = f"http://127.0.0.1:{applet_actual_port}/"

        parent_html.write_text(
            "\n".join(
                [
                    "<!doctype html>",
                    "<html>",
                    "  <head>",
                    "    <meta charset='utf-8' />",
                    "    <meta name='viewport' content='width=device-width, initial-scale=1' />",
                    f"    <title>Pack Preview: {manifest.slug}</title>",
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
                    f"        <div style='font-size:18px;margin-top:6px'>{manifest.name}</div>",
                    f"        <div style='opacity:.7;margin-top:8px'>{manifest.summary}</div>",
                    "        <div style='opacity:.7;margin-top:10px'>Bridge: <span id='bridge'>pending</span></div>",
                    "      </div>",
                    "    </div>",
                    "    <div class='card' style='margin-top:12px'>",
                    f"      <iframe id='applet' sandbox='allow-scripts allow-forms allow-popups' src='{applet_url}index.html'></iframe>",
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
        for t in threads:
            t.start()

        try:
            print(
                f"Pack preview running:\n  Parent: {parent_url}\n  Applet: {applet_url}\n(Press Ctrl+C to stop)"
            )
            while True:
                threading.Event().wait(3600)
        except KeyboardInterrupt:
            pass
        finally:
            parent_server.shutdown()
            applet_server.shutdown()

        return PreviewUrls(parent_url=parent_url, applet_url=applet_url)
