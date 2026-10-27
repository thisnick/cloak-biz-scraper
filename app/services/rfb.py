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

**The clipboard is translated both ways, and carried as UTF-8.** KasmVNC 1.3.3
speaks only its own BinaryClipboard (type 180): it ignores a standard
ClientCutText (measured — a paste never reached the browser), and announces a
copy with a 180 that noVNC cannot read. So the proxy turns ClientCutText into
180 on the way in and 180 into ServerCutText on the way out. Standard RFB says
cut text is Latin-1, which would turn every ’, — and 中 on a listing page into
`?`; both directions here carry UTF-8 instead, and the dashboard's pane encodes
and decodes it. That is safe because this proxy's only drivable client is that
pane (the in-chat viewer is view-only, and noVNC drops cut text when viewOnly).
"""
from __future__ import annotations

import logging
import struct

logger = logging.getLogger("cloakbiz.rfb")


def parse_kasmvnc_clipboard(data: bytes) -> str | None:
    """Extract text/plain from a server's KasmVNC BinaryClipboard (type 180).

    Layout: type, item count, then per item a 4-byte clip id, a 1-byte mime
    length, the mime, a 4-byte data length and the data. A copy in Chromium
    usually carries text/html alongside text/plain, in either order.
    """
    if len(data) < 2:
        return None
    offset = 2
    for _ in range(data[1]):
        if offset + 5 > len(data):
            break
        offset += 4  # clip id
        mime_len = data[offset]
        offset += 1
        mime_type = data[offset:offset + mime_len]
        offset += mime_len
        if offset + 4 > len(data):
            break
        data_len = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        if mime_type == b"text/plain":
            text = data[offset:offset + data_len].decode("utf-8", errors="replace")
            return text[:-1] if text.endswith("\0") else text
        offset += data_len
    return None


def kasmvnc_clipboard_size(data: bytes) -> int | None:
    """Length of the server BinaryClipboard at the start of `data`, or None
    while part of it has yet to arrive."""
    if len(data) < 2:
        return None
    offset = 2
    for _ in range(data[1]):
        if offset + 5 > len(data):
            return None
        offset += 5 + data[offset + 4]  # clip id, mime length, mime
        if offset + 4 > len(data):
            return None
        offset += 4 + struct.unpack_from(">I", data, offset)[0]
    return offset if offset <= len(data) else None


class ServerClipboard:
    """Turns the server's BinaryClipboard into ServerCutText, across frames.

    A copy of any size (220 KB measured) reaches the proxy in several WebSocket
    frames. Forwarding the later ones untranslated put clipboard text where
    noVNC expected a message type, and it dropped the connection ("Unexpected
    server message"). So a 180 is held until complete; everything else passes.
    """

    def __init__(self) -> None:
        self._pending: bytearray | None = None

    def feed(self, frame: bytes) -> list[bytes]:
        """Take one frame from the server; return the frames for the viewer."""
        if self._pending is None:
            if not frame or frame[0] != 180:
                return [frame]
            self._pending = bytearray(frame)
        else:
            self._pending.extend(frame)
        size = kasmvnc_clipboard_size(self._pending)
        if size is None:
            return []
        message, rest = bytes(self._pending[:size]), bytes(self._pending[size:])
        self._pending = None
        out = []
        text = parse_kasmvnc_clipboard(message)
        if text:
            out.append(build_server_cut_text(text))
        if rest:
            out.extend(self.feed(rest))
        return out


def build_server_cut_text(text: str) -> bytes:
    """Build ServerCutText (type 3) for the dashboard pane — UTF-8, not the
    standard's Latin-1 (see the module docstring)."""
    text_bytes = text.encode("utf-8")
    return struct.pack(">BxxxI", 3, len(text_bytes)) + text_bytes


def build_kasmvnc_clipboard(text: str) -> bytes:
    """Build a client's KasmVNC BinaryClipboard (type 180) carrying text/plain.

    The client-to-server form has no clip ids: type, item count, then per item
    a 1-byte mime length, the mime, a 4-byte data length and the data.
    """
    mime = b"text/plain"
    data = text.encode("utf-8")
    return struct.pack(">BBB", 180, 1, len(mime)) + mime + struct.pack(">I", len(data)) + data


def _cut_text_to_kasmvnc(payload: bytes) -> bytes:
    """ClientCutText's payload as the 180 KasmVNC will actually take.

    The pane sends UTF-8; a standard client would send Latin-1, which is kept
    readable rather than dropped when it is not valid UTF-8."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        text = payload.decode("latin-1")
    return build_kasmvnc_clipboard(text)


# The longest paste passed on. noVNC sends one ClientCutText in 10 KiB frames,
# so a paste of any size spans several and has to be held until complete; past
# this it is skipped instead, so a viewer cannot make the proxy buffer without
# bound.
MAX_CUT_TEXT = 1 << 20

# The largest frame sent on to KasmVNC: noVNC's own send-buffer size, which is
# what KasmVNC is used to receiving.
MAX_UPSTREAM_FRAME = 10 * 1024


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


class ClientStream:
    """One viewer's client-to-server bytes, filtered message by message.

    Keep only standard RFB types (0-6), rewriting for KasmVNC compatibility.
    Stateful because a message can span WebSocket frames: noVNC flushes its send
    buffer every 10 KiB, so a long paste arrives in pieces. Filtering each frame
    on its own dropped the start of the paste and then parsed the rest of it as
    fresh messages — clipboard text read as keystrokes and clicks.

    Boundaries are always parsed, even for messages that are then dropped:
    skipping a message means knowing its length, and a view-only filter that
    guessed would desynchronise the stream and pass the *rest* of it through as
    garbage — which is how a "read-only" viewer sends a click.
    """

    def __init__(self, *, view_only: bool = False) -> None:
        self.view_only = view_only
        self._buf = bytearray()
        self._skip = 0  # bytes still to come of a paste over MAX_CUT_TEXT

    def feed(self, data: bytes) -> bytes:
        """Take one frame; return what may go upstream now."""
        if self._skip:
            skipped = min(self._skip, len(data))
            self._skip -= skipped
            data = data[skipped:]
        buf = self._buf
        buf.extend(data)
        result = bytearray()
        offset = 0
        while offset < len(buf):
            msg_type = buf[offset]
            if msg_type == 6 and len(buf) - offset >= 8:
                length = struct.unpack_from(">I", buf, offset + 4)[0]
                if length > MAX_CUT_TEXT:
                    # Also catches noVNC's extended-clipboard form (a negative
                    # length), which KasmVNC never negotiates here.
                    self._skip = length - (len(buf) - offset - 8)
                    if self._skip < 0:
                        offset += 8 + length
                        self._skip = 0
                        continue
                    offset = len(buf)
                    break
            msg_len = _rfb_msg_length(buf, offset)
            if msg_len is None:
                if msg_type in (2, 6):
                    break  # its length is in a header that has not all arrived
                offset = len(buf)  # an unknown type: nothing after it can be trusted
                break
            if offset + msg_len > len(buf):
                break  # the rest of this message is in a later frame
            if msg_type in RFB_MSG_SIZE and not (self.view_only and msg_type in _INPUT_MSG_TYPES):
                if msg_type == 2:
                    result.extend(_rewrite_set_encodings(buf, offset, msg_len))
                elif msg_type == 5:
                    result.extend(_rewrite_pointer_event(buf, offset))
                elif msg_type == 6:
                    result.extend(_cut_text_to_kasmvnc(bytes(buf[offset + 8:offset + msg_len])))
                else:
                    result.extend(buf[offset:offset + msg_len])
            offset += msg_len
        del buf[:offset]
        return bytes(result)


def filter_client_messages(data: bytes, *, view_only: bool = False) -> bytes:
    """Filter one self-contained run of messages; a message cut off at the end
    is dropped. The proxy uses a ClientStream, which holds it for the next frame."""
    return ClientStream(view_only=view_only).feed(data)
