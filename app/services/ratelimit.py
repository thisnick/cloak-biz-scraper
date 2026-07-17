"""Throttling the two doors that take APP_SECRET.

**Why this is needed at all.** `MIN_SECRET_LENGTH` is 16, which permits a
memorable secret rather than the 32 random chars Railway mints — and a memorable
16 characters is guessable at a few hundred tries a second. Step 3 measured the
unthrottled reality: **30 wrong attempts in 0.0s, zero refusals.** One secret
guards the browser, the proxy, and the Notion workspace, so it has to cost
something to guess.

**Be precise about what this does NOT fix.** Railway sleeps on the absence of
*outbound* packets, so it is tempting to file throttling under cost control. It
is not: a `429` is an outbound response exactly like a `401` is. An attacker who
wants to hold the machine awake and burn the owner's money can do it against any
endpoint that answers at all, and nothing this module does changes that. What it
buys is the brute force, which is the thing worth buying. The awake-pinning
vector survives Step 4 and belongs to Railway's edge, not to us.

**Failures only.** A correct secret never consumes budget, so no amount of
legitimate use throttles anyone.

**Two buckets, and the global one is the real one.** Per-IP is the fair limit —
it keeps one noisy source from locking out the owner — but it is only as
trustworthy as the client address, and behind Railway's proxy that address comes
from `X-Forwarded-For`, which the client writes. An attacker who rotates that
header gets a fresh per-IP bucket every request, so a per-IP limit alone is not a
limit. The global bucket cannot be rotated away and is what actually bounds the
guess rate. The cost is that a flood can lock the owner out for a window; the
window is a minute, and the alternative is a limit that an attacker turns off by
editing a header.

In memory on purpose. The counters reset if the process restarts or Railway naps
— but napping needs ~6 minutes of silence, which an attacker mid-flood is not
giving it, and a 6-minute pause per window is itself the slowdown we wanted.
A volume write per failed guess would hand over a cheap way to grind the disk.
"""
from __future__ import annotations

import threading
import time
from collections import deque

# Enough that a person fumbling a paste is never told off; nowhere near enough to
# search a keyspace.
MAX_FAILURES = 10
WINDOW_SEC = 60.0
# Deliberately looser than per-IP: it exists to bound the total guess rate when
# the per-IP key is being rotated, not to police one honest user.
GLOBAL_MAX_FAILURES = 30

_GLOBAL = "*"


class RateLimiter:
    """A sliding window of recent failures per key."""

    def __init__(self, max_failures: int = MAX_FAILURES, window_sec: float = WINDOW_SEC,
                 global_max: int = GLOBAL_MAX_FAILURES) -> None:
        self._max = max_failures
        self._window = window_sec
        self._global_max = global_max
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _limit_for(self, key: str) -> int:
        return self._global_max if key == _GLOBAL else self._max

    def _live(self, key: str, now: float) -> deque[float]:
        """This key's failures inside the window, still attached to the store.

        Returning a detached deque here was a real bug and an instructive one:
        an earlier version pruned an empty key out of the dict and handed the
        orphan back, so `fail()` appended to a deque nobody could see and the
        limiter counted to zero forever. It looked like careful cleanup and was
        a silent no-op — the exact shape of a security control that reports
        success while doing nothing.
        """
        hits = self._hits.setdefault(key, deque())
        cutoff = now - self._window
        while hits and hits[0] < cutoff:
            hits.popleft()
        return hits

    def _forget_if_empty(self, key: str) -> None:
        """Drop spent keys so a spoofed header cannot grow the dict forever."""
        if key != _GLOBAL and not self._hits.get(key):
            self._hits.pop(key, None)

    def retry_after(self, key: str, *, now: float | None = None) -> float:
        """Seconds until this key may try again; 0.0 when it may try now.

        Checks the caller's key and the global one, and reports the longer wait —
        so rotating the key cannot shorten it.
        """
        now = time.time() if now is None else now
        with self._lock:
            wait = 0.0
            for candidate in (key, _GLOBAL):
                hits = self._live(candidate, now)
                if len(hits) >= self._limit_for(candidate):
                    wait = max(wait, hits[0] + self._window - now)
                self._forget_if_empty(candidate)
            return round(max(wait, 0.0), 1)

    def fail(self, key: str, *, now: float | None = None) -> None:
        """Record one wrong secret against this key and the global budget."""
        now = time.time() if now is None else now
        with self._lock:
            for candidate in (key, _GLOBAL):
                self._live(candidate, now).append(now)

    def reset(self, key: str) -> None:
        """Forget this key's failures — called when the secret was right.

        The global budget is deliberately NOT cleared: one correct login does not
        vouch for the thousand wrong ones that came from somewhere else.
        """
        with self._lock:
            self._hits.pop(key, None)


def client_key(request) -> str:
    """Best-effort caller identity for the per-IP bucket.

    `X-Forwarded-For` is read because behind Railway every connection appears to
    come from the proxy, so the peer address would put every user in one bucket.
    It is also client-controlled, which is exactly why the global bucket exists
    and why nothing security-critical rests on this value: it makes the limit
    *fairer*, not *stronger*. The left-most entry is the conventional original
    client and the one an honest proxy chain reports.
    """
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "?"
