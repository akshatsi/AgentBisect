# Vendored libraries

The report pages are one HTML file with these libraries pasted in. They come from the npm registry
(`npm pack preact@10.29.8 htm@3.1.1`, fetched once; the build has no other network use) and are
copied unchanged into `vendor/`. `agentbisect/report/page.py` makes two edits when it inlines them:
it drops the source-map comment, and it writes the `http://` of the XML namespace strings as
`http:\/\/`, which JavaScript reads as the same text, so a built page holds no `http://`.

| library | version | licence | files in `vendor/` | source |
|---|---|---|---|---|
| preact | 10.29.8 | MIT | `preact.min.js`, `hooks.umd.js` (the `preact/hooks` build), `LICENSE.preact` | `preact@10.29.8` on npm, `dist/preact.min.js` and `hooks/dist/hooks.umd.js` |
| htm | 3.1.1 | Apache-2.0 | `htm.js`, `LICENSE.htm` | `htm@3.1.1` on npm, `dist/htm.js` |

Preact 11 (2026) ships as ES modules only. The pages need classic scripts that run from a plain
inline `<script>`, so they use the last 10.x release, which still ships a global build.

htm is Apache-2.0, not MIT. Its licence text is in `vendor/LICENSE.htm`; the package has no NOTICE
file.

The app code in this folder (`common.js`, `run_app.js`, `study_app.js`, the `.css` files) is part
of AgentBisect.
