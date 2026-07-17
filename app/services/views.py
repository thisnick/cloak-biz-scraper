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


def instance_view(inst, *, secret: str | None = None, base_url: str = "") -> InstanceView:
    """One running instance, with a freshly minted CDP URL.

    The token is minted per call and lives ten minutes, so this value is
    deliberately different every time and must never be cached by a caller.
    Without a secret configured there is nothing to sign with, and a URL that
    cannot be opened is worse than none — so it is omitted rather than faked.
    """
    cdp_url = None
    if secret and base_url:
        token = tokens.issue(inst.id, secret)
        ws = base_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
        cdp_url = f"{ws}/instances/{inst.id}/cdp?t={token}"

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
        expires_at=inst.created_wall + inst.ttl_min * 60,
        age_sec=round(inst.age_sec(), 1),
        idle_sec=round(inst.idle_sec(), 1),
        geoip=inst.geoip,
        humanize=inst.humanize,
    )
