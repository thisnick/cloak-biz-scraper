# cloak-biz-scraper

**Ask your assistant what came on the market this week, and have the answer land
in your own Notion.**

You tell ChatGPT or Claude to check a search you care about — Bay Area
businesses, $750k to $10M, say — and a few minutes later the new listings are in
a Notion database you own: title, location, asking price, cash flow, EBITDA, a
link back to the listing. Ones you have already seen are not added again, so the
database is your deal flow rather than a pile of duplicates. Ask it to pull a
full listing page and it writes the whole thing into the Notion page for you to
read and mark up.

It is your server, your Notion, your data. Nobody else's account is involved and
there is nothing to log into but your own. Setting it up is a deploy button and
four web forms — **no terminal, ever.**

For the strongest listing-site setup, bring a CloakBrowser Pro licence, a
residential proxy account, and a Notion workspace. The server can also run the
public CloakBrowser build without a key; it has fewer bypasses and has not been
tested by us against the listing sites. See **What you need before you start**.

> **Status: deployable via the button below.** The server is built and tested —
> the browser core, the settings UI, the Notion store, the scrape and archive
> tools, the profile manager, and OAuth 2.1 on `/mcp` and `/api/*` — and it runs
> end to end on Railway against real listings. The one piece still unverified is
> the connector UX: **adding this server to ChatGPT and Claude has not yet been
> tested against their live connector UIs** (see *Connecting ChatGPT or Claude*).
> Everything up to that point works.

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
- On-demand download of the selected public or Pro CloakBrowser build into the volume.
- **A settings UI** behind a login: licence (with a "verify" that proves the
  selected build works and pre-downloads it), proxy (with a "test" that reports the
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

- **OAuth 2.1** on `/mcp` and `/api/*` — dynamic client registration, PKCE, and a
  login that proves `APP_SECRET`. Unauthenticated calls get a 401.
- **Runs on Railway**, proven rather than assumed: the image builds there, the
  browser downloads and launches, and a real sweep of 955 listings across 20 pages
  completed through a residential proxy. The server sleeps when idle and wakes on
  the next request in about a second, with jobs kept on disk so results survive
  the nap.

Also shipped: **the Railway template** (deploy button above) and the profile
manager. Still unverified: **connecting real ChatGPT or Claude** through their
connector UIs (Step 6).

## What you need before you start

Four things. Read the proxy one **before you buy a proxy** — it rules some
providers out entirely, and you cannot work around it afterwards.

**1. Optional: a CloakBrowser Pro licence.** Leave the key blank to deliberately
use the public build. It has fewer bypasses and we have not tested it against the
listing sites. A Pro key unlocks CloakBrowser's private builds with more bypasses;
paste it into Settings and verify it before scraping. A present but invalid or
expired key is a visible error and never silently falls back to public.

> **If your licence has an expiry date, know this one thing.** Your server caches
> the licence check, so if CloakBrowser's servers go down, your scraping keeps
> working. **That does not rescue an expired licence** — an expired one is
> reported as invalid even while offline. So if your renewal happens to fall
> during a CloakBrowser outage, your server stops scraping until both are back.
> Licences with no expiry date are not affected.

**2. Recommended: a residential proxy account** — one that allows username/password
sign-in **from any IP address**.

Without a proxy the browser still launches, but it goes out over your server's
datacenter address, and bot-hostile listing sites like BizBuySell will block a
non-residential IP. A proxy is what makes those sites usable — strongly
recommended, though no longer required just to start.

> 🔴 **If you do use a proxy, this is the requirement that catches people out.**
> Some providers make you register the IP addresses allowed to connect. **That
> cannot work here** — your server's outbound address is assigned by the hosting
> platform and **changes without warning** (across three checks of one deployment
> we saw three different addresses). There is no address to register.
>
> **Before you pay, ask the provider: "can I authenticate with just a username and
> password, from any IP?"** If the answer is no, or the plan requires IP
> allowlisting, pick a different provider or plan.

A *broken* proxy (wrong host, unroutable) still fails closed — the browser refuses
to launch rather than silently leak over the datacenter address. The datacenter
address is used only when you have deliberately configured no proxy at all.

**3. A Notion workspace**, and an integration token for it. The app can either use
a database you already have (it checks the columns and tells you exactly what is
missing) or create one for you under a page you choose. It never creates anything
you did not ask for, and it never touches columns it does not own — so you can add
your own notes, ratings and views freely.

**4. A Railway account.** The Hobby plan is $5/month and includes $5 of usage. See
**What it costs**.

## Setting it up

No terminal. You will be in the Railway dashboard once, and everything else
happens in this app's own web pages.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/a7IwW8?referralCode=aXB6nz&utm_medium=integration&utm_source=template&utm_campaign=generic)

<!-- PLACEHOLDER: step-by-step screenshots — capture from a real deploy. -->

**1. Deploy it.** Click the button. Railway asks for one thing — `APP_SECRET` —
and fills it in for you. Accept it and let it build (about three minutes).

**2. Copy `APP_SECRET`.** Railway → your service → **Variables**. Copy the value.
This is your password for the app; there is no other account to make. You can
change it later on the app's settings page.

**3. Turn on Serverless. ← do not skip this**

Railway → your service → **Settings** → enable **Serverless**.

You are already in the dashboard from step 2, so this is one more click while you
are standing there. It makes the server switch itself off when nothing is
happening, and switch back on — in about a second — the next time you use it.

**Skipping it costs about $8–9 a month and slowly rising, for a server doing
nothing.**
That is more than the $5 of usage your plan includes. Nothing will warn you: the
app works exactly the same either way, and the bill is the only feedback you get.

**The template cannot do this for you.** Not an oversight on our part — Railway
templates cannot carry the setting at all. (We checked all 1500 public templates:
not one has it. `docs/railway-template.md` has the evidence.)

**4. Open the app and finish in the browser.** Go to your Railway URL, log in with
`APP_SECRET`, and fill in the settings pages: your optional CloakBrowser licence, your
proxy, and your Notion workspace. Each page tests itself and tells you what it
actually found.

## What it costs

Railway bills what you actually use: roughly **$10 per GB of memory per month** and
**$20 per vCPU per month**. The Hobby plan is $5/month and **includes $5 of usage**.

With Serverless on (step 3), you pay for the minutes a sweep actually runs and
close to nothing the rest of the time. **Without it, you pay for every hour of the
month.** Same app, same code — the only difference is that one toggle.

Measured on a real deployment, on Railway's hardware:

| the server is… | memory | costs, if it never slept |
|---|---|---|
| asleep | 0 | **$0** |
| awake, freshly started, doing nothing | 0.12 GB | ~$1.20/month |
| **awake after a sweep, doing nothing** | **0.78–0.92 GB** | **~$8–9/month** |
| running a sweep | up to 1.6 GB | pennies per sweep |

The third row is the one that catches people out. **Memory is not handed back when
a sweep's browsers exit — sleeping is what reclaims it.** So a server that never
sleeps does not sit at 0.12 GB; it sits near a gigabyte for as long as it stays
up, whether or not you ever use it again.

For scale: a full 20-page sweep of the Bay Area — 955 listings — takes about six
minutes. Run one of those every day and, with Serverless on, you are billed for
roughly three hours of compute a month.

## Connecting ChatGPT or Claude

> ⚠️ **Untested.** The server speaks the standard protocol and its login flow has
> been driven end to end by a test client against the live URL, but **neither
> ChatGPT nor Claude has actually been connected to it yet.** The steps below are
> what we expect to work, not what we have seen work. Expect rough edges, and do
> not treat a failure here as your mistake.

Add your server as a **connector** (ChatGPT) or **custom connector** (Claude)
using your Railway URL with `/mcp` on the end:

```
https://your-server.up.railway.app/mcp
```

Your assistant registers itself and sends you to your own server's login page,
where you paste `APP_SECRET` — the same one from setup. Approve it, and the tools
appear in the assistant.

**Unverified specifics, flagged rather than guessed:**
- Whether either client's connector UI accepts this server without complaint.
- **ChatGPT's tool-call time limit is undocumented and we have not measured it.**
  If a long call fails there, that is the first thing to suspect — but we cannot
  yet tell you the number.

### If you use Claude Code

**Claude Code's default tool timeout is 60 seconds**, which matters for
`archive_page` — it reads a full listing page and writes it into Notion, so it is
the slow one.

**Measured: 20.5s on Railway** for a QuietLight listing (23.6s on a laptop). That
fits inside 60s with room to spare, so the default is usually fine. A slower or
much longer page could still exceed it. If you hit a timeout, raise it:

```bash
MCP_TOOL_TIMEOUT=180000 claude    # milliseconds
```

Sweeps are not affected: `scrape_listings` returns immediately with a job id and
you collect the results with a second call, so a five-minute sweep never sits in
a tool call waiting.

## Pinning the browser version

The settings page has an optional **version pin**. Leave it **empty** and you get
the latest CloakBrowser release, which is what you want unless a specific version
has caused you a problem.

To pin, give a **full dotted version** exactly as CloakBrowser publishes it:

```
148.0.7778.215.5      ✅
148.0.7778            ❌ rejected immediately — not a full version
latest                ❌ rejected immediately — leave the box empty instead
```

A malformed pin is rejected as soon as you save it, not silently ignored.

> **If a valid-looking pin fails to download, the version has been retired.**
> CloakBrowser removes old builds; when one is gone it is gone for every kind of
> computer, so this is never a problem with your server or its architecture. We
> checked: retired versions 404 identically on both Intel and ARM. **Clear the
> box to get the latest build.** If any error message ever tells you this is an
> architecture problem, that message is wrong — please report it.

## When something goes wrong

### "Test proxy" fails with a 407 — your password is almost certainly wrong

**Check your username and password first**, and copy them from your proxy
provider's dashboard rather than retyping them.

> **This one cost us most of a day, so it is worth a paragraph.** Some proxy
> providers **skip the password check for addresses they already trust** — often
> including your home or office. So a password can be *wrong*, and still work
> perfectly when you test it from your own computer, and then be refused the
> moment your server tries the same thing. **A proxy that works from your laptop
> is not evidence your password is right.** If your server says 407 and your
> laptop says fine, believe the server.

A 407 means the proxy answered and rejected the sign-in. It does not tell us
which credential it disliked, and it is not caused by anything on this server.

### A sweep finishes with no listings

First, run **Test proxy** on the settings page. It reports the exit IP it actually
measured, so a green result means traffic really is getting out. Most empty sweeps
are a proxy that has stopped working.

If the proxy is fine, the site may be showing a block page instead of results, or
the URL may not be a listings search page — a browse or category page can look
right and contain no listing cards. Try the search URL you would use yourself,
with the filters already applied.

> **Being straight with you: there is currently no way to see *why* from the web
> UI.** The server does save screenshots and page snapshots when a page is blocked
> — to `/data/evidence/` on its disk — but nothing serves them to you, and you
> have no terminal. `archive_page` at least returns the folder it wrote to;
> a sweep does not report one at all. **So today this is a gap, not a workflow.**
> If you are stuck, the job's error message is what you have.

### You forgot `APP_SECRET`

You are not locked out, and there is nothing to reset. `APP_SECRET` is just a
Railway variable — open your service → **Variables** → `APP_SECRET` and read it.

To change it, edit it there and redeploy. The next boot uses the new value, and
every open session is signed out (a good thing if you're changing it because it
leaked). Your settings, licence, proxy and Notion configuration are untouched —
they are not encrypted with this secret, so changing it can never strand them.

## The tools

```
scrape_listings(url, max_pages=1, sync=false, db_id=null) -> ScrapeResult
get_scrape_listing_results(job_id)                        -> ScrapeResult
archive_page(url, notion_page_id)                         -> ArchiveResult
create_instance(profile?, country?, region?, geoip?)      -> InstanceView
list_instances() / get_instance(id) / close_instance(id)
list_profiles()                                            -> list[ProfileView]
create_profile(name, country?, region?)                    -> ProfileView
update_profile(name, new_name?, country?, region?)         -> ProfileView
new_proxy_session(name)                                    -> ProfileView
delete_profile(name)                                       -> ProfileDeleteResult
```

Every one is mirrored in REST (`POST /api/scrape`, `GET /api/scrape/{job_id}`,
`POST /api/archive`, `/api/instances`, `GET|POST|PATCH|DELETE /api/profiles`, and
`POST /api/profiles/new-proxy-session`) over the same services, so the two return
the same payloads. Profile names are carried in request bodies (or the delete
query), not URL path segments, so names containing `/` work unchanged.

Profile responses are deliberately narrow: name, geography, Default/in-use
status, and whether a complete proxy is configured. Fingerprint seeds, sticky
session tokens, and cookie-storage paths never cross REST or MCP. Creating a
profile through REST/MCP is explicit and rejects name collisions. Rename and
delete are refused while the source identity is queued, opening, open, or
closing; rename also guards its destination. `Default` cannot be deleted, and
delete permanently removes that profile's cookies and logins. A new proxy
session applies on the next launch and is refused when the server is in direct
mode or its proxy settings are incomplete.

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

`APP_SECRET` is the one exception: it is **not** stored on the volume and not
seeded — it is read straight from the environment every boot, so the Railway
variable is always the live value. That is why it is the single variable the
deployment needs, and why changing it is one edit in Railway rather than a rotate
button plus a reset flag. (Encryption is keyed on a volume-local data key, never
on `APP_SECRET`, so changing the secret never strands the settings.)

**What "test proxy" does and does not tell you.** It reports the exit IP and geo
it actually measured through the proxy. It does *not* reliably prove your
credentials: some providers skip the password check for addresses they already
trust, so a wrong password can still yield a working exit when you test from your
own machine and be refused from your server. Nothing in the UI claims the
credentials are verified, because nothing measured that.

**Nothing reports a value it did not measure.** If the exit IP cannot be read back
through the proxy, launching fails immediately rather than holding a pool slot on
a browser whose every page load would fail — and the timezone is reported as
unknown rather than defaulted to something plausible.

**The binary is not in the image.** The Pro Chromium is proprietary and
non-redistributable, and a mounted volume shadows the image layer anyway, so
baking any build in was always pointless. The `cloakbrowser` package downloads
the selected public or Pro build on first launch into `/data/.cloakbrowser` and
reuses it afterward. One
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
cp .env.example .env     # optional: add a Pro licence and proxy
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

## Security

**One secret to deploy; everything else in the UI.** Railway sets only
`APP_SECRET` — your dashboard password and what assistants authenticate against.
It is read from the environment each boot and never written to the volume, so
changing it is one edit in Railway.

**Your licence key, proxy password, and Notion token** live in
`/data/settings.json`, **encrypted at rest** with a key on the volume
(`/data/.dek`). Be clear on what that buys: the key sits on the same volume as the
ciphertext, so it defends against *casual* exposure — a disk snapshot, a stray
backup — **not** against someone who can already read your volume or sign into
your dashboard. That is inherent to self-hosting: whoever controls the box
controls its config.

What we are deliberate about, and verify:

- **Secrets are never echoed back.** The settings pages show only "saved / not
  saved" — the raw key or password never re-enters the HTML, so viewing the page
  or its source never exposes them. A blank submit keeps the stored value.
- **Secrets are never logged.** Proxy URLs are masked, the licence key is hashed
  for its validation cache, and diagnostic/error paths are redacted.
- **The status API returns booleans, not values** — never the proxy password,
  licence key, or Notion token.
- **`.env` is gitignored** and has never been committed; this repo is public.
- **The browser egresses only through your proxy** (or, if you set none, the
  datacenter address) — never your own machine, and a broken proxy fails closed.
- **CDP and live-view URLs carry short-lived, single-browser signed tokens**, not
  your `APP_SECRET`: a leaked one is good for ten minutes and one browser.

## Contributing

PRs target **`main`**, never `release` — `release` is the deployed branch, so
merging to it ships to everyone running the template. Never commit `.env`. Run the
tests both locally and in the container, keep MCP and REST returning identical
payloads, and add a regression test for any behavioural change. Changes touching
credentials, filesystem deletion, browser control, or deployment get an
adversarial review before they land.

## Credits

The instance-pool skeleton is adapted from
[CloakBrowser-Manager](https://github.com/CloakHQ/cloakbrowser) (MIT).

## Licence

MIT.
