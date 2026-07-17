"""Choosing an adapter by URL, and the dedupe keys it produces.

`normalize_url` is a **dedupe key**, so these are not cosmetic string tests: two
sightings of one listing that normalize differently become two rows in someone's
database, and nobody notices until it is full of duplicates.
"""
from __future__ import annotations

import pytest

from app import sources
from app.sources import UnsupportedURL
from app.sources.bizbuysell import BizBuySellSerp, listing_id_from
from app.sources.urls import canonical_url, normalize_url

BAY_AREA = (
    "https://www.bizbuysell.com/california/san-francisco-bay-area-businesses-for-sale/"
    "?q=Z2lmcm9tPTc1MDAwMCZnaXRvPTEwMDAwMDAwJmx0PTMwLDQwLDgwJnBmcm9tPTc1MDAwMA%3D%3D"
)
SACRAMENTO = "https://www.bizbuysell.com/california/sacramento-area-businesses-for-sale/"


class TestNormalization:
    def test_the_plan_fixture(self):
        """Verbatim from the plan: query, fragment, www, and trailing slash all go."""
        url = "https://www.bizbuysell.com/business-opportunity/foo/2484566/?utm_source=x#section"
        assert normalize_url(url) == "bizbuysell.com/business-opportunity/foo/2484566"
        assert listing_id_from(url) == "2484566"

    @pytest.mark.parametrize(
        "variant",
        [
            "https://www.bizbuysell.com/business-opportunity/foo/2484566/",
            "http://www.bizbuysell.com/business-opportunity/foo/2484566",
            "https://bizbuysell.com/business-opportunity/foo/2484566/?utm_source=newsletter",
            "https://BizBuySell.com/business-opportunity/foo/2484566#details",
            "bizbuysell.com/business-opportunity/foo/2484566",
        ],
    )
    def test_every_way_of_writing_one_listing_agrees(self, variant):
        """Each of these is the same listing. Disagreeing here means duplicate rows."""
        assert normalize_url(variant) == "bizbuysell.com/business-opportunity/foo/2484566"

    def test_two_different_listings_do_not_collide(self):
        a = normalize_url("https://www.bizbuysell.com/business-opportunity/foo/2484566/")
        b = normalize_url("https://www.bizbuysell.com/business-opportunity/foo/2484567/")
        assert a != b

    @pytest.mark.parametrize("empty", [None, "", "   ", "https://"])
    def test_nothing_normalizes_to_nothing(self, empty):
        assert normalize_url(empty) is None

    def test_canonical_url_keeps_the_address_but_drops_the_baggage(self):
        assert (
            canonical_url("https://www.bizbuysell.com/business-opportunity/foo/2484566/?utm=x#a")
            == "https://www.bizbuysell.com/business-opportunity/foo/2484566/"
        )


class TestListingId:
    def test_from_a_profile_query(self):
        assert listing_id_from("https://www.bizbuysell.com/listings/Profile/?q=2484566") == "2484566"

    def test_a_non_bizbuysell_url_has_no_bizbuysell_id(self):
        assert listing_id_from("https://quietlight.com/listings/18829322/") is None

    def test_a_page_without_an_id_has_none(self):
        assert listing_id_from(SACRAMENTO) is None


class TestDispatch:
    @pytest.mark.parametrize("url", [BAY_AREA, SACRAMENTO])
    def test_a_serp_selects_the_bizbuysell_adapter(self, url):
        assert sources.for_url(url).name == "bizbuysell_serp"

    @pytest.mark.parametrize(
        "url",
        [
            "https://abc.xyz/investor/",
            "https://example.com/businesses-for-sale/",
            "https://quietlight.com/listings/18829322/",
            "",
            "not a url",
        ],
    )
    def test_an_unsupported_url_is_a_hard_error(self, url):
        """Never a best-effort attempt: a generic scrape of a page we do not
        understand returns an empty result that looks exactly like "no listings
        matched", and an agent would report that as fact."""
        with pytest.raises(UnsupportedURL):
            sources.for_url(url)

    def test_the_error_names_what_is_supported(self):
        """The plan's case. The message has to leave the reader able to act."""
        with pytest.raises(UnsupportedURL) as exc:
            sources.for_url("https://abc.xyz/investor/")
        message = str(exc.value)
        assert "abc.xyz/investor" in message
        assert "bizbuysell.com" in message
        assert "businesses-for-sale" in message
        # And point at the tool that does handle an arbitrary page.
        assert "archive_page" in message

    def test_a_listing_page_is_not_a_search_page(self):
        """A BizBuySell detail URL is the right site and the wrong job. Sweeping
        it would find only the cards it links to, which is not what was asked."""
        assert not BizBuySellSerp().matches(
            "https://www.bizbuysell.com/business-opportunity/premier-restoration/2515728/"
        )

    def test_a_broker_profile_is_out_of_scope(self):
        assert not BizBuySellSerp().matches(
            "https://www.bizbuysell.com/business-broker/andrew-rogerson/rogerson/20770/"
        )

    def test_a_lookalike_domain_does_not_match(self):
        assert not BizBuySellSerp().matches("https://bizbuysell.com.evil.example/x-businesses-for-sale/")


class TestPaging:
    def test_page_one_has_no_segment(self):
        assert BizBuySellSerp().page_url(SACRAMENTO, 1) == SACRAMENTO

    def test_later_pages_use_a_path_segment(self):
        assert BizBuySellSerp().page_url(SACRAMENTO, 3) == (
            "https://www.bizbuysell.com/california/sacramento-area-businesses-for-sale/3/"
        )

    def test_the_query_survives_paging(self):
        """The filters live in ?q=. Losing them would silently sweep every
        listing in the region instead of the ones that were asked for."""
        assert "?q=Z2lmcm9t" in BizBuySellSerp().page_url(BAY_AREA, 2)

    def test_paging_a_url_that_is_already_paged(self):
        """Idempotent: page 2 of "page 3 of X" is page 2 of X, not page 3/2."""
        paged = BizBuySellSerp().page_url(SACRAMENTO, 3)
        assert BizBuySellSerp().page_url(paged, 2) == (
            "https://www.bizbuysell.com/california/sacramento-area-businesses-for-sale/2/"
        )
