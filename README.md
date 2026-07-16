# cloak-biz-scraper

A self-hosted scraper for business-for-sale listings that you drive from your own
ChatGPT or Claude over MCP. You bring a CloakBrowser Pro license, a residential
proxy account, and a Notion workspace; you deploy one container; you configure it
in a web form. No terminal.

> **Status: early.** Step 1 of 7 is built — the scaffold, the settings store, and
> the browser core. There is no UI, no MCP server, and no scraping yet. See
> "What works today".

## Why it exists

Listing sites are aggressively bot-hostile, so a browser that can read them has to
look like a real person's browser on a real person's connection. That takes a
stealth Chromium, a residential proxy per browser, and a coherent fingerprint —
which is a lot of setup for someone who just wants their assistant to check what's
for sale this week. This packages the hard part.

## What works today

- A FastAPI service with an unauthenticated `GET /healthz`.
- An encrypted settings store on the volume, seeded from the environment on first
  boot and authoritative thereafter.
- The browser core: a pool of stealth Chromium instances, one Evomi sticky-session
  residential proxy each, with a reserve so interactive sessions are never starved
  by a batch sweep.
- On-demand download of the CloakBrowser Pro binary into the volume.

Not built yet: the settings UI, the Notion store, the scrape and archive tools,
the MCP server, OAuth, live VNC.

## Design

```
GET /healthz          Railway healthcheck (unauthenticated)
/data (volume)        settings, the Chromium binary cache, profiles
```

**One service layer.** Everything lives in `app/services/`. Routes are façades —
they resolve a service, call it, and shape the response. The REST API, the MCP
tools, and the web UI are meant to be three doors onto one implementation rather
than three implementations that drift apart.

**One variable.** The deployment sets `APP_SECRET` and nothing else. Every other
setting is filled into a web form and stored on the volume, because an env var the
app cannot rewrite is an env var the user cannot change from a web form — it would
revert on the next restart.

Env vars still *seed* the settings on first boot, which is a convenience for local
dev and CI. After that the volume wins and the environment is ignored.

**The binary is not in the image.** The Pro Chromium is proprietary and
non-redistributable, and a mounted volume shadows the image layer anyway, so
baking it in was always pointless. The `cloakbrowser` package downloads it on
first launch into `/data/.cloakbrowser` and reuses it forever after. One
consequence worth knowing: **unpinned tracks the latest build, pinned does not.**
Set a pin in settings to freeze it.

Because of all this, `docker build` needs no credentials at all.

**Encryption at rest, honestly described.** `/data/settings.json` is encrypted
with a data key at `/data/.dek`. The key sits on the same volume as the
ciphertext, so anyone who can read the volume can read the settings — this is
*not* a defence against an attacker with volume access. It protects against
casual exposure: a snapshot, a stray backup. The key is deliberately **not**
derived from `APP_SECRET`, so rotating the secret never strands the settings.

## Local development

Needs Docker. You do not need a Python environment on your machine.

```bash
cp .env.example .env     # fill in your license + proxy
docker compose up -d
curl localhost:18800/healthz
```

`.env` is gitignored and must stay that way — this repo is public.

To prove the browser works end to end (this is what the exit criteria check):

```bash
docker compose exec app python scripts/verify_browser.py myprofile
docker compose exec app python scripts/show_settings.py
```

`verify_browser.py` launches a real browser through the proxy and reports the exit
IP, the geo, which binary ran, and what the page itself saw.

**Never launch a browser outside the container.** It would go out over your own IP
and burn its reputation with the listing sites.

Tests:

```bash
docker run --rm -v "$PWD":/src -w /src cloak-biz-scraper:local \
  sh -c "pip install -q pytest pytest-asyncio httpx && python -m pytest -q"
```

## Credits

The instance-pool skeleton is adapted from
[CloakBrowser-Manager](https://github.com/CloakHQ/cloakbrowser) (MIT).

## Licence

MIT.
