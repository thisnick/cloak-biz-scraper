# Tutorial verification record

This is the working verification record for the
[shared scraper setup](set-up-scraper-for-ai.md),
[daily listing watch](set-up-daily-listing-watch.md), and
[protected-site browsing](browse-protected-sites.md) guides. It separates what was tested
against the running product from what was checked against provider documentation.

Verification date: **2026-08-30**<br>
Runtime baseline tested: **e6cfa7d**. The later documentation changes reorganize the same
verified setup and controls without changing runtime behavior.

## Tested against the local product

| Check | Result | Data effect |
|---|---|---|
| Docker deployment at `http://127.0.0.1:18800` | Healthy; dashboard sign-in and all tabs loaded | Local only |
| Browser licence | Running binary resolved to CloakBrowser `151.0.7922.108.3-pro` | None |
| Residential proxy | Real page observed a California residential ISP; browser timezone and locale matched `America/Los_Angeles` and `en-US` | Proxy traffic only |
| Saved proxy credential | Found a stale encrypted-volume value, replaced it from the valid `EVOMI_PROXY_PASSWORD`, restarted, and repeated the Pro/proxy check successfully | Local encrypted settings updated; `.env` unchanged |
| Manual browser | Launched from **Browsers**, live view rendered, proxy status appeared, then the test instance was closed | No forms submitted |
| Notion connection | Existing Listings database discovered and column mapping verified read-only | No listing rows created or edited |
| BizBuySell one-page sweep | After two blocked attempts using the stale saved credential, the corrected-password run completed in 64 seconds with 50 listings across one page and `synced=null` | **No Notion writes** (`sync=false`) |
| Failure evidence | Task result contained status, summary, error, page count, and a screenshot/HTML evidence directory | Local task history only |
| `archive_page` | Archived `https://example.com/` into a disposable Notion page in one attempt; returned `Example Domain`, 5 blocks, and 149 Markdown characters; `Source Content` was present | Scratch parent archived after verification; existing listings untouched |
| Agent reads archived body | A second synthetic page received the same five-block Example Domain archive; Claude then used the connected Notion integration to read `Source Content` and identify the archived source | No existing listing rows edited; synthetic verification page retained as reproducible evidence |

The repository's `scripts/verify_browser.py` was used for the independent browser check. It
resolves the actual process binary, visits an IP/geo echo page through the launched browser,
compares the page's timezone and locale, and closes the browser.

## Verified from source code and tool schemas

- `scrape_listings` requires one or more BizBuySell search-result or broker-profile URLs,
  returns a job ID immediately, and has one shared `max_pages` value per call.
- `get_scrape_listing_results` must be polled with the returned job ID.
- A `sync=false` result contains all found listings and makes no Notion writes.
- A `sync=true` result contains only newly inserted rows. Existing rows are counted but not
  returned or refreshed.
- Each new synced row carries `synced_row_id`, the Notion page ID expected by `archive_page`.
- `archive_page` appends page content to an existing Notion page, returns status/counts rather
  than full text, and normally blocks for 40–60 seconds.
- Archive writes are append-only and can be partial if Notion accepts some blocks before an
  error. The tutorial therefore forbids blind retries.
- `create_instance` uses a durable profile. A configured but unusable proxy fails rather than
  silently falling back to a direct connection.
- `agent_browser` accepts one listed command per tool call and requires fresh snapshot
  references after page changes.

## Checked against current provider documentation

- Railway Serverless location and the required redeploy for the setting to affect the
  container.
- CloakBrowser's key/concurrency model and current checkout instruction that a paid licence
  key is emailed after payment.
- Evomi's live **My Products → Core Residential → Proxy Generator** flow, whole-string copy
  behavior, host-first format, Core Residential endpoint, and HTTP port. The captured guide
  images omit the copied credential string and personal account controls.
- Notion internal-connection permissions and page sharing.
- Notion's hosted MCP URL, OAuth flow, read/write behavior, and current tool names.
- Claude remote-connector and Cowork schedule flows.
- ChatGPT Work availability on individual paid plans, Scheduled Tasks, and the separate plan
  restrictions documented for full custom-MCP actions.
- The signed-in Claude and individual ChatGPT accounts both exposed **Scheduled** in their
  current interfaces. Privacy-safe screenshots were captured from each product; the ChatGPT
  page visibly includes the **Chat / Work** selector.
- The workspace-owning Notion account was opened through Google OAuth. The live **Seed URLs**
  database, page-level **Connections** menu, Listings property panel, `Bot Triage` select,
  triage-criteria page, and listing-watch runbook were inspected and captured without editing
  rows. The live final triage values are `REVIEW` and `REJECT`; the tutorial and prompt template
  were aligned to them.
- The authenticated Claude account exposed **Customize → Connectors → Add custom connector**.
  Its live form asks for a name and remote MCP server URL. The existing Cloak Biz Scraper
  connection appeared as a connected custom web connector.
- The authenticated individual ChatGPT account exposed **Plugins → Create app**. Its live form
  asks for a server URL and authentication method, defaults to OAuth, and requires an explicit
  custom-MCP risk acknowledgement before creation. Privacy-safe crops of both product screens
  were added to the tutorial.
- The end-to-end archive handoff was repeated against a synthetic Notion page. The scraper's
  live `archive_page` call appended five blocks under `Source Content`; Claude then used its
  hosted Notion integration to find that page, read the archived body, and identify the source
  as Example Domain without editing the page. A privacy-safe product screenshot is included in
  the tutorial.
- The local dashboard's live **Settings → Capacity** and **Settings → Disk space** controls
  were inspected again for the operations guide. Privacy-safe screenshots show the real
  calculated task budget, host-memory warning, browser-build cleanup, task-evidence cleanup,
  and upload cleanup controls.

## Publication status

The tutorial's product, documentation, visual, and end-to-end verification checks are complete.

Do not publish screenshots that expose an integration token, `APP_SECRET`, a proxy username or
password, a private database ID, personal listings, or a private Notion page URL.
