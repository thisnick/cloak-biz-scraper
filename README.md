# cloak-biz-scraper

[![CI](https://github.com/thisnick/cloak-biz-scraper/actions/workflows/ci.yml/badge.svg)](https://github.com/thisnick/cloak-biz-scraper/actions/workflows/ci.yml)

A **cloaked cloud browser your AI assistant can drive** — patched Chromium that slips
past anti-bot detection, running behind your own residential proxy. On top of it are
built-in tasks that scrape BizBuySell search results and archive listings into your
Notion. Your server, your data: one-click deploy, everything else configured in a web
UI — no terminal.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/a7IwW8?referralCode=aXB6nz&utm_medium=integration&utm_source=template&utm_campaign=generic)

## Demo

https://github.com/user-attachments/assets/8bc957ef-130d-4516-a356-9efdcedeb60d

## Features

- **Cloaked browser in the cloud** — patched Chromium (CloakBrowser) that passes
  bot-detection checks. Drive it live in the dashboard or hand it to your assistant.
- **Residential routing** — send traffic through an Evomi residential proxy so sites
  see a real home IP in the country/region you choose, not a datacenter.
- **Many browsers at once** — a pool of instances, with a reserve so an interactive
  session is never starved by a batch job.
- **Profiles** — durable browser identities; each keeps its own cookies, logins, and
  settings across relaunches. Rename, delete, or rotate a profile's exit IP without
  losing its cookies.
- **Built-in listing tasks** — sweep a BizBuySell search page into structured
  listings, dedupe into a Notion database, and archive full pages.
- **Connect your own driver over CDP** — every instance hands back a CDP URL you can
  attach Playwright, or any other browser driver, to.
- **`agent_browser` MCP tools** — your assistant (ChatGPT, Claude) can open and browse
  any website through the cloaked browser.

## Set it up

No terminal — one visit to the Railway dashboard, everything else in the app's web UI.

**Watch the deploy walkthrough:**

https://github.com/user-attachments/assets/3c86899d-9f1b-4946-b1ca-4b11a53514b5

1. **Deploy.** Click the button above. Railway generates your `APP_SECRET` for you and
   builds the server (~3 minutes).
2. **Turn on Serverless.** Railway → your service → **Settings** → enable **Serverless**,
   so it sleeps when idle. Skipping it costs roughly **$8–9/month** for a server doing
   nothing.
3. **Copy `APP_SECRET`.** Railway → your service → **Variables**. This is your dashboard
   password — there's no other account to make.
4. **Open your server's URL and log in** with `APP_SECRET`.
5. **Fill in Settings** (each page tests itself and shows what it found):
   - **CloakBrowser licence** — optional; blank runs the free public build (fewer bypasses).
   - **Evomi proxy** — optional, but listing sites block non-residential IPs, so add one
     before scraping them.
   - **Notion** — optional; needed only to save listings into a database.

## Costs

Railway bills actual usage. The Hobby plan is **$5/month and includes $5 of usage**. With
Serverless on you pay for the minutes a sweep runs and close to nothing idle; without it,
roughly **$8–9/month** whether you use it or not.

Bring your own (both optional):
- **CloakBrowser Pro** — [pricing](https://cloakbrowser.dev/). A blank key uses the free
  public build.
- **Evomi residential proxy** — [pricing](https://evomi.com/); *Core Residential* is a
  fine starting tier.

## Connect ChatGPT, Claude, or Claude Code

Add your server as a connector using your URL with `/mcp` on the end (copy the exact link
from the app's **Connect** page — it's pre-filled):

```
https://your-server.up.railway.app/mcp
```

Your assistant registers itself and sends you to your own login page; paste `APP_SECRET`,
approve, and the tools appear.

**ChatGPT** — **Settings → Integrations → Plugins** → the **MCPs** tab → **Add Server** →
give it a name, choose **Streamable HTTP**, paste the link → **Save** → click the
**Authenticate** button that appears and enter your `APP_SECRET`.

**ChatGPT (classic / web)** — **Settings → Plugins** → open the **Developer Mode** setting
and turn it on → back in **Plugins**, click **Browse all plugins** → top-right **add a
custom app** → add an **MCP server**, paste the link, choose **OAuth**, **Scan Tools**,
then sign in with your `APP_SECRET`.

**Claude** — **Settings → Connectors** → **Add custom connector** → paste the link (no
client ID or secret — it uses dynamic registration) → **Connect** → sign in with your
`APP_SECRET`. Team/Enterprise owners add it first under **Organization settings →
Connectors**.
[Anthropic's connector docs](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).

**Claude Code** — run `claude mcp add --transport http cloak-biz-scraper <your-url>/mcp`,
start Claude Code, type `/mcp`, then select the server and enter your `APP_SECRET`.

## What you can do

Once it's connected, just ask:

- *"Open my Default browser, go to this listing, and tell me the asking price and cash flow."*
- *"Search BizBuySell for California businesses under $2M cash flow, then sweep the first five pages."*
- *"Sweep this search and save new listings to my Notion, skipping ones already there."*
- *"Archive this listing's full page into my Notion."*
- *"Create a profile called Broker Research with a US/California exit, then open it."*
- *"Launch my Default profile and give me a CDP URL"* — then drive it from Playwright yourself.
- *"What's my server status — Pro or public build, is the proxy working, how many slots, is Notion connected?"*

## Design

- **One browser service, many doors.** A pool of cloaked Chromium instances behind a
  single service layer, reachable four ways: the **MCP** endpoint (`/mcp`), a **REST**
  API (`/api/*`), a per-instance **CDP** URL, and the **web portal**. All configuration
  lives on a `/data` volume; the deploy sets only `APP_SECRET`.
- **Auth.** The web UI uses a cookie session (log in with `APP_SECRET`). `/mcp` and
  `/api/*` use OAuth 2.1 with dynamic client registration + PKCE — unauthenticated calls
  get a 401. CDP and live-view URLs carry short-lived, single-browser signed tokens,
  never your `APP_SECRET`.

## Security

Your licence key, proxy password, and Notion token are stored on the volume, **encrypted
at rest** with a volume-local key.

**Developing against it:** run the tests locally and in the container (`docker compose up`,
then `pytest`), keep MCP and REST returning identical payloads, and expect an adversarial
review for anything touching credentials, filesystem deletion, browser control, or
deployment.

## Contributing

PRs target **`main`**, never `release` — `release` is the deployed branch, so merging to
it ships to everyone running the template. Never commit `.env`. Add a regression test for
any behavioural change.

## FAQ

**What are the tools?** Every one is mirrored in REST over the same service layer, so MCP
and REST return identical payloads:

```
scrape_listings(url, max_pages=1, sync=false, db_id=null) -> ScrapeResult   # async: returns a job_id
get_scrape_listing_results(job_id)                        -> ScrapeResult
archive_page(url, notion_page_id)                         -> ArchiveResult   # blocks ~20s
create_instance(profile?, country?, region?, geoip?)      -> InstanceView    # includes a CDP URL
list_instances() / get_instance(id) / close_instance(id)
agent_browser(instance_id, command)                       -> command output (+ optional screenshot)
list_profiles() / create_profile(...) / update_profile(...) / new_proxy_session(name) / delete_profile(name)
server_info()                                             -> ServerInfo
```

A sweep is asynchronous (returns a `job_id`; collect with `get_scrape_listing_results`);
an archive blocks. `sync=false` needs no Notion and writes nothing; `sync=true` dedupes
and inserts only what's new. Money fields are the verbatim strings the card showed
(`"$1,258,000"`, `"Not Disclosed"`) — the Notion store parses them into numbers on the way
in.

**How do I pin the browser version?** Settings has an optional version pin. Leave it empty
for the latest build. To pin, use a **full dotted version** (`148.0.7778.215.5`); a partial
version or `latest` is rejected on save. If a valid pin ever stops downloading, that build
was retired by CloakBrowser — clear the box to get the latest.

## Credits

Instance-pool skeleton adapted from [CloakBrowser](https://github.com/CloakHQ/cloakbrowser)
(MIT).

## Licence

MIT.
