"""Ephemeral, instance-scoped tokens for the CDP and VNC endpoints.

CDP is **full control** of a browser holding the user's optional proxy credentials
and whatever cookies it has collected. So the token that opens it is minted by the
machine, never handled by the user, scoped to one instance, and dies in minutes.

**Why the token goes in the URL.** WebSocket clients frequently cannot set
headers — that is the whole reason this is not simply a Bearer token. A URL is a
leaky place for a credential (proxy logs, history, agent transcripts), which is
exactly why the thing we put there is not the credential that matters: this
grants "drive this one browser for ten minutes", not "do anything to this
deployment". `Authorization: Bearer` is still accepted for clients that can.

Signed with the same APP_SECRET the UI session uses, which buys revocation for
free: rotating the secret invalidates every outstanding token immediately.

**CDP and VNC are separate audiences, and that is not tidiness.** Watching a
browser and driving it are different privileges, and their tokens leak at very
different rates: a VNC URL is designed to sit in an `iframe src` on a page, where
it reaches the DOM, the referrer, and the history of whoever opens the
dashboard. If both grants were spelled `instance:<id>`, that easily-leaked
watch URL would be a perfectly valid *drive* token — a privilege escalation
delivered by copy-paste. `cdp:<id>` and `vnc:<id>` cannot be swapped for one
another, so a leaked viewer stays a viewer.

**`sub` binds a token to the OAuth subject it was minted for**, and the CDP/VNC
routes check it against the subject that owns the instance. Step 3 could not do
this — no OAuth existed, so there were no subjects, and a placeholder would have
been a check that looked like it was doing something and was not. Now the
subject is real and arrives from the access token that asked for the URL.

Be precise about what that buys *today*: this deployment has one APP_SECRET and
therefore exactly one resource owner, so in practice every token has the same
`sub` and the check cannot fire in production. It is enforced, tested, and real
— a token bearing another subject is refused — but it is defence in depth
against a future with more than one subject, not a wall standing between two
users who exist today. Claiming otherwise would be the fiction this module's
history has been careful to avoid.
"""
from __future__ import annotations

from . import signing

# Ten minutes: long enough to attach a debugger and work, short enough that a
# token found in a log later is worthless.
TTL_SEC = 10 * 60

# The one resource owner. See the docstring: with a single APP_SECRET there is
# exactly one, and pretending otherwise would overstate the subject check.
OWNER = "owner"

CDP = "cdp"
VNC = "vnc"


def _audience(kind: str, instance_id: str) -> str:
    return f"{kind}:{instance_id}"


def issue(instance_id: str, secret: str, *, kind: str = CDP, subject: str = OWNER,
          control: bool = False, ttl_sec: int = TTL_SEC, now: float | None = None) -> str:
    """A fresh token for one instance and one grant. Minted per call — never
    cached, never reused.

    `control` distinguishes the two VNC grants **within** the one `vnc:<id>`
    audience: a plain viewer token (the default) versus one that additionally
    lets its holder drive the browser over RFB — "Take control". Keeping them the
    same audience, separated by a signed claim, is deliberate: a control token is
    still not a CDP token and still cannot open a browser it was not minted for,
    and a *leaked viewer* token — the one that could end up in a DOM or history —
    has no `ctl` claim and stays a viewer, because the claim is inside the MAC and
    cannot be added without the secret. The dashboard mints a viewer token for
    every pane at rest and a control token only when the user explicitly asks.
    """
    claims = {"aud": _audience(kind, instance_id), "sub": subject}
    if control:
        claims["ctl"] = 1
    return signing.issue(claims, secret, ttl_sec=ttl_sec, now=now)


def verify(token: str | None, instance_id: str, secret: str | None, *, kind: str = CDP,
           subject: str | None = OWNER, now: float | None = None) -> bool:
    """True only for a live token minted for *this* instance, grant, and subject.

    The audience check is what stops a token for a browser the caller is allowed
    to drive from opening one they are not — and what stops a viewer's token
    from driving anything at all.

    `subject=None` means "any subject", which exists for the instance whose
    owner was never recorded (one launched before this field existed, or by an
    internal caller with no OAuth context). It is a deliberate hole and the
    callers that pass it say why.
    """
    if not instance_id:
        return False
    claims = signing.verify(token, secret, audience=_audience(kind, instance_id), now=now)
    if claims is None:
        return False
    if subject is not None and claims.get("sub") != subject:
        return False
    return True


def grants_control(token: str | None, instance_id: str, secret: str | None, *,
                   subject: str | None = OWNER, now: float | None = None) -> bool:
    """True only for a live VNC token that also carries the `control` grant.

    Read *after* `verify` has already accepted the token: this answers the
    second, narrower question — "may this viewer also drive?" — and it re-derives
    the answer from the signed bytes rather than trusting anything the client
    said. A viewer token, a CDP token, a token for another instance or subject,
    or any forgery all answer False, so the VNC proxy falls back to view-only,
    which is the safe default for a framebuffer of the user's logged-in browser.
    """
    if not instance_id:
        return False
    claims = signing.verify(token, secret, audience=_audience(VNC, instance_id), now=now)
    if claims is None:
        return False
    if subject is not None and claims.get("sub") != subject:
        return False
    return bool(claims.get("ctl"))
