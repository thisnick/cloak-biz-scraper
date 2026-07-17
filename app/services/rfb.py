"""RFB (VNC) message translation between generic noVNC and KasmVNC 1.3.3.

Ported from browserd (app/rfb.py), which vendored it from CloakBrowser-Manager
(MIT). noVNC v1.4 batches several RFB messages into one WebSocket frame and
sends extension types KasmVNC 1.3.3 rejects, so we parse the message boundaries,
keep only standard types, rewrite PointerEvents into KasmVNC's 11-byte form, and
whitelist encodings.

**`view_only` is the part that is new here, and it is a security control rather
than a preference.** "Watch the sweep run" sounds read-only and is not: RFB is a
remote *desktop* protocol, so a viewer's pointer and key events are clicks and
keystrokes in the browser. Attaching a plain viewer to a running sweep would let
whoever opened the dashboard type into it mid-navigation — the same corruption
CDP is refused for. Dropping types 4, 5 and 6 on the way in is what makes
view-only a property of the bytes that reach the browser, rather than a promise
about how gently the viewer intends to move their mouse.
"""
from __future__ import annotations

import logging
import struct

logger = logging.getLogger("cloakbiz.rfb")


def parse_kasmvnc_clipboard(data: bytes) -> str | None:
    """Extract text/plain from KasmVNC BinaryClipboard (type 180)."""
    if len(data) < 7:
        return None
    offset = 6
    while offset < len(data):
        if offset + 1 > len(data):
            break
        mime_len = data[offset]
        offset += 1
        if offset + mime_len > len(data):
            break
        mime_type = data[offset:offset + mime_len]
        offset += mime_len
        if offset + 4 > len(data):
            break
        data_len = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        if mime_type == b"text/plain":
            end = min(offset + data_len, len(data))
            return data[offset:end].decode("utf-8", errors="replace")
        offset += data_len
    return None


def build_server_cut_text(text: str) -> bytes:
    """Build standard RFB ServerCutText (type 3); RFB mandates Latin-1."""
    text_bytes = text.encode("latin-1", errors="replace")
    return struct.pack(">BxxxI", 3, len(text_bytes)) + text_bytes


RFB_MSG_SIZE: dict[int, int | None] = {
    0: 20,    # SetPixelFormat
    2: None,  # SetEncodings — 4 + numEncodings*4
    3: 10,    # FramebufferUpdateRequest
    4: 8,     # KeyEvent
    5: 6,     # PointerEvent
    6: None,  # ClientCutText — 8 + length
}

# What a viewer sends to *change* something rather than to see it: keystrokes,
# mouse, and clipboard pastes. Dropped for a browser that belongs to a sweep.
_INPUT_MSG_TYPES = frozenset({4, 5, 6})

_RFB_EXTENSION_SIZE: dict[int, int] = {150: 10, 248: 10, 252: 4, 255: 4}

_ALLOWED_ENCODINGS: set[int] = {
    0, 1, 2, 5, 7, 16, -239, -224,
    *range(-32, -22),
    *range(-256, -246),
}


def _rfb_msg_length(data: bytes, offset: int) -> int | None:
    if offset >= len(data):
        return None
    msg_type = data[offset]
    fixed = RFB_MSG_SIZE.get(msg_type)
    if fixed is not None:
        return fixed
    remaining = len(data) - offset
    if msg_type == 2 and remaining >= 4:
        num_enc = struct.unpack_from(">H", data, offset + 2)[0]
        return 4 + num_enc * 4
    if msg_type == 6 and remaining >= 8:
        length = struct.unpack_from(">I", data, offset + 4)[0]
        return 8 + length
    ext_size = _RFB_EXTENSION_SIZE.get(msg_type)
    if ext_size is not None:
        return ext_size
    return None


def _rewrite_set_encodings(data: bytes, offset: int, msg_len: int) -> bytes:
    num_enc = struct.unpack_from(">H", data, offset + 2)[0]
    kept, stripped = [], []
    for i in range(num_enc):
        enc = struct.unpack_from(">i", data, offset + 4 + i * 4)[0]
        (kept if enc in _ALLOWED_ENCODINGS else stripped).append(enc)
    if not stripped:
        return data[offset:offset + msg_len]
    result = struct.pack(">BxH", 2, len(kept))
    for enc in kept:
        result += struct.pack(">i", enc)
    return result


def _rewrite_pointer_event(data: bytes, offset: int) -> bytes:
    mask = data[offset + 1]
    x = struct.unpack_from(">H", data, offset + 2)[0]
    y = struct.unpack_from(">H", data, offset + 4)[0]
    return struct.pack(">BHHHhh", 5, mask, x, y, 0, 0)


def filter_client_messages(data: bytes, *, view_only: bool = False) -> bytes:
    """Keep only standard RFB types (0-6), rewriting for KasmVNC compatibility.

    Boundaries are always parsed, even for messages that are then dropped:
    skipping a message means knowing its length, and a view-only filter that
    guessed would desynchronise the stream and pass the *rest* of the frame
    through as garbage — which is how a "read-only" viewer sends a click.
    """
    result = bytearray()
    offset = 0
    while offset < len(data):
        msg_type = data[offset]
        msg_len = _rfb_msg_length(data, offset)
        if msg_len is None or offset + msg_len > len(data):
            break
        if msg_type in RFB_MSG_SIZE and not (view_only and msg_type in _INPUT_MSG_TYPES):
            if msg_type == 2:
                result.extend(_rewrite_set_encodings(data, offset, msg_len))
            elif msg_type == 5:
                result.extend(_rewrite_pointer_event(data, offset))
            else:
                result.extend(data[offset:offset + msg_len])
        offset += msg_len
    return bytes(result)
