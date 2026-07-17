"""What must be true before a socket onto a live browser is accepted.

Shared by the CDP and VNC endpoints because they are the same decision with one
difference, and writing it twice is how the copy that gets a fix and the copy
that does not come to exist. Both endpoints hand out control of a browser
holding the user's residential proxy credentials and cookies; both are refused
**before** `accept()`, so nothing is ever attached to the browser by a caller
who turns out not to be allowed.

The one difference is what a task-owned browser may do:

* **CDP: refused outright.** A sweep's browser is mid-navigation on a schedule
  of its own. A debugger attached to it corrupts the sweep and confuses whoever
  attached. This is a Step 3 property and it does not regress.
* **VNC: allowed, but view-only.** Watching a sweep run is the inspection
  feature the product is partly sold on, and watching cannot corrupt anything —
  *provided* it is actually watching. It is not by default: a VNC viewer sends
  pointer and key events, so an unfiltered "viewer" on a running sweep is
  someone clicking in it. services/rfb.py drops those for task browsers, which
  is what makes "view-only" a fact about the bytes rather than a promise about
  the reader's intentions.
"""
from __future__ import annotations

from ..services import tokens
from .mcp import origin_allowed


class Denied(Exception):
    """Close codes are the only thing a WS client can read on a refused upgrade."""

    def __init__(self, code: int, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(reason)


def bearer(headers) -> str | None:
    auth = headers.get("authorization") or ""
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


def authorize(app, headers, query_token: str | None, instance_id: str, *, kind: str):
    """The instance this caller may open, or Denied.

    The token may arrive in the query string or as a Bearer header: WS clients
    frequently cannot set headers, which is the whole reason the URL carries one
    — but a client that can should not be forced into the leakier option.
    """
    if not origin_allowed(headers):
        raise Denied(4403, "origin not allowed")

    secret = app.state.secret.current()
    token = query_token or bearer(headers)

    inst = app.state.instances.get(instance_id)
    # The subject that owns this browser. `or OWNER` rather than `or None`: None
    # means "any subject" to tokens.verify, and an instance launched without a
    # recorded owner should fall back to the strictest reading, not the loosest.
    # An unknown instance is checked against OWNER too, so that a bad token and a
    # missing instance take the same path and cannot be told apart by timing.
    owner = getattr(inst, "owner", None) or tokens.OWNER

    if not tokens.verify(token, instance_id, secret, kind=kind, subject=owner):
        # Deliberately one message for missing, expired, forged, wrong-instance,
        # wrong-grant, and wrong-subject. Which one it was is only useful to
        # someone who should not have been here.
        raise Denied(4401, "invalid or expired token")

    if inst is None:
        raise Denied(4004, "no such instance")
    if kind == tokens.CDP and inst.origin == "task":
        raise Denied(4003, "this browser belongs to a running sweep")
    return inst
