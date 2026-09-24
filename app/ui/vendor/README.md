# Vendored: @modelcontextprotocol/ext-apps 2.0.0

`ext-apps-app-2.0.0.min.js` is the `App` class (the View side of MCP Apps:
the postMessage bridge a page inside Claude or ChatGPT talks to its host
through) and three theme helpers, from the package's own self-contained
`app-with-deps` build, re-wrapped as a classic script that defines one
global, `McpApps`.

Why vendored: the page is served as one inline HTML document over MCP
(`ui://cloak-biz-scraper/live-view.html`), a host's default sandbox allows no
network requests, and this repo has no JavaScript build step. The file is
inlined into the page by `app/mcp_server.py`; nothing loads it over HTTP.

Licence: see `LICENSE-ext-apps` (MIT / Apache-2.0, as shipped in the package).

To regenerate (or move to a new version), with Node on PATH:

```sh
npm pack @modelcontextprotocol/ext-apps@2.0.0 && tar xzf modelcontextprotocol-ext-apps-2.0.0.tgz
printf 'export { App, applyDocumentTheme, applyHostStyleVariables, applyHostFonts } from "./package/dist/src/app-with-deps.js";\n' > entry.mjs
npx esbuild@0.25.10 entry.mjs --bundle --format=iife --global-name=McpApps \
  --minify --target=es2020 --legal-comments=eof --outfile=ext-apps-app-2.0.0.min.js
```

sha256 of the committed file: `cc2837c94a11bfa2fca5487cac6a9cf431ccf872933ac805411836bc42e2741e`
