"""What a listing source is, and how one is chosen.

A source adapter knows exactly one thing: how to turn a search-results page on
one site into `Listing`s. It never learns where they land — that is the store's
half of the contract (`stores/base.py`), and keeping the two ignorant of each
other is what lets either be replaced without touching the other.

**Adapters are chosen by URL pattern, never by a parameter.** The user pastes a
URL they are already looking at; asking them to also name its source would be
asking them to tell us something the URL already says, and would let them get it
wrong.

A site with no adapter of its own is read by the generic reader
(`sources/generic.py`), when the TypeSafe Classifier (e.g. Jev) is set up to make
the decisions an adapter would have hard-coded. What that reader never does is
guess: a page with no list of businesses on it is a loud error for that source,
with evidence, never an empty result an agent would report as "no listings
matched". And a site that HAS an adapter never falls through to it — a
BizBuySell page the adapters do not read (a single listing) is refused, because
the adapter's silence about it is a decision, not a gap (see `owner_of`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..models import Listing


@dataclass(frozen=True)
class CardPage:
    """One results page as the browser saw it.

    `blocked` is separate from an empty list because they call for opposite
    responses: a block means rotate the exit IP and retry, while genuinely
    zero results means stop paging. Conflating them either retries forever on
    an empty last page or gives up silently on a challenge page.

    `error` is the third answer: the page loaded and was not a block, but the
    source can see it is not something it can read ("no list of businesses for
    sale on this page"). It fails the source loudly, with evidence, instead of
    reporting an empty list — which an agent would read as "no listings
    matched". `retry` says whether another attempt from a fresh exit IP could
    change that; for most such errors it cannot, and three attempts would only
    turn one clear failure into several minutes of the same one.

    `seen_urls` exists because paging stops on a page with nothing new, and a
    source may drop cards on purpose (a page of sold listings). Counted over
    what was *returned*, such a page looks like the end of the feed and paging
    stops early; counted over what was *on the page*, it does not. Same URL
    shape as `Listing.url`. None means "the returned listings are all there
    was".
    """

    listings: list[Listing]
    blocked: bool = False
    title: str = ""
    error: str = ""
    retry: bool = True
    seen_urls: list[str] | None = None


@runtime_checkable
class Source(Protocol):
    """One site's search-results pages.

    Five optional members are read with `getattr`, so a source that has no use
    for them simply leaves them out (they are not declared below, because a
    runtime-checkable protocol would then demand them):

    * `hosts: tuple[str, ...]` — the sites a registered adapter owns
      ("bizbuysell.com" covers www. and every other subdomain). A URL on an
      owned host that the adapter does not match is refused rather than handed
      to the generic reader; see `sources.owner_of`.
    * `warmup_url: str` — a page to land on before the first results page.
    * `begin() -> None` — called at the start of every attempt. The sweep reuses
      one source object across the attempts `scrape_with_retry` makes, so
      anything a source remembers while reading (the page it is on, what it
      decided about the layout) must be reset here, or a retry starts from the
      failed attempt's state.
    * `async advance(page, n) -> bool` — reach results page `n` (always > 1)
      from the page that is loaded, by navigating or by clicking. Page 1 is
      always `page_url(url, 1)`. Without it, page `n` is `page_url(url, n)`,
      which only works for sites that page by URL; with it, a site that pages
      with a script-only "Next" button can be swept too. Returning False means
      there is no next page, and paging stops.
    * `chooses_cards: bool` — True for a source that decides for itself which
      elements of a page it has never seen are the cards (the generic reader).
      Only then may the legibility check drop single cards the classifier judges
      not to be listings, and only then is an illegible page final rather than
      retried from a new exit IP. A site adapter's cards are read by code
      written for the page, so the classifier judges the page and never removes
      one of them; and an adapter's page that reads wrong is most likely a soft
      block served to a flagged IP.
    """

    # Recorded on every Listing, and the value of the Notion `Source` column.
    # This is the machine id (e.g. "bizbuysell_serp"); it is not shown to a person.
    name: str
    # A short human name for this source, shown on the dashboard (e.g. in a task's
    # label). Distinct from `name`: `name` is the stored id, `label` is display
    # text. A new source declares its own here, in the one place it is registered,
    # so nothing central has to learn it. See sources.label_for.
    label: str
    # Shown when a URL matches nothing — so it must describe the URL a person
    # would paste, not a regex.
    describes: str
    example: str

    def matches(self, url: str) -> bool:
        """Whether this adapter handles the given URL."""
        ...

    def page_url(self, url: str, page: int) -> str:
        """The Nth results page for a search URL (page 1 always; later pages
        only when the source has no `advance`)."""
        ...

    async def cards(self, page) -> CardPage:
        """Extract the listing cards from the currently loaded page."""
        ...


class UnsupportedURL(ValueError):
    """This URL cannot be swept, and the batch should hear why.

    Raised by `for_url` when no site adapter matches, and by the sweep when the
    generic reader cannot take the URL either (not a web address, a page on a
    site whose adapter does not read it, or no classifier set up). A hard error
    on purpose: attempting a page we cannot read would return a
    plausible-looking empty result, and an agent would report "no listings
    found" for a page full of them.

    `hint` is the specific reason, put first in the message because it is the
    part a person acts on; the list of adapter-read pages follows it. `reason`
    is the same thing in one line, for a batch where this URL is only one
    source's failure among several and the job's error names each in turn.
    """

    def __init__(self, url: str, sources: list[Source], hint: str = "",
                 reason: str = "") -> None:
        self.url = url
        self.hint = hint
        self.reason = reason or hint or "not a supported listings page"
        supported = "\n".join(f"  · {s.describes}\n    e.g. {s.example}" for s in sources)
        lead = f"Nothing here knows how to read listings from {url!r}."
        if hint:
            lead = f"Can't read listings from {url!r}: {hint}"
        super().__init__(
            f"{lead}\n"
            f"Pages read by this app's own site adapters:\n{supported}\n"
            f"For one of these, paste the URL of a search-results page — the one in your "
            f"address bar with your filters already applied. To archive a single listing's "
            f"page into Notion instead, use archive_page, which works on any URL."
        )
