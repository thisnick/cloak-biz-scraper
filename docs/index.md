# Wake up to businesses worth reviewing

Cloak Biz Scraper turns saved business-marketplace searches into a daily AI workflow you
control. Every morning, your agent sweeps the search pages, applies your written filters,
archives promising listing pages into Notion, and leaves you a short review queue.

[Start the visual setup tutorial](set-up-daily-listing-watch.md){ .md-button .md-button--primary }
[View the open-source project](https://github.com/thisnick/cloak-biz-scraper){ .md-button }

![Example morning review with scraped listings, triage decisions, and archive status](assets/setup-tutorial/outcome-morning-review.png)

*Illustrative sample using fictional listings. Your review queue will contain the businesses
found by your saved searches.*

## What you will build

1. **Seed URLs in Notion** hold the marketplace searches you want watched.
2. **Cloak Biz Scraper** opens those searches through CloakBrowser and your residential proxy.
3. **Claude or ChatGPT Work** runs the workflow on a morning schedule.
4. **Objective triage rules** reject only listings that clearly fail a written filter.
5. **`archive_page`** saves the full page into the listing's Notion page when the agent needs
   the details or wants to preserve the source.
6. **A morning report** links the listings marked `REVIEW` and reports failures honestly.

## Let your AI browse websites with anti-bot detection

Some public websites block an AI agent's normal browser because it comes from a datacenter or
looks automated. Once Cloak Biz Scraper is connected, tell the agent to use the scraper's
`create_instance` and `agent_browser` MCP tools instead. The protected-site guide includes
copy-and-paste prompts for one-page and multi-step browsing.

[Open the protected-site prompt guide](browse-protected-sites.md){ .md-button .md-button--primary }

![A live CloakBrowser instance the AI can operate](assets/setup-tutorial/scraper-live-browser.png)

For the scheduled listing workflow, copy the [daily agent runbook](prompts/listing-watch-runbook.md)
into Notion after completing the setup tutorial.

## Verified against the real workflow

The guide was checked against the running Pro browser and residential proxy. A one-page
BizBuySell sweep returned 50 listings with `sync=false`. The archive tool then saved a
synthetic page under `Source Content`, and Claude read that archived body back through its
Notion connector without editing it.

![Claude reading content saved by archive_page through Notion](assets/setup-tutorial/claude-archive-read.png)

See the [verification record](tutorial-verification.md) for the full test boundary.
