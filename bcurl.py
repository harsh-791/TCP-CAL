"""Client for the binary protocol.

Usage: bcurl [-v] host:port/path
Example: ./bcurl -v localhost:9000/index.html

The file bytes go to stdout. -v prints every frame, sent and received,
to stderr. One process opens one TCP connection.
"""

import re
import socket
import sys

import bproto


def parse_target(text):
    match = re.fullmatch(r"(?:http://)?([^/:]+):(\d+)(/[^?#]*)", text)
    if not match:
        raise ValueError("want host:port/path")
    host, port_text, path = match.group(1), match.group(2), match.group(3)
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError("port must be 1..65535")
    if not path.startswith("/"):
        raise ValueError("path must start with /")
    return host, port, path


def status_of(headers, body):
    mapping = bproto.header_map(headers)
    if b":status" not in mapping or b"content-length" not in mapping:
        raise bproto.Malformed("missing status or content-length")
    status_bytes = mapping[b":status"]
    length_bytes = mapping[b"content-length"]
    if not re.fullmatch(rb"\d{3}", status_bytes):
        raise bproto.Malformed("bad status")
    if not re.fullmatch(rb"\d+", length_bytes):
        raise bproto.Malformed("bad content-length")
    status = int(status_bytes)
    declared = int(length_bytes)
    if declared != len(body):
        raise bproto.Malformed("content-length does not match body")
    return status


def exchange(host, port, path, verbose):
    frame = bproto.build_request(host, port, path)
    sock = socket.create_connection((host, port))
    try:
        bproto.write_all(sock, frame)
        if verbose:
            print(f">> request ({len(frame)} bytes)", file=sys.stderr)
            print(bproto.hexdump(frame), file=sys.stderr)
        while True:
            incoming = bproto.read_frame(sock)
            if incoming is None:
                print("bcurl: connection closed before a response", file=sys.stderr)
                return 1
            if verbose:
                label = {1: "request", 2: "response"}.get(
                    incoming.type, f"unknown {incoming.type}"
                )
                print(
                    f"<< {label} ({len(incoming.raw)} bytes)",
                    file=sys.stderr,
                )
                print(bproto.hexdump(incoming.raw), file=sys.stderr)
            if incoming.type not in (bproto.TYPE_REQUEST, bproto.TYPE_RESPONSE):
                continue
            if incoming.flags != 0 or incoming.type != bproto.TYPE_RESPONSE:
                print("bcurl: malformed response", file=sys.stderr)
                return 1
            headers, body = bproto.parse_message(incoming.payload)
            status = status_of(headers, body)
            sys.stdout.buffer.write(body)
            if status >= 400:
                print(f"bcurl: {status}", file=sys.stderr)
                return 1
            return 0
    finally:
        sock.close()


def main(argv):
    verbose = False
    args = []
    for arg in argv[1:]:
        if arg == "-v":
            verbose = True
        elif arg.startswith("-"):
            print("usage: bcurl [-v] host:port/path", file=sys.stderr)
            return 2
        else:
            args.append(arg)
    if len(args) != 1:
        print("usage: bcurl [-v] host:port/path", file=sys.stderr)
        return 2
    try:
        host, port, path = parse_target(args[0])
    except ValueError as exc:
        print(f"bcurl: {exc}", file=sys.stderr)
        return 2
    try:
        return exchange(host, port, path, verbose)
    except (bproto.NeedClose, bproto.Malformed, EOFError, OSError, UnicodeError, ValueError) as exc:
        print(f"bcurl: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
