"""HTTP/1.1 calculator.

Listens on port 8080. One TCP connection can carry many requests.
A request ends at the blank line, plus exactly Content-Length body
bytes when that header is present. The socket stays open afterwards.
"""

import re
import socket
import sys
import urllib.parse

MAX_HEADERS = 65536
MAX_BODY = 65536
OPERATIONS = ("/add", "/sub", "/mul", "/div")


class CloseConnection(Exception):
    """The request boundary is not trustworthy. Reply 400 and close."""


def write_all(sock, data):
    """Send every byte. One send() may write only part of the buffer."""
    view = memoryview(data)
    while view:
        sent = sock.send(view)
        view = view[sent:]


def build_response(status, body, close):
    if isinstance(body, str):
        body = body.encode("ascii")
    reason = {
        200: "OK",
        400: "Bad Request",
        404: "Not Found",
        405: "Method Not Allowed",
    }[status]
    lines = [
        f"HTTP/1.1 {status} {reason}",
        f"Content-Length: {len(body)}",
        "Content-Type: text/plain; charset=utf-8",
    ]
    if status == 405:
        lines.append("Allow: GET")
    if close:
        lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


def connection_tokens(headers):
    raw = headers.get("connection", "")
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def peer_wants_close(version, headers):
    tokens = connection_tokens(headers)
    if "close" in tokens:
        return True
    # HTTP/1.0 closes unless the client asked to keep the socket open.
    if version == "HTTP/1.0" and "keep-alive" not in tokens:
        return True
    return False


def header_block(buf):
    """Split one header block off the front of buf.

    Returns (header_bytes, rest) or None if the blank line has not
    arrived yet. The body is not inspected here.
    """
    marker = buf.find(b"\r\n\r\n")
    if marker < 0:
        return None
    return buf[:marker], buf[marker + 4 :]


def content_length_of(headers):
    """How many body bytes belong to this request.

    No Content-Length means an empty body. A bad value means we cannot
    tell where the next request starts, so the connection must close.
    """
    if "transfer-encoding" in headers:
        raise CloseConnection("chunked bodies are not supported")
    if "content-length" not in headers:
        return 0
    raw = headers["content-length"]
    if not re.fullmatch(r"\d+", raw):
        raise CloseConnection("bad content-length")
    length = int(raw)
    if length > MAX_BODY:
        raise CloseConnection("body too large")
    return length


def inspect_headers(head):
    """Return (request, headers, length).

    request is (method, target, version), or None when the request line
    is nonsense. length is still returned in that case, so the body can
    be removed and the next request kept intact. CloseConnection means
    the body boundary itself is unknown.
    """
    lines = head.decode("iso-8859-1").split("\r\n")
    if not lines or lines == [""]:
        raise CloseConnection("missing header")
    headers = {}
    for line in lines[1:]:
        if ":" not in line:
            raise CloseConnection("bad header")
        name, value = line.split(":", 1)
        name = name.strip().lower()
        value = value.strip()
        if not name or re.search(r"\s", name):
            raise CloseConnection("bad header name")
        # Two Content-Length values can disagree. Then no safe boundary exists.
        if name in headers and name in ("content-length", "transfer-encoding"):
            raise CloseConnection("duplicate framing header")
        if name in headers and name == "host":
            raise CloseConnection("duplicate host")
        headers[name] = value
    length = content_length_of(headers)
    parts = lines[0].split(" ")
    if len(parts) != 3:
        return None, headers, length
    method, target, version = parts
    if version not in ("HTTP/1.0", "HTTP/1.1") or method == "":
        return None, headers, length
    return (method, target, version), headers, length


def parse_integer(text):
    if not re.fullmatch(r"-?\d+", text):
        return None
    return int(text)


def calculate(method, target, version, headers):
    """Return (status, body, close_after). The body is plain text."""
    close = peer_wants_close(version, headers)
    if method != "GET":
        return 405, "method not allowed", close
    if version == "HTTP/1.1" and not headers.get("host"):
        return 400, "host required", close

    parsed = urllib.parse.urlsplit(target)
    if parsed.path not in OPERATIONS:
        return 404, "not found", close

    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    a_values = query.get("a", [])
    b_values = query.get("b", [])
    if len(a_values) != 1 or len(b_values) != 1:
        return 400, "bad request", close
    left = parse_integer(a_values[0])
    right = parse_integer(b_values[0])
    if left is None or right is None:
        return 400, "bad request", close
    if parsed.path == "/div" and right == 0:
        return 400, "bad request", close

    if parsed.path == "/add":
        result = left + right
    elif parsed.path == "/sub":
        result = left - right
    elif parsed.path == "/mul":
        result = left * right
    else:
        # Integer division. 9/3 is 3. Toward negative infinity, as in Python.
        result = left // right
    return 200, str(result), close


def handle_connection(conn):
    """Read requests until the peer closes, or until we must close.

    Leftover bytes stay in `buf`. They are the start of the next request,
    which is why the end of a request is never "the socket closed".
    """
    buf = b""
    while True:
        while header_block(buf) is None:
            if len(buf) > MAX_HEADERS:
                write_all(conn, build_response(400, "headers too large", True))
                return
            chunk = conn.recv(4096)
            if not chunk:
                # Empty buf: the client closed between requests. That is normal.
                # A partial header means the request never finished.
                return
            buf += chunk

        head, buf = header_block(buf)
        try:
            request, headers, length = inspect_headers(head)
        except CloseConnection:
            write_all(conn, build_response(400, "bad request", True))
            return

        while len(buf) < length:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
        # Drop exactly `length` body bytes, even if this request is rejected.
        # Whatever remains in `buf` is the next request, not part of this one.
        buf = buf[length:]

        if request is None:
            close = "close" in connection_tokens(headers)
            write_all(conn, build_response(400, "bad request", close))
            print("bad request line -> 400", file=sys.stderr)
            if close:
                return
            continue

        method, target, version = request
        status, message, close = calculate(method, target, version, headers)
        write_all(conn, build_response(status, message, close))
        print(f"{method} {target} -> {status}", file=sys.stderr)
        if close:
            return


def serve(host, port):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    listener.listen(16)
    bound = listener.getsockname()[1]
    print(f"calculator listening on {host}:{bound}", file=sys.stderr)
    try:
        while True:
            conn, _addr = listener.accept()
            try:
                handle_connection(conn)
            finally:
                conn.close()
    except KeyboardInterrupt:
        return 0
    finally:
        listener.close()
    return 0


def main(argv):
    port = 8080
    if len(argv) == 2:
        try:
            port = int(argv[1])
        except ValueError:
            print("usage: httpcalc.py [PORT]", file=sys.stderr)
            return 2
    elif len(argv) != 1:
        print("usage: httpcalc.py [PORT]", file=sys.stderr)
        return 2
    if not 1 <= port <= 65535:
        print("port must be 1..65535", file=sys.stderr)
        return 2
    try:
        return serve("0.0.0.0", port)
    except OSError as exc:
        print(f"could not listen: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
