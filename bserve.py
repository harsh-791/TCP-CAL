"""File server for the binary protocol.

Usage: bserve ROOT PORT
Example: ./bserve ./www 9000
"""

import sys
from pathlib import Path

import bproto


def handle_request(payload, root):
    """Return (response_bytes, status, path_for_log)."""
    headers, body = bproto.parse_message(payload)
    mapping = bproto.header_map(headers)
    raw_path = mapping.get(b":path")
    method = mapping.get(b":method", b"GET")
    path_text = raw_path.decode("ascii") if raw_path is not None else "-"

    if raw_path is None or method != b"GET":
        raise bproto.Malformed("method and path")
    if b"content-length" in mapping:
        declared = int(mapping[b"content-length"].decode("ascii"))
        if declared != len(body):
            raise bproto.Malformed("content-length")
    if body:
        raise bproto.Malformed("request body")

    relative = bproto.safe_path(path_text)
    root_real = root.resolve()
    candidate = root_real.joinpath(*relative).resolve()
    if not candidate.is_relative_to(root_real):
        raise bproto.Malformed("path escapes root")
    if not candidate.is_file():
        return bproto.build_response(404), 404, path_text

    data = candidate.read_bytes()
    # Leave room for the status headers inside the 24-bit payload.
    if len(data) > bproto.MAX_PAYLOAD - 128:
        raise bproto.Malformed("file too large")
    content_type = bproto.content_type_for(candidate.suffix)
    return bproto.build_response(200, data, content_type), 200, path_text


def handle_connection(conn, root):
    while True:
        try:
            frame = bproto.read_frame(conn)
        except bproto.NeedClose:
            bproto.write_all(conn, bproto.build_response(400))
            return
        except EOFError:
            return
        if frame is None:
            return

        # A type this version does not know is skipped. Length already
        # told read_frame how many bytes to consume.
        if frame.type not in (bproto.TYPE_REQUEST, bproto.TYPE_RESPONSE):
            print(f"skip type {frame.type} ({len(frame.payload)} bytes)", file=sys.stderr)
            continue

        if frame.flags != 0 or frame.type != bproto.TYPE_REQUEST:
            bproto.write_all(conn, bproto.build_response(400))
            print("malformed frame -> 400", file=sys.stderr)
            continue

        try:
            response, status, path_text = handle_request(frame.payload, root)
        except (bproto.Malformed, UnicodeError, ValueError, OSError):
            response, status, path_text = bproto.build_response(400), 400, "-"
        bproto.write_all(conn, response)
        print(f"{path_text} -> {status}", file=sys.stderr)


def serve(root, host, port):
    listener = bproto.open_listener(host, port)
    bound = listener.getsockname()[1]
    print(f"bserve {root} on {host}:{bound}", file=sys.stderr)
    try:
        while True:
            conn, _addr = listener.accept()
            try:
                handle_connection(conn, root)
            finally:
                conn.close()
    except KeyboardInterrupt:
        return 0
    finally:
        listener.close()
    return 0


def main(argv):
    if len(argv) != 3:
        print("usage: bserve ROOT PORT", file=sys.stderr)
        return 2
    root = Path(argv[1])
    try:
        port = int(argv[2])
    except ValueError:
        print("usage: bserve ROOT PORT", file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    if not 1 <= port <= 65535:
        print("port must be 1..65535", file=sys.stderr)
        return 2
    try:
        return serve(root, "0.0.0.0", port)
    except OSError as exc:
        print(f"could not listen: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
