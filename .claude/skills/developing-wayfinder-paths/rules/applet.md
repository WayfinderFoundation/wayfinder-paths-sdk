# Applets are retired

Wayfinder no longer serves path applets (browser UI bundled in a path), and
review treats runtime applet analysis as off. Don't add an applet to a new
path, and don't offer to build one:

- `wayfinder path init` scaffolds no applet by default; don't pass `--applet`.
- Leave the `applet:` block out of `wfpath.yaml`.
- An existing path's applet folder can stay or go; nothing shows it.
