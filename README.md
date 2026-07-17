# cloak-biz-scraper

A self-hosted scraper for business-for-sale listings that you drive from your own
ChatGPT or Claude over MCP. You bring a CloakBrowser Pro license, a residential
proxy account, and a Notion workspace; you deploy one container; you configure it
in a web form. No terminal.

> **Status: early.** Steps 1–3 of 7 are built — the scaffold, the settings store,
> the browser core, the settings UI, the Notion store, and the scrape/archive
> tools behind both an MCP server and a REST API. The MCP endpoint is **not
> authenticated yet** (Step 4), so do not put this on a public URL. See "What
> works today".

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
- **A settings UI** behind a login: licence (with a "verify" that proves the key
  works and pre-downloads the browser), proxy (with a "test" that reports the
  measured exit IP and geo), Notion, pool sizes, and secret rotation.
- **The Notion store**: pick an existing database and see exactly what its schema
  is missing, or create one explicitly. Never auto-created; never writes a column
  you added yourself.
- **An MCP server** at `POST /mcp` — stateless Streamable HTTP, no session id —
  and a REST API at `/api/*`, both over the same service layer.
- **`scrape_listings`**: sweep a BizBuySell search-results page. Starts a job and
  returns immediately; collect it with `get_scrape_listing_results`.
- **`archive_page`**: read any page and append its content to a Notion page.
- **`create_instance` / `list_instances` / `get_instance` / `close_instance`**,
  each carrying a freshly minted, short-lived CDP URL you can drive the browser
  through.

Not built yet: OAuth (so nothing is authenticated yet), live VNC, the Railway
template.

## What it costs — and the one switch that decides it

Railway bills what you actually use: roughly **$10 per GB of memory per month** and
**$20 per vCPU per month**. The Hobby plan is $5/month and **includes $5 of usage**.

**Turn on "Serverless" for the service after you deploy.** It is one toggle in the
same Railway tab where you copy `APP_SECRET`, and it is what makes the numbers
below small. Do not skip it, and do not assume the template did it for you — **it
cannot**. This has been measured against every public template on Railway (1500 of
them, 2964 services): **not one carries a sleep setting**, because no template
authoring path can store it. See `docs/railway-template.md`.

Measured on a real deployment, on Railway's hardware:

| the server is… | memory | costs, if it never slept |
|---|---|---|
| asleep | 0 | **$0** |
| awake, freshly started, doing nothing | 0.12 GB | ~$1.20/month |
| **awake after a sweep, doing nothing** | **0.78–0.92 GB** | **~$8–9/month** |
| running a sweep | up to 1.6 GB | pennies per sweep |

The row that matters is the third one, and it is the one you would not guess:
**memory is not handed back when a sweep's browsers exit — sleeping is what
reclaims it.** So a server that never sleeps does not idle at 0.12 GB, it idles at
close to a gigabyte, for as long as it stays up. That is **~$8–9/month of paying
for nothing**, which is more than the $5 your plan includes.

With Serverless on, the server sleeps a few minutes after it goes quiet and wakes
on the next request in about a second, so you are billed for the minutes a sweep
runs and essentially nothing else. A sweep is short: a full 20-page sweep of the
Bay Area — 955 listings — took **about six minutes**.

## The tools

```
scrape_listings(url, max_pages=1, sync=false, db_id=null) -> ScrapeResult
get_scrape_listing_results(job_id)                        -> ScrapeResult
archive_page(url, notion_page_id)                         -> ArchiveResult
create_instance(profile?, country?, region?, geoip?)      -> InstanceView
list_instances() / get_instance(id) / close_instance(id)
```

Every one is mirrored in REST (`POST /api/scrape`, `GET /api/scrape/{job_id}`,
`POST /api/archive`, `/api/instances`) over the same services, so the two return
the same payloads.

**A sweep is asynchronous, an archive is not.** A multi-page sweep with
block-retries takes minutes, which is past every MCP client's wall — so
`scrape_listings` returns a `job_id` immediately and the model is told to collect
it. A single page archive takes about a minute and fits, so it blocks. That is
right at Claude Code's 60s default: raise `MCP_TOOL_TIMEOUT` if you use it there.

**Jobs live on the volume.** Railway sleeps a service after ten minutes with no
outbound traffic and wakes it on inbound — which is exactly the shape of "sweep
finishes, agent comes back later, poll wakes the container". A finished job has
to outlive the process that ran it. A job interrupted by a restart is reported as
failed, not left claiming to be working forever.

**`sync=false` needs no Notion at all.** It reads listings back and writes
nothing — not a Notion code path behind a flag, but the absence of one, so this
is usable before you have configured a database. `sync=true` dedupes against the
store and inserts only what is new.

**Money is quoted, not interpreted.** A listing's `asking_price` is the string
the card showed — `"$1,258,000"`, `"Not Disclosed"`, `"$81,000 + Inventory"`.
Turning that into a number is the *store's* job, because being a number is a fact
about a Notion column rather than about the listing: `NotionStore` parses on the
way in and leaves the cell empty when it cannot be sure, since `81000` for
"$81,000 + Inventory" is a wrong number that looks like a right one.

**Only BizBuySell search pages, and unsupported URLs fail loudly.** The adapter is
chosen by URL pattern; anything else is a hard error naming what is supported. A
best-effort scrape of a page we do not understand returns an empty result that
looks exactly like "nothing matched".

## Design

```
POST /mcp              MCP, stateless Streamable HTTP; GET -> 405; Origin validated
/api/*                 REST mirror of every tool
ws   /instances/{id}/cdp   drive a browser (short-lived signed token in the URL)
GET  /healthz          Railway healthcheck (unauthenticated)
/  /login  /settings/* the web UI (cookie session)
/data (volume)         settings, the secret, the Chromium binary cache, profiles,
                       jobs, evidence
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

That applies to `APP_SECRET` too: you can rotate it in the UI, and the rotation
survives restarts because the volume's copy is the real one. If you forget the
secret you rotated to, set a new `APP_SECRET` **and** `APP_SECRET_RESET=true` and
restart — the next boot adopts it. The reset is consumed once, so a flag left set
afterwards will not keep reverting later rotations.

**What "test proxy" does and does not tell you.** It reports the exit IP and geo
it actually measured through the proxy. It does *not* verify your credentials:
Evomi accepts any password and only rejects a wrong username, so a typo'd
password still yields a working residential exit. Nothing in the UI claims
otherwise, because nothing measured it.

**Nothing reports a value it did not measure.** If the exit IP cannot be read back
through the proxy, launching fails immediately rather than holding a pool slot on
a browser whose every page load would fail — and the timezone is reported as
unknown rather than defaulted to something plausible.

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
open http://localhost:18800   # log in with APP_SECRET
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

To exercise the MCP endpoint's transport rules against a running server:

```bash
python scripts/verify_mcp.py --base http://127.0.0.1:18800
python scripts/verify_parity.py --base http://127.0.0.1:18800 --job <job_id>
```

Or point the official inspector at it:

```bash
npx @modelcontextprotocol/inspector --cli http://127.0.0.1:18800/mcp --method tools/list
```

To exercise the Notion store against a real workspace — it creates a scratch page,
does everything under it, and archives it again, so it leaves nothing behind:

```bash
python scripts/verify_notion.py --parent <page-id> [--readonly-db <db-id>]
```

Tests:

```bash
docker run --rm -v "$PWD":/src -w /src cloak-biz-scraper:local \
  sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q"
```

A handful of tests assert what **martian** does with our markdown and need node,
so they skip outside the container and run inside it. That is deliberate: what
martian silently drops is the whole reason those tests exist, and asserting it
from memory would defeat the point — the memory was wrong.

## Credits

The instance-pool skeleton is adapted from
[CloakBrowser-Manager](https://github.com/CloakHQ/cloakbrowser) (MIT).

## Licence

MIT.
