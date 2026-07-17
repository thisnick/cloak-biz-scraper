"""What a listing source is, and how one is chosen.

A source adapter knows exactly one thing: how to turn a search-results page on
one site into `Listing`s. It never learns where they land — that is the store's
half of the contract (`stores/base.py`), and keeping the two ignorant of each
other is what lets either be replaced without touching the other.

**Adapters are chosen by URL pattern, never by a parameter.** The user pastes a
URL they are already looking at; asking them to also name its source would be
asking them to tell us something the URL already says, and would let them get it
wrong. The cost is that an unrecognised URL must fail loudly rather than be
attempted with a guess — see `for_url`.
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
    """

    listings: list[Listing]
    blocked: bool = False
    title: str = ""


@runtime_checkable
class Source(Protocol):
    """One site's search-results pages."""

    # Recorded on every Listing, and the value of the Notion `Source` column.
    name: str
    # Shown when a URL matches nothing — so it must describe the URL a person
    # would paste, not a regex.
    describes: str
    example: str

    def matches(self, url: str) -> bool:
        """Whether this adapter handles the given URL."""
        ...

    def page_url(self, url: str, page: int) -> str:
        """The Nth results page for a search URL."""
        ...

    async def cards(self, page) -> CardPage:
        """Extract the listing cards from the currently loaded page."""
        ...


class UnsupportedURL(ValueError):
    """No adapter matches this URL.

    A hard error on purpose. The alternative — attempting a generic scrape —
    would return a plausible-looking empty result for a URL we never understood,
    and an agent would report "no listings found" for a page full of them.
    """

    def __init__(self, url: str, sources: list[Source]) -> None:
        self.url = url
        supported = "\n".join(f"  · {s.describes}\n    e.g. {s.example}" for s in sources)
        super().__init__(
            f"Nothing here knows how to read listings from {url!r}.\n"
            f"Supported search-results pages:\n{supported}\n"
            f"Paste the URL of a search-results page from one of these sites — the one "
            f"in your address bar with your filters already applied. To archive a single "
            f"listing's page into Notion instead, use archive_page, which works on any URL."
        )
