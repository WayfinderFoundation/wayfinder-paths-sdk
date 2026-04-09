# Applets (Static Path UI)

An applet is an optional **static** web UI bundled inside your path zip.

## Required files (MVP)

- `wfpath.yaml` includes:
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
  "readySelector": "[data-path-ready='true']",
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

`/api/v1/paths/<slug>/versions/<version>/applet/`

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

## Fetching live data at runtime

Applets embedded on the Strategies host can fetch Delta Lab data via the
public timeseries endpoint — no API key needed. The endpoint is same-origin
when the applet is served from the path page.

### Pattern

1. Capture the API base from the host bridge: prefer `wf:state.apiBase`,
   then the `wf:hello` event origin.
2. Fetch: `${apiBase}/api/v1/delta-lab/public/assets/${SYMBOL}/timeseries/?series=price,funding,pendle&lookback_days=60&limit=2000`
3. Treat non-200 as "data unavailable" — render a fallback UI, don't crash.

Available series: `price`, `funding`, `lending`, `yield`, `pendle`, `boros`.
Not all series exist for all symbols. Degrade gracefully.

### Static fallback for local development

On localhost the public endpoint is cross-origin and will be blocked.
Bundle pre-computed data under `data/` and attempt live fetch first:

```js
async function loadData(apiBase) {
  try {
    var r = await fetch(apiBase +
      "/api/v1/delta-lab/public/assets/SYMBOL/timeseries/?series=price,funding&lookback_days=30&limit=2000");
    if (!r.ok) throw new Error(r.status);
    return { live: true, data: await r.json() };
  } catch {
    var fb = await fetch("./data/fallback.json");
    return fb.ok ? { live: false, data: await fb.json() } : null;
  }
}
```

When displaying fallback data, show the date it was generated so the viewer
knows they are looking at stale data (e.g. "Showing cached data from
2026-04-01").

### External APIs (Pendle, Hyperliquid, etc.)

Direct browser fetch to third-party APIs will fail due to CORS.
Use only data available through the Delta Lab public endpoint.
If a needed series isn't available for your symbol, fall back to
static bundled data or request the series be added to Delta Lab.

## User-configurable parameters

When an applet runs a computation (e.g. backtest), expose key inputs
so the viewer can explore scenarios without republishing:

- **Investment amount (USD):** number input, sensible default (e.g. $100,000)
- **Lookback period (days):** number input, default 30, range 7–90

Wire inputs to re-run the computation and re-render on change (debounce
~400ms). If a parameter requires a live data re-fetch (e.g. lookback
changes the API query), refetch then recompute. If a parameter only
affects the computation (e.g. notional), recompute on cached data.

Disable controls that have no effect in the current mode (e.g. lookback
is meaningless when using static fallback data — disable the input and
add a title tooltip explaining why).

## Chart hover tooltips

All canvas-based charts should include hover interaction by default.

### Pattern

1. Maintain a `chartRegistry` array. After drawing each chart, register:
   canvas element, series array (`[{data, mapped?, color, label, lineWidth?}]`),
   timestamps array, value formatter, y-bounds, and optional transform threshold.
2. Create one shared tooltip div (`position:fixed`, `pointer-events:none`,
   dark background, `z-index:100`).
3. On each canvas `mousemove`:
   - Resolve nearest data index from cursor x
   - Redraw chart with a dashed vertical crosshair + filled dots at intersections
   - Populate tooltip: timestamp line + one color-coded value per series
4. On `mouseleave`: hide tooltip, redraw chart without crosshair.
5. For transformed scales (e.g. symlog), store both raw data and mapped
   on the series entry. Tooltip shows raw values; crosshair renders in
   transformed space.

## MVP constraints

For now:
- Applets are static assets only (no server code)
- Keep UI self-contained and avoid collecting secrets in the browser
