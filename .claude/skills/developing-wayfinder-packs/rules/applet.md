# Applets (Static Pack UI)

An applet is an optional **static** web UI bundled inside your pack zip.

## Required files (MVP)

- `wfpack.yaml` includes:
  - `applet.build_dir`
  - `applet.manifest`
- `applet.manifest.json` exists at the path you declare
- `build_dir` contains the applet entry file (`index.html` by default)

## `applet.manifest.json` example

```json
{
  "schemaVersion": "0.1",
  "entry": "index.html",
  "preferredHeight": 760,
  "readySelector": "[data-pack-ready='true']",
  "permissions": {
    "bridge": [],
    "externalOrigins": [],
    "walletMode": "optional"
  }
}
```

Fields used by the current web MVP:
- `preferredHeight`: used to size the iframe
- `entry`: which HTML file to serve as the iframe root

## Important: asset URL base paths

Applet assets must load correctly from a **nested** applet URL like:

`/api/v1/packs/<slug>/versions/<version>/applet/`

That means:
- Avoid absolute asset URLs like `src="/assets/app.js"` or `href="/assets/app.css"`
- Prefer relative asset URLs (`./assets/...`) so they resolve under the applet path

### Vite

For Vite applets, set a relative base:

```ts
// vite.config.ts
export default defineConfig({
  base: "./",
});
```

## Wayfinder Bridge (parent communication)

Applets communicate with the host page via `postMessage`. The host sends a `wf:hello` message; the applet replies with `wf:hello_ack` and can exchange state via `wf:state`.

**Important: never use `'*'` as the target origin.** The OPA review will flag wildcard origins. Instead, capture the parent origin from the `wf:hello` event and use it for all replies:

```js
let parentOrigin = null;

window.addEventListener('message', e => {
  const d = e.data;
  if (!d || typeof d !== 'object') return;

  if (d.type === 'wf:hello') {
    parentOrigin = e.origin;
    window.parent.postMessage({ type: 'wf:hello_ack' }, parentOrigin);
  }

  if (d.type === 'wf:state') {
    // apply incoming state
  }
});
```

When emitting state back to the host, always use the captured origin:

```js
if (parentOrigin) {
  window.parent.postMessage({ type: 'wf:state', state }, parentOrigin);
}
```

## MVP constraints

For now:
- Applets are static assets only (no server code)
- Keep UI self-contained and avoid collecting secrets in the browser

