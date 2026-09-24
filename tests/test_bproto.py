"""Socket tests for the binary file protocol."""

import io
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bcurl
import bproto
import bserve

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def start_server(handler):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    port = listener.getsockname()[1]

    def run():
        while True:
            try:
                conn, _addr = listener.accept()
            except OSError:
                return
            try:
                handler(conn)
            finally:
                conn.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return listener, port, thread


def connect(port):
    sock = socket.create_connection(("127.0.0.1", port), timeout=3)
    sock.settimeout(3)
    return sock


def send_slices(sock, data, ends):
    """Send data in pieces that end at the given indexes, then the tail."""
    start = 0
    for end in ends:
        sock.sendall(data[start:end])
        start = end
    if start < len(data):
        sock.sendall(data[start:])


def response_of(sock):
    frame = bproto.read_frame(sock)
    if frame is None:
        raise EOFError("no frame")
    headers, body = bproto.parse_message(frame.payload)
    mapping = bproto.header_map(headers)
    status = int(mapping[b":status"])
    if int(mapping[b"content-length"]) != len(body):
        raise AssertionError("content-length does not match body")
    return frame, status, body, mapping


class FramingTests(unittest.TestCase):
    def test_approved_request_bytes(self):
        frame = bproto.build_request("localhost", 9000, "/index.html")
        expected = bytes.fromhex(
            "c1 a1 01 01 00 00 00 39"
            "04"
            "01 00 03 47 45 54"
            "02 00 0b 2f 69 6e 64 65 78 2e 68 74 6d 6c"
            "04 00 0e 6c 6f 63 61 6c 68 6f 73 74 3a 39 30 30 30"
            "00 0a 75 73 65 72 2d 61 67 65 6e 74 00 05 62 63 75 72 6c"
        )
        self.assertEqual(frame, expected)
        ftype, flags, length = bproto.parse_header(frame[:8])
        self.assertEqual((ftype, flags, length), (1, 0, 57))
        headers, body = bproto.parse_message(frame[8:])
        self.assertEqual(body, b"")
        self.assertEqual(
            headers,
            [
                (b":method", b"GET"),
                (b":path", b"/index.html"),
                (b"host", b"localhost:9000"),
                (b"user-agent", b"bcurl"),
            ],
        )

    def test_approved_response_bytes(self):
        frame = bproto.build_response(200, b"hi\n", "text/html")
        expected = bytes.fromhex(
            "c1 a1 01 02 00 00 00 1a"
            "03"
            "03 00 03 32 30 30"
            "05 00 01 33"
            "06 00 09 74 65 78 74 2f 68 74 6d 6c"
            "68 69 0a"
        )
        self.assertEqual(frame, expected)
        _ftype, _flags, length = bproto.parse_header(frame[:8])
        self.assertEqual(length, 26)
        headers, body = bproto.parse_message(frame[8:])
        self.assertEqual(body, b"hi\n")
        self.assertEqual(headers[1], (b"content-length", b"3"))

    def test_sample_file_is_the_worked_example(self):
        self.assertEqual((ROOT / "www" / "index.html").read_bytes(), b"hi\n")

    def test_unknown_header_id_is_malformed(self):
        payload = bytes([1, 9, 0, 1, ord("x")])
        with self.assertRaises(bproto.Malformed):
            bproto.parse_message(payload)

    def test_payload_shorter_than_header_count_is_malformed(self):
        with self.assertRaises(bproto.Malformed):
            bproto.parse_message(b"\x05")


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "index.html").write_bytes(b"hi\n")
        (self.root / "hello.txt").write_bytes(b"hello\n")
        (self.root / "empty.txt").write_bytes(b"")
        (self.root / "magic.bin").write_bytes(b"\xc1\xa1HELLO")
        (self.root / "big.bin").write_bytes(b"A" * 300_000)
        (self.root / "subdir").mkdir()
        outside = self.root.parent / f"outside-{self.root.name}.txt"
        outside.write_text("secret", encoding="ascii")
        self.outside = outside
        (self.root / "escape").symlink_to(outside)
        self.listener, self.port, self.thread = start_server(
            lambda conn: bserve.handle_connection(conn, self.root)
        )

    def tearDown(self):
        self.listener.close()
        self.thread.join(timeout=2)
        self.outside.unlink(missing_ok=True)
        self.tmp.cleanup()

    def request(self, path):
        return bproto.build_request("127.0.0.1", self.port, path)

    def test_existing_file_and_content_length(self):
        sock = connect(self.port)
        try:
            bproto.write_all(sock, self.request("/index.html"))
            frame, status, body, mapping = response_of(sock)
            self.assertEqual(status, 200)
            self.assertEqual(body, b"hi\n")
            self.assertEqual(mapping[b"content-type"], b"text/html")
            self.assertEqual(mapping[b"content-length"], b"3")
            # Frame length covers headers plus body, not the body alone.
            _ftype, _flags, length = bproto.parse_header(frame.raw[:8])
            self.assertGreater(length, len(body))
        finally:
            sock.close()

    def test_text_file(self):
        sock = connect(self.port)
        try:
            bproto.write_all(sock, self.request("/hello.txt"))
            _frame, status, body, mapping = response_of(sock)
            self.assertEqual((status, body), (200, b"hello\n"))
            self.assertEqual(mapping[b"content-type"], b"text/plain")
        finally:
            sock.close()

    def test_missing_file_and_directory(self):
        sock = connect(self.port)
        try:
            bproto.write_all(sock, self.request("/missing.html"))
            _frame, status, body, _mapping = response_of(sock)
            self.assertEqual((status, body), (404, b""))
            bproto.write_all(sock, self.request("/subdir"))
            _frame, status, body, _mapping = response_of(sock)
            self.assertEqual((status, body), (404, b""))
        finally:
            sock.close()

    def test_bad_magic_does_not_wait_for_declared_length(self):
        sock = connect(self.port)
        try:
            # Version/magic wrong and length claims 16MB-1, but no payload follows.
            sock.sendall(b"\x00\x00\x01\x01\x00\xff\xff\xff")
            _frame, status, body, _mapping = response_of(sock)
            self.assertEqual((status, body), (400, b""))
            self.assertEqual(sock.recv(1), b"")
        finally:
            sock.close()

    def test_bad_version_closes(self):
        sock = connect(self.port)
        try:
            sock.sendall(b"\xc1\xa1\x02\x01\x00\x00\x00\x00")
            _frame, status, body, _mapping = response_of(sock)
            self.assertEqual((status, body), (400, b""))
            self.assertEqual(sock.recv(1), b"")
        finally:
            sock.close()

    def test_unknown_frame_then_real_request(self):
        sock = connect(self.port)
        try:
            unknown = bproto.encode_frame(0x7F, b"\xc1\xa1not-a-frame")
            bproto.write_all(sock, unknown + self.request("/hello.txt"))
            _frame, status, body, _mapping = response_of(sock)
            self.assertEqual((status, body), (200, b"hello\n"))
        finally:
            sock.close()

    def test_empty_payload_then_real_request(self):
        sock = connect(self.port)
        try:
            empty = bproto.encode_frame(bproto.TYPE_REQUEST, b"")
            bproto.write_all(sock, empty + self.request("/empty.txt"))
            _frame, status, body, _mapping = response_of(sock)
            self.assertEqual((status, body), (400, b""))
            _frame, status, body, mapping = response_of(sock)
            self.assertEqual((status, body), (200, b""))
            self.assertEqual(mapping[b"content-length"], b"0")
        finally:
            sock.close()

    def test_payload_length_does_not_match_header_block(self):
        sock = connect(self.port)
        try:
            # Length is 1. That one byte claims five headers which are not there.
            short = bproto.encode_frame(bproto.TYPE_REQUEST, b"\x05")
            bproto.write_all(sock, short + self.request("/index.html"))
            _frame, status, body, _mapping = response_of(sock)
            self.assertEqual((status, body), (400, b""))
            _frame, status, body, _mapping = response_of(sock)
            self.assertEqual((status, body), (200, b"hi\n"))
        finally:
            sock.close()

    def test_nonzero_flags_then_next_request(self):
        sock = connect(self.port)
        try:
            flagged = bytearray(self.request("/index.html"))
            flagged[4] = 0x01
            bproto.write_all(sock, bytes(flagged) + self.request("/index.html"))
            _frame, status, body, _mapping = response_of(sock)
            self.assertEqual((status, body), (400, b""))
            _frame, status, body, _mapping = response_of(sock)
            self.assertEqual((status, body), (200, b"hi\n"))
        finally:
            sock.close()

    def test_two_files_one_connection(self):
        sock = connect(self.port)
        try:
            bproto.write_all(sock, self.request("/index.html") + self.request("/hello.txt"))
            _frame, status, body, _mapping = response_of(sock)
            self.assertEqual((status, body), (200, b"hi\n"))
            _frame, status, body, _mapping = response_of(sock)
            self.assertEqual((status, body), (200, b"hello\n"))
            sock.getpeername()
        finally:
            sock.close()

    def test_illegal_paths(self):
        sock = connect(self.port)
        try:
            for path in ("/../secret", "/foo/../../etc/passwd", "/%2e%2e/secret", "/"):
                bproto.write_all(sock, self.request(path))
                _frame, status, body, _mapping = response_of(sock)
                self.assertEqual((status, body), (400, b""), path)
        finally:
            sock.close()

    def test_symlink_escape(self):
        sock = connect(self.port)
        try:
            bproto.write_all(sock, self.request("/escape"))
            _frame, status, body, _mapping = response_of(sock)
            self.assertEqual(status, 400)
            self.assertNotIn(b"secret", body)
        finally:
            sock.close()

    def test_large_file_and_magic_bytes_inside_body(self):
        sock = connect(self.port)
        try:
            bproto.write_all(sock, self.request("/big.bin") + self.request("/magic.bin"))
            _frame, status, body, _mapping = response_of(sock)
            self.assertEqual(status, 200)
            self.assertEqual(body, b"A" * 300_000)
            _frame, status, body, _mapping = response_of(sock)
            self.assertEqual((status, body), (200, b"\xc1\xa1HELLO"))
        finally:
            sock.close()

    def test_partial_frame_header_and_payload(self):
        frame = self.request("/index.html")
        self.assertGreater(len(frame), 8)
        sock = connect(self.port)
        try:
            # 2 bytes, then 1, then 4, then the rest of the frame.
            send_slices(sock, frame, (2, 3, 7))
            _frame, status, body, mapping = response_of(sock)
            self.assertEqual((status, body), (200, b"hi\n"))
            self.assertEqual(mapping[b"content-length"], b"3")
            sock.getpeername()
        finally:
            sock.close()

    def test_partial_split_before_last_payload_byte(self):
        frame = self.request("/hello.txt")
        sock = connect(self.port)
        try:
            sock.sendall(frame[:-1])
            sock.sendall(frame[-1:])
            _frame, status, body, _mapping = response_of(sock)
            self.assertEqual((status, body), (200, b"hello\n"))
        finally:
            sock.close()

    def test_two_frames_split_across_writes(self):
        first = self.request("/index.html")
        second = self.request("/hello.txt")
        both = first + second
        sock = connect(self.port)
        try:
            send_slices(sock, both, (5, len(first) + 3))
            _frame, status, body, _mapping = response_of(sock)
            self.assertEqual((status, body), (200, b"hi\n"))
            _frame, status, body, _mapping = response_of(sock)
            self.assertEqual((status, body), (200, b"hello\n"))
        finally:
            sock.close()

    def test_byte_at_a_time(self):
        raw = self.request("/hello.txt")
        sock = connect(self.port)
        try:
            for index in range(len(raw)):
                sock.send(raw[index : index + 1])
            _frame, status, body, _mapping = response_of(sock)
            self.assertEqual((status, body), (200, b"hello\n"))
        finally:
            sock.close()

    def test_wrong_method(self):
        headers = [
            (bproto.ID_METHOD, "POST", None),
            (bproto.ID_PATH, "/index.html", None),
        ]
        frame = bproto.encode_frame(bproto.TYPE_REQUEST, bproto.encode_payload(headers, b""))
        sock = connect(self.port)
        try:
            bproto.write_all(sock, frame)
            _frame, status, body, _mapping = response_of(sock)
            self.assertEqual((status, body), (400, b""))
        finally:
            sock.close()


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "index.html").write_bytes(b"hi\n")
        self.listener, self.port, self.thread = start_server(
            lambda conn: bserve.handle_connection(conn, self.root)
        )

    def tearDown(self):
        self.listener.close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def test_exchange_opens_one_tcp_connection(self):
        calls = []
        real = socket.create_connection

        def counting(address, *args, **kwargs):
            calls.append(address)
            return real(address, *args, **kwargs)

        captured = io.BytesIO()
        saved_stdout = sys.stdout
        socket.create_connection = counting
        sys.stdout = io.TextIOWrapper(captured, encoding="utf-8")
        try:
            code = bcurl.exchange("127.0.0.1", self.port, "/index.html", verbose=False)
            sys.stdout.flush()
            body = captured.getvalue()
        finally:
            socket.create_connection = real
            sys.stdout = saved_stdout
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(body, b"hi\n")

    def test_bcurl_stdout_and_verbose_hex(self):
        proc = subprocess.run(
            [PY, str(ROOT / "bcurl.py"), "-v", f"127.0.0.1:{self.port}/index.html"],
            cwd=ROOT,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, b"hi\n")
        text = proc.stderr.decode("ascii")
        self.assertIn(">> request", text)
        self.assertIn("<< response", text)
        self.assertIn("c1 a1", text)

    def test_bcurl_missing_file_exits_nonzero(self):
        proc = subprocess.run(
            [PY, str(ROOT / "bcurl.py"), f"127.0.0.1:{self.port}/missing.html"],
            cwd=ROOT,
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, b"")
        self.assertIn(b"404", proc.stderr)

    def test_client_skips_unknown_frame_on_one_connection(self):
        def handler(conn):
            incoming = bproto.read_frame(conn)
            if incoming is None:
                return
            unknown = bproto.encode_frame(0x2A, b"skip-me")
            ok = bproto.build_response(200, b"hi\n", "text/html")
            bproto.write_all(conn, unknown + ok)

        listener, port, thread = start_server(handler)
        captured = io.BytesIO()
        saved = sys.stdout
        sys.stdout = io.TextIOWrapper(captured, encoding="utf-8")
        try:
            code = bcurl.exchange("127.0.0.1", port, "/index.html", verbose=False)
            self.assertEqual(code, 0)
            self.assertEqual(captured.getvalue(), b"hi\n")
        finally:
            sys.stdout = saved
            listener.close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
