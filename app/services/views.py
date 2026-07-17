"""Turning service objects into the payloads a caller sees.

This module exists so that "MCP and REST return identical payloads" is true by
construction rather than by discipline. Both façades import `instance_view`;
neither builds a dict of its own. Two hand-written serializers that agree today
are two serializers that disagree after the next field is added, and the drift
would show up as an agent and a dashboard disagreeing about the same browser.
"""
from __future__ import annotations

from ..models import InstanceView
from . import tokens


def instance_view(inst, *, secret: str | None = None, base_url: str = "",
                  subject: str = tokens.OWNER) -> InstanceView:
    """One running instance, with freshly minted CDP and VNC URLs.

    The tokens are minted per call and live ten minutes, so these values are
    deliberately different every time and must never be cached by a caller.
    Without a secret configured there is nothing to sign with, and a URL that
    cannot be opened is worse than none — so they are omitted rather than faked.

    `subject` is the OAuth subject the URLs are minted *for*, and it is stamped
    into both tokens; the endpoints check it against the instance's owner. It
    defaults to the one subject this deployment has (see tokens.OWNER) rather
    than to None, because every caller that reaches here has already passed the
    OAuth guard — there is no anonymous path to an instance view — and a `None`
    default would be a value to forget to pass rather than a case to handle.

    **CDP and VNC get different tokens, not one token used twice.** Watching is
    not driving: the VNC URL is built to be dropped into an `iframe src`, where
    it lands in the DOM and the browser's history, and the CDP URL grants total
    control of a browser holding the user's proxy credentials. One token for
    both would silently promote every viewer into a driver.
    """
    cdp_url = vnc_url = None
    if secret and base_url:
        ws = base_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
        cdp_token = tokens.issue(inst.id, secret, kind=tokens.CDP, subject=subject)
        cdp_url = f"{ws}/instances/{inst.id}/cdp?t={cdp_token}"

        # Only when this browser actually has a live view. An instance whose
        # display fell back to Xvfb has no framebuffer to serve, and a viewer URL
        # for it would load a page that spins forever.
        if getattr(inst, "vnc_port", None):
            vnc_token = tokens.issue(inst.id, secret, kind=tokens.VNC, subject=subject)
            # The noVNC viewer page, not the raw socket: this is meant to be
            # opened by a human, and a bare websocket renders as nothing in a
            # browser. noVNC takes the socket it should dial in `path`, so the
            # token has to survive being a query string nested inside a query
            # string — hence the encoding.
            http = base_url.rstrip("/")
            vnc_url = (
                f"{http}/novnc/vnc.html?path=instances/{inst.id}/vnc%3Ft%3D{vnc_token}"
                f"&autoconnect=true&resize=scale&reconnect=true"
            )

    return InstanceView(
        instance_id=inst.id,
        profile=inst.profile,
        origin=inst.origin,
        proxy_ip=inst.proxy_ip,
        # Passed through exactly as measured. None means "we looked and could not
        # tell", which is a fact worth reporting; a default would be a fiction.
        timezone=inst.timezone,
        locale=inst.locale,
        cdp_url=cdp_url,
        vnc_url=vnc_url,
        expires_at=inst.created_wall + inst.ttl_min * 60,
        age_sec=round(inst.age_sec(), 1),
        idle_sec=round(inst.idle_sec(), 1),
        geoip=inst.geoip,
        humanize=inst.humanize,
    )
