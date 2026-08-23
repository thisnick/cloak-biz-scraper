"""Turning service objects into the payloads a caller sees.

This module exists so that "MCP and REST return identical payloads" is true by
construction rather than by discipline. Both façades import `instance_view`;
neither builds a dict of its own. Two hand-written serializers that agree today
are two serializers that disagree after the next field is added, and the drift
would show up as an agent and a dashboard disagreeing about the same browser.
"""
from __future__ import annotations

from ..models import (
    BrowserInfo,
    InstanceView,
    NotionInfo,
    PoolInfo,
    ProxyInfo,
    ServerInfo,
    UploadTicket,
)
from . import tokens, uploads


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


def upload_ticket(ticket, *, base_url: str = "") -> UploadTicket:
    """One staging slot, as both façades hand it to a caller.

    Here rather than at either call site for the reason this module exists: the
    tool and `POST /api/uploads` must describe the same ticket in the same
    words, and two hand-written dicts that agree today disagree after the next
    field is added.

    **The caps come from the store's own constants, never from a literal here.**
    A tool description that overstates what the server will accept is a bug with
    a very long feedback loop — the model believes it for the rest of the
    session and keeps posting files that keep being refused. Reading them from
    `services/uploads` is what makes "what a model is told" and "what is
    enforced" the same number by construction.

    `curl` is pre-baked and copy-pasteable because models follow a whole command
    far more reliably than they assemble one from parts, and because the one
    thing that must not be got wrong — the token in a header rather than the URL
    — is then not something the caller has to know.

    `expires_in` is the full TTL rather than a fresh subtraction: the ticket was
    minted for this very response, so the two are the same number, and deriving
    it from the wall clock would make an otherwise pure view non-deterministic.
    `expires_at` carries the absolute answer for anyone who needs to reason
    about it later.
    """
    url = f"{base_url}/uploads/{ticket.handle}"
    return UploadTicket(
        handle=ticket.handle,
        upload_url=url,
        token=ticket.token,
        curl=(
            f"curl -H 'Authorization: Bearer {ticket.token}' "
            f"-F 'file=@photo.jpg' {url}"
        ),
        expires_at=ticket.expires_at,
        expires_in=uploads.TTL_SEC,
        max_files=uploads.MAX_FILES_PER_TICKET,
        max_bytes_per_file=uploads.MAX_BYTES_PER_FILE,
        accepts=list(uploads.ACCEPTS),
    )


def server_info(settings, instances) -> ServerInfo:
    """A secret-free status snapshot from the same sources the settings chips use.

    Built here so MCP and REST return the identical payload, and so the "no
    secret ever leaves" property is enforced in one place: this reads only the
    booleans, statuses, versions, and counts — never proxy_password,
    cloakbrowser_license_key, or notion_api_token.
    """
    from .capacity import detect_capacity

    counts = instances.counts()
    # Read the container's real ceiling so an agent/UI can see the safe pool
    # size next to the configured one. None off a Linux container — omitted, not
    # faked. No secret: a byte count and a browser count only.
    recommended_max = detect_capacity().recommended_max_browsers()

    proxy_configured = settings.proxy_configured()
    return ServerInfo(
        proxy=ProxyInfo(
            configured=proxy_configured,
            status=settings.proxy_status(),
            # Defaults such as US/california are targeting choices, not an
            # observed direct-egress location. Never present them as active
            # when there is no complete proxy configuration.
            country=(settings.proxy_country or None) if proxy_configured else None,
            region=(settings.proxy_region or None) if proxy_configured else None,
        ),
        browser=browser_info(settings, instances),
        pool=PoolInfo(
            max=counts["max"],
            reserved=counts["reserve"],
            in_use=counts["total"],
            recommended_max=recommended_max,
        ),
        notion=NotionInfo(connected=settings.notion_configured()),
    )


def browser_info(settings, instances) -> BrowserInfo:
    """The selected build without confusing a saved key with a Pro artifact.

    `cloakbrowser.binary_info()` prefers any cached Pro binary, even when the
    current settings deliberately select the public build, so it cannot answer
    this question on a volume that has used both modes. The manager remembers
    the exact path returned for the current key fingerprint + pin — in memory,
    and on the volume so a restart does not un-verify a working install —
    and `is_pro(path)` is then the ground truth. Before a keyed build resolves
    (or after its cached binary goes away), status stays `pro-unverified` rather
    than claiming that the key worked.
    """
    from .license import _version_from_path, is_pro

    path = None
    get_path = getattr(instances, "binary_path_for", None)
    if callable(get_path):
        path = get_path(settings)

    if path:
        pro = is_pro(path)
        build = "pro" if pro else "public"
        version = _version_from_path(path)
    elif settings.cloakbrowser_license_key:
        pro = None
        build = "pro-unverified"
        version = settings.cloakbrowser_version or "latest"
    else:
        # Blank key is itself the deliberate public selection. There is no
        # licensing-server outcome that can turn it into Pro.
        pro = False
        build = "public"
        version = settings.cloakbrowser_version or "latest"

    return BrowserInfo(
        build=build,
        pro=pro,
        version=version,
        windows_fonts="not bundled",
    )
