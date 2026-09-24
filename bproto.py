"""Binary file protocol shared by bserve and bcurl.

A frame is 8 header bytes plus Length payload bytes. Length is how the
receiver finds the next frame. Unknown types are skipped by that length.
See protocol-spec.md for the same layout.
"""

import re
import socket

MAGIC = b"\xc1\xa1"
VERSION = 1
TYPE_REQUEST = 1
TYPE_RESPONSE = 2
HEADER_SIZE = 8
MAX_PAYLOAD = (1 << 24) - 1

# Static header names. ID 0 means "the name is written out in the frame".
ID_METHOD = 1
ID_PATH = 2
ID_STATUS = 3
ID_HOST = 4
ID_CONTENT_LENGTH = 5
ID_CONTENT_TYPE = 6

NAMES = {
    ID_METHOD: b":method",
    ID_PATH: b":path",
    ID_STATUS: b":status",
    ID_HOST: b"host",
    ID_CONTENT_LENGTH: b"content-length",
    ID_CONTENT_TYPE: b"content-type",
}

CONTENT_TYPES = {
    ".html": "text/html",
    ".txt": "text/plain",
    ".css": "text/css",
    ".js": "text/javascript",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


class NeedClose(Exception):
    """Magic or version is wrong, so Length cannot be trusted."""


class Malformed(Exception):
    """The frame was fully read, but the payload is not a valid message."""


class Frame:
    def __init__(self, ftype, flags, payload):
        self.type = ftype
        self.flags = flags
        self.payload = payload

    @property
    def raw(self):
        return encode_frame(self.type, self.payload, self.flags)


def write_all(sock, data):
    view = memoryview(data)
    while view:
        sent = sock.send(view)
        view = view[sent:]


def read_exact(sock, n):
    """Read exactly n bytes.

    None means the peer closed before this read started.
    EOFError means it closed after we had already started the frame.
    """
    if n == 0:
        return b""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            if buf == b"":
                return None
            raise EOFError("connection closed mid-frame")
        buf += chunk
    return buf


def encode_frame(ftype, payload, flags=0):
    if not 0 <= ftype <= 255 or not 0 <= flags <= 255:
        raise ValueError("type and flags must fit in one byte")
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload exceeds 24-bit length")
    return (
        MAGIC
        + bytes([VERSION, ftype, flags])
        + len(payload).to_bytes(3, "big")
        + payload
    )


def parse_header(header):
    if len(header) != HEADER_SIZE:
        raise Malformed("header must be 8 bytes")
    if header[:2] != MAGIC or header[2] != VERSION:
        raise NeedClose("unrecognised magic or version")
    ftype = header[3]
    flags = header[4]
    length = int.from_bytes(header[5:8], "big")
    return ftype, flags, length


def read_frame(sock):
    """Read one frame, or None if the peer closed cleanly beforehand."""
    header = read_exact(sock, HEADER_SIZE)
    if header is None:
        return None
    ftype, flags, length = parse_header(header)
    payload = read_exact(sock, length)
    if payload is None:
        raise EOFError("connection closed mid-frame")
    return Frame(ftype, flags, payload)


def encode_header(name_id, value, literal_name=None):
    if isinstance(value, str):
        value = value.encode("ascii")
    if len(value) > 65535:
        raise ValueError("header value too long")
    if name_id == 0:
        if isinstance(literal_name, str):
            literal_name = literal_name.encode("ascii")
        if not literal_name or len(literal_name) > 255:
            raise ValueError("literal header name must be 1..255 bytes")
        return (
            bytes([0, len(literal_name)])
            + literal_name
            + len(value).to_bytes(2, "big")
            + value
        )
    if name_id not in NAMES:
        raise ValueError("unknown header id")
    return bytes([name_id]) + len(value).to_bytes(2, "big") + value


def encode_payload(headers, body=b""):
    """headers is a list of (name_id, value, literal_name or None)."""
    if len(headers) > 255:
        raise ValueError("too many headers")
    out = bytearray([len(headers)])
    for name_id, value, literal_name in headers:
        out += encode_header(name_id, value, literal_name)
    out += body
    return bytes(out)


def parse_message(payload):
    """Return (headers, body). headers is a list of (name_bytes, value_bytes)."""
    if not payload:
        raise Malformed("empty payload")
    count = payload[0]
    offset = 1
    headers = []
    for _ in range(count):
        name, value, offset = _read_header(payload, offset)
        headers.append((name, value))
    return headers, payload[offset:]


def _read_header(buf, offset):
    if offset >= len(buf):
        raise Malformed("truncated header")
    name_id = buf[offset]
    offset += 1
    if name_id == 0:
        if offset >= len(buf):
            raise Malformed("truncated literal name")
        name_len = buf[offset]
        offset += 1
        if name_len == 0 or offset + name_len > len(buf):
            raise Malformed("bad literal name")
        name = buf[offset : offset + name_len]
        offset += name_len
    elif name_id in NAMES:
        name = NAMES[name_id]
    else:
        raise Malformed("unknown header id")
    if offset + 2 > len(buf):
        raise Malformed("truncated value length")
    value_len = int.from_bytes(buf[offset : offset + 2], "big")
    offset += 2
    if offset + value_len > len(buf):
        raise Malformed("truncated value")
    value = buf[offset : offset + value_len]
    offset += value_len
    return name, value, offset


def header_map(headers):
    """First value wins. A repeated control header is malformed."""
    found = {}
    single = {b":method", b":path", b":status", b"content-length", b"content-type"}
    for name, value in headers:
        if name in found and name in single:
            raise Malformed("duplicate header")
        if name not in found:
            found[name] = value
    return found


def build_request(host, port, path):
    headers = [
        (ID_METHOD, "GET", None),
        (ID_PATH, path, None),
        (ID_HOST, f"{host}:{port}", None),
        (0, "bcurl", "user-agent"),
    ]
    return encode_frame(TYPE_REQUEST, encode_payload(headers, b""))


def build_response(status, body=b"", content_type=None):
    if status == 200:
        if content_type is None:
            content_type = "application/octet-stream"
        headers = [
            (ID_STATUS, "200", None),
            (ID_CONTENT_LENGTH, str(len(body)), None),
            (ID_CONTENT_TYPE, content_type, None),
        ]
    else:
        body = b""
        headers = [
            (ID_STATUS, str(status), None),
            (ID_CONTENT_LENGTH, "0", None),
        ]
    return encode_frame(TYPE_RESPONSE, encode_payload(headers, body))


def content_type_for(suffix):
    return CONTENT_TYPES.get(suffix.lower(), "application/octet-stream")


def safe_path(path):
    """Return the relative segments, or raise Malformed.

    The path must stay a single relative file name under the web root.
    """
    if not path.startswith("/"):
        raise Malformed("path must start with /")
    parts = path.split("/")
    relative = []
    for part in parts[1:]:
        if part in ("", ".", ".."):
            raise Malformed("illegal path")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", part):
            raise Malformed("illegal path")
        relative.append(part)
    if not relative:
        raise Malformed("empty path")
    return relative


def hexdump(data):
    """16 bytes per line: offset, hex, nothing else. Used by bcurl -v."""
    lines = []
    for offset in range(0, len(data), 16):
        chunk = data[offset : offset + 16]
        hex_part = " ".join(f"{byte:02x}" for byte in chunk)
        lines.append(f"{offset:04x}  {hex_part}")
    return "\n".join(lines)


def open_listener(host, port):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    listener.listen(16)
    return listener
