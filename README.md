# cloak-biz-scraper

[![CI](https://github.com/thisnick/cloak-biz-scraper/actions/workflows/ci.yml/badge.svg)](https://github.com/thisnick/cloak-biz-scraper/actions/workflows/ci.yml)

A **cloaked cloud browser your AI assistant can drive** — patched Chromium designed to
reduce anti-bot blocks, with optional routing through your own residential proxy. On top
of it are built-in tasks that scrape BizBuySell search results and archive listings into
your Notion. Your server, your data: one-click deploy, everything else configured in a
web UI. Core setup needs no terminal.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/a7IwW8?referralCode=aXB6nz&utm_medium=integration&utm_source=template&utm_campaign=generic)

## Demo

https://github.com/user-attachments/assets/8bc957ef-130d-4516-a356-9efdcedeb60d

## Features

- **Cloaked browser in the cloud** — patched Chromium (CloakBrowser) designed to reduce
  bot-detection blocks. Drive it live in the dashboard or hand it to your assistant;
  no browser can guarantee access to every protected site.
- **Residential routing** — send traffic through an Evomi residential proxy so sites
  see a real home IP in the country/region you choose, not a datacenter.
- **Many browsers at once** — a pool of instances, with a reserve so batch jobs cannot
  consume the browser slots reserved for interactive work.
- **Profiles** — durable browser identities; each keeps its own cookies, logins, and
  settings across relaunches. Rename, delete, or rotate a profile's exit IP without
  losing its cookies — or clear one to sign it out of everything and start over,
  including the Default profile, which cannot be deleted. Settings shows what each
  profile is using on disk.
- **Built-in listing tasks** — sweep BizBuySell search or broker pages into structured
  listings, dedupe into a Notion database, and append readable page content to a listing's
  existing Notion page.
- **Connect your own driver over CDP** — every instance hands back a short-lived CDP URL
  you can attach Playwright, or any other browser driver, to.
- **`agent_browser` MCP tool** — your assistant (ChatGPT, Claude) can open, browse, and
  fill in any website through the cloaked browser.
- **Send it a file to upload** — an HTTP-capable assistant can request a staging ticket,
  post a photo or PDF to the server, and attach it to a form in the cloaked browser.
  Files are checked by their contents and deleted a couple of hours later.

## Set it up

No terminal — one visit to the Railway dashboard, everything else in the app's web UI.

Start with the **[visual setup site](https://thisnick.github.io/cloak-biz-scraper/)**. Its
[shared scraper guide](docs/set-up-scraper-for-ai.md) covers deployment, the Pro key, proxy,
MCP connection, and a harmless agent test. Then choose the
[daily listing watch](docs/set-up-daily-listing-watch.md) or
[protected-site prompt guide](docs/browse-protected-sites.md). The site also covers
[live monitoring and takeover](docs/monitor-and-take-control.md) and
[advanced controls](docs/advanced-controls.md).

**Watch the deploy walkthrough:**

https://github.com/user-attachments/assets/3c86899d-9f1b-4946-b1ca-4b11a53514b5

1. **Deploy.** Click the button above. Railway generates your `APP_SECRET` for you and
   builds the server (~3 minutes).
2. **Turn on Serverless.** Railway → your service → **Settings → Deploy → Serverless**,
   then redeploy so the setting takes effect and the service sleeps when idle.
3. **Copy `APP_SECRET`.** Railway → your service → **Variables**. This is your dashboard
   password — there's no other account to make.
4. **Open your server's URL and log in** with `APP_SECRET`.
5. **Fill in Settings** (each page tests itself and shows what it found):
   - **CloakBrowser licence** — the paid Pro build is required for the documented listing
     and protected-site workflows. A blank key runs the public build for basic testing only.
   - **Evomi proxy** — required for the documented workflows because target sites commonly
     block Railway's datacenter IP.
   - **Notion** — optional; needed only to save listings into a database.

## Costs

Railway charges for the plan plus resource usage above the included amount. Its Hobby
plan currently has a **$5 monthly minimum that includes $5 of usage**. Serverless can
reduce idle compute charges, but the actual cost depends on browser time, memory, storage,
and network traffic. Check [Railway's current pricing](https://railway.com/pricing) before
deploying.

Bring your own for the documented protected-site and listing workflows:

- **CloakBrowser Pro** — [pricing](https://cloakbrowser.dev/). A blank key uses the free
  public build, which is suitable for basic testing but not the verified workflow.
- **Evomi residential proxy** — [pricing](https://evomi.com/); *Core Residential* is a
  practical starting tier.

## Connect ChatGPT, Claude, or Claude Code

Add your server as a connector using your URL with `/mcp` on the end (copy the exact link
from the app's **Connect** page — it's pre-filled):

```
https://your-server.up.railway.app/mcp
```

Your assistant registers itself and sends you to your own login page; paste `APP_SECRET`,
approve, and the tools appear.

The location and availability of custom MCP connections varies by product and plan.
Follow the current screen-by-screen instructions in the
**[shared scraper guide](docs/set-up-scraper-for-ai.md)**, then run its smoke test. The
complete workflow needs a client that can call action tools, not only read data.

**After you upgrade the server, reconnect if the new tools do not appear.** Some clients
cache the tool list from an earlier connection.

**Claude Code** — run `claude mcp add --transport http cloak-biz-scraper <your-url>/mcp`,
start Claude Code, type `/mcp`, then select the server and enter your `APP_SECRET`.

## What you can do

Once it's connected, just ask:

- *"Open my cloaked scraper, go to this listing, and tell me the asking price and cash flow."*
- *"Search BizBuySell for California businesses with an asking price under $2M, then sweep the first five pages using cloaked scraper."*
- *"Sweep this search and save new listings to my Notion, skipping ones already there."*
- *"Archive this listing's readable page content into its Notion page."*
- *"Upload this photo to the listing form on that page."* — this requires an assistant
  that can send the file to the temporary HTTP upload URL before controlling the browser.
- *"Launch my Default profile in cloaked scraper and give me a CDP URL"* — then drive it from Playwright yourself.

## Design

- **One browser service, many doors.** A pool of cloaked Chromium instances behind a
  single service layer, reachable four ways: the **MCP** endpoint (`/mcp`), a **REST**
  API (`/api/*`), a per-instance **CDP** URL, and the **web portal**. All configuration
  lives on a `/data` volume; the deploy sets only `APP_SECRET`.
- **Settings → Disk space** shows what the volume is holding — browser versions, saved
  task history, and uploaded files — and clears each of them.
- **Auth.** The web UI uses a cookie session (log in with `APP_SECRET`). `/mcp` and
  `/api/*` use OAuth 2.1 with dynamic client registration + PKCE — unauthenticated calls
  get a 401. CDP and live-view URLs carry short-lived, single-browser signed tokens,
  never your `APP_SECRET`.

## Security

Your licence key, proxy password, and Notion token are stored on the volume, **encrypted
at rest** with a volume-local key.

**Developing against it:** run the tests locally and in the container (`docker compose up`,
then `pytest`), keep MCP and REST behavior aligned, and expect an adversarial review for
anything touching credentials, filesystem deletion, browser control, or deployment.

## Contributing

PRs target **`main`**, never `release` — `release` is the deployed branch, so merging to
it ships to everyone running the template. Never commit `.env`. Add a regression test for
any behavioural change.

## FAQ

**What are the tools?** The MCP currently publishes these 15 tools:

| Tool | What it does |
| --- | --- |
| `server_info()` | Read proxy, browser-build, pool-capacity, and Notion connection status without exposing secrets. |
| `scrape_listings(urls, max_pages=1, sync=false)` | Start one asynchronous BizBuySell sweep across one or more search-results or broker-profile URLs; results are merged and de-duplicated. |
| `get_scrape_listing_results(job_id)` | Poll a sweep without blocking. Completed results are retained for two weeks. |
| `archive_page(url, notion_page_id)` | Read a page and append its readable content to an existing Notion page. It takes roughly a minute and repeated successful calls append the content again. |
| `create_instance(profile="Default", country=null, region=null, geoip=true)` | Launch a browser with a durable profile and return a short-lived CDP URL plus a live-view URL when available. It closes after 15 minutes idle or 60 minutes total. |
| `list_instances()` | List running browsers with fresh CDP and live-view URLs. |
| `get_instance(instance_id)` | Get one running browser and refresh its short-lived connection URLs. |
| `close_instance(instance_id)` | Close a browser and discard its current page state; saved profile cookies and logins remain. Safe to retry. |
| `agent_browser(instance_id, command)` | Run one allowlisted browser command such as `navigate`, `snapshot`, `read`, `click`, `fill`, `upload`, or `screenshot`. Screenshots return an image alongside the text result. |
| `create_upload_url()` | Mint a temporary HTTP upload ticket and ready-made `curl` command for staging images or PDFs. The tool itself does not carry the file bytes. |
| `list_profiles()` | Read safe status for the durable browser profiles. |
| `create_profile(name, country=null, region=null)` | Create a durable, initially logged-out browser identity without launching it. |
| `update_profile(name, new_name=null, country=null, region=null)` | Rename a profile and/or change its proxy geography. Geography applies on the next proxied launch; `Default` cannot be renamed. |
| `new_proxy_session(name)` | Rotate a profile's sticky proxy session for its next launch while keeping cookies, logins, fingerprint, and geography. |
| `delete_profile(name)` | Permanently delete a profile and its cookies and logins. `Default` cannot be deleted. |

Every tool has a REST counterpart over the same service layer. Structured operations use
the same response models, but transport-specific data can differ: for example,
`agent_browser` returns a screenshot as an MCP image block and as base64 in REST.

Tools also publish MCP safety hints so a client can make a better approval decision.
`server_info`, `get_scrape_listing_results`, `list_profiles`, `list_instances`, and
`get_instance` are marked read-only. `agent_browser`, `close_instance`, and
`delete_profile` are marked destructive; `close_instance` is also marked safe to retry.
`scrape_listings` is marked write-capable because `sync=true` writes to Notion, even though
its default `sync=false` mode does not.

`create_upload_url` takes no arguments. MCP cannot carry local file bytes, so the client
must run the returned `curl` command or make the equivalent multipart HTTP request. The
upload response contains a server path; `agent_browser`'s `upload` command accepts only a
live path created by this flow. If the client has no shell or HTTP capability, it cannot
perform this hand-off.

A sweep is asynchronous: collect it with `get_scrape_listing_results`. With `sync=false`,
the completed result contains every listing found and does not use Notion. With
`sync=true`, it writes only new rows to the Notion database configured in Settings and
returns only those newly inserted listings, each with a `synced_row_id` suitable for
`archive_page`; existing rows are counted under `synced.existing`. Money fields are the
verbatim strings shown on the listing card (`"$1,258,000"`, `"Not Disclosed"`) and are
parsed into numbers only when written to Notion.

**How do I pin the browser version?** Settings has an optional version pin. Leave it empty
for the latest build. To pin, use a **full dotted version** (`148.0.7778.215.5`); a partial
version or `latest` is rejected on save. If a valid pin ever stops downloading, that build
was retired by CloakBrowser — clear the box to get the latest.

## Credits

Instance-pool skeleton adapted from [CloakBrowser](https://github.com/CloakHQ/cloakbrowser)
(MIT).

## Licence

MIT.
