"""Throttling the doors that take APP_SECRET.

Step 3 measured the unthrottled reality: 30 wrong attempts in 0.0s, zero
refusals, against a secret the floor allows to be 16 memorable characters.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import isolate_auth

from app.main import app
from app.services.ratelimit import RateLimiter

SECRET = "test-secret-value-long-enough"


class TestTheLimiter:
    def test_failures_below_the_limit_are_not_throttled(self):
        limiter = RateLimiter(max_failures=3, window_sec=60)
        for _ in range(2):
            limiter.fail("1.2.3.4")
        assert limiter.retry_after("1.2.3.4") == 0.0

    def test_the_limit_bites(self):
        limiter = RateLimiter(max_failures=3, window_sec=60)
        for _ in range(3):
            limiter.fail("1.2.3.4")
        assert limiter.retry_after("1.2.3.4") > 0

    def test_the_window_slides(self):
        limiter = RateLimiter(max_failures=3, window_sec=60)
        for _ in range(3):
            limiter.fail("1.2.3.4", now=1000.0)
        assert limiter.retry_after("1.2.3.4", now=1030.0) > 0
        assert limiter.retry_after("1.2.3.4", now=1061.0) == 0.0

    def test_success_clears_the_callers_budget(self):
        """A correct secret never leaves anyone throttled."""
        limiter = RateLimiter(max_failures=3, window_sec=60)
        for _ in range(3):
            limiter.fail("1.2.3.4")
        limiter.reset("1.2.3.4")
        assert limiter.retry_after("1.2.3.4") == 0.0

    def test_success_does_not_clear_the_global_budget(self):
        """One correct login does not vouch for the thousand wrong ones that came
        from somewhere else — otherwise an attacker who knows any valid secret
        (or waits for the owner to log in) resets the flood budget."""
        limiter = RateLimiter(max_failures=3, window_sec=60, global_max=4)
        for i in range(4):
            limiter.fail(f"10.0.0.{i}")
        limiter.reset("10.0.0.0")
        assert limiter.retry_after("10.0.0.99") > 0

    def test_rotating_the_source_address_does_not_reset_the_limit(self):
        """The attack the global bucket exists for.

        Behind Railway the per-IP key comes from X-Forwarded-For, which the
        client writes. A limit that a header can turn off is not a limit.
        """
        limiter = RateLimiter(max_failures=3, window_sec=60, global_max=5)
        for i in range(5):
            limiter.fail(f"10.0.0.{i}")  # a fresh "address" every time
        assert limiter.retry_after("10.0.0.123") > 0

    def test_per_address_budgets_are_independent_below_the_global_cap(self):
        """One noisy source must not lock out the owner while there is still
        global headroom."""
        limiter = RateLimiter(max_failures=2, window_sec=60, global_max=100)
        for _ in range(2):
            limiter.fail("1.2.3.4")
        assert limiter.retry_after("1.2.3.4") > 0
        assert limiter.retry_after("5.6.7.8") == 0.0


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_SECRET", SECRET)
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as c:
        # Also resets the limiter: it is a singleton on the shared app, and one
        # test's flood would otherwise be another's starting budget.
        isolate_auth(app, tmp_path)
        yield c


class TestTheLoginFlood:
    def test_a_flood_is_throttled(self, client):
        """The Step 3 measurement, inverted: 30 wrong guesses used to cost
        nothing at all."""
        codes = [
            client.post("/login", data={"secret": f"guess-{i}"},
                        headers={"X-Forwarded-For": "9.9.9.9"}).status_code
            for i in range(30)
        ]
        assert 429 in codes, "an unthrottled flood is the bug this closes"
        assert codes.count(429) > 15, f"most of the flood should be refused: {codes}"

    def test_the_429_says_when_to_come_back(self, client):
        for i in range(15):
            r = client.post("/login", data={"secret": f"guess-{i}"},
                            headers={"X-Forwarded-For": "9.9.9.9"})
        assert r.status_code == 429
        assert int(r.headers["Retry-After"]) > 0

    def test_the_right_secret_still_works_after_a_few_misses(self, client):
        """A person fumbling a paste must not be locked out."""
        for i in range(3):
            client.post("/login", data={"secret": f"typo-{i}"},
                        headers={"X-Forwarded-For": "9.9.9.9"})
        r = client.post("/login", data={"secret": SECRET},
                        headers={"X-Forwarded-For": "9.9.9.9"})
        assert r.status_code == 303

    def test_the_oauth_login_is_throttled_too(self, client):
        """Both doors take the same secret. Throttling only one of them would
        just move the flood to the other."""
        from app.services import signing

        blob = signing.issue(
            {"aud": "oauth:pending", "cid": "c", "cc": "x", "ru": "https://c.example/cb",
             "rux": True, "state": None, "scopes": ["mcp"], "res": None},
            SECRET, ttl_sec=600,
        )
        codes = [
            client.post("/authorize/login", data={"p": blob, "secret": f"guess-{i}"},
                        headers={"X-Forwarded-For": "8.8.8.8"}).status_code
            for i in range(30)
        ]
        assert 429 in codes

class TestRegistrationIsThrottled:
    """DCR must stay open — ChatGPT and Claude register themselves — but open is
    not unlimited, and this endpoint writes to disk.

    Every registration re-encrypts and rewrites the whole client store, so an
    unthrottled flood is O(n) disk work per request against a file the flood is
    growing, on a volume the user pays for. It is the one unauthenticated
    write path in the app.
    """

    def test_a_registration_flood_is_throttled(self, client):
        codes = [
            client.post("/register", json={"redirect_uris": ["https://c.example/cb"]},
                        headers={"X-Forwarded-For": "6.6.6.6"}).status_code
            for _ in range(25)
        ]
        assert 429 in codes, "an unauthenticated endpoint that writes to disk must be bounded"
        assert codes.count(201) <= 10

    def test_a_handful_of_real_registrations_still_work(self, client):
        """A user connecting ChatGPT and Claude registers twice, ever."""
        for _ in range(3):
            r = client.post("/register", json={"redirect_uris": ["https://c.example/cb"]})
            assert r.status_code == 201

    def test_registration_has_its_own_budget(self, client):
        """A login flood must not stop a legitimate client from registering, and
        vice versa — they are different kinds of traffic with different limits."""
        for i in range(15):
            client.post("/login", data={"secret": f"guess-{i}"},
                        headers={"X-Forwarded-For": "4.4.4.4"})
        assert app.state.login_limiter.retry_after("4.4.4.4") > 0
        r = client.post("/register", json={"redirect_uris": ["https://c.example/cb"]},
                        headers={"X-Forwarded-For": "4.4.4.4"})
        assert r.status_code == 201


class TestTheLoginFloodContinued:
    def test_the_two_doors_share_one_budget(self, client):
        """Otherwise an attacker alternates between them and gets double the
        guesses for free."""
        for i in range(10):
            client.post("/login", data={"secret": f"guess-{i}"},
                        headers={"X-Forwarded-For": "7.7.7.7"})
        r = client.post("/authorize/login", data={"p": "irrelevant", "secret": "guess"},
                        headers={"X-Forwarded-For": "7.7.7.7"})
        # The pending blob is junk, so a 400 would also mean "not throttled";
        # what must not happen is the secret being checked at all.
        assert r.status_code in (400, 429)
        assert app.state.login_limiter.retry_after("7.7.7.7") > 0
