"""Socket tests for the HTTP/1.1 calculator."""

import socket
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpcalc


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


def http_request(method, target, headers=None, body=b"", version="HTTP/1.1"):
    fields = {} if headers is None else dict(headers)
    if body and "Content-Length" not in fields:
        fields["Content-Length"] = str(len(body))
    lines = [f"{method} {target} {version}"]
    for key, value in fields.items():
        lines.append(f"{key}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


def send_parts(sock, parts):
    for part in parts:
        sock.sendall(part)


def read_response(sock, buf=b""):
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise EOFError("connection closed before headers")
        buf += chunk
    head, buf = buf.split(b"\r\n\r\n", 1)
    lines = head.decode("iso-8859-1").split("\r\n")
    status = int(lines[0].split(" ", 2)[1])
    headers = {}
    for line in lines[1:]:
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    need = int(headers["content-length"])
    while len(buf) < need:
        chunk = sock.recv(4096)
        if not chunk:
            raise EOFError("connection closed before body")
        buf += chunk
    return status, headers, buf[:need], buf[need:]


class CalculatorTests(unittest.TestCase):
    def setUp(self):
        self.listener, self.port, self.thread = start_server(httpcalc.handle_connection)

    def tearDown(self):
        self.listener.close()
        self.thread.join(timeout=2)

    def request(self, method, target, headers=None, body=b""):
        fields = {"Host": "localhost"}
        if headers:
            fields.update(headers)
        return http_request(method, target, fields, body)

    def test_marker_six_requests_one_connection(self):
        """The marking script: six responses, one handshake, socket still open."""
        cases = [
            ("GET", "/add?a=2&b=3", 200, b"5"),
            ("GET", "/sub?a=10&b=4", 200, b"6"),
            ("GET", "/mul?a=6&b=7", 200, b"42"),
            ("GET", "/div?a=1&b=0", 400, b"bad request"),
            ("GET", "/pow?a=2&b=8", 404, b"not found"),
            ("POST", "/add", 405, b"method not allowed"),
        ]
        sock = connect(self.port)
        try:
            blob = b"".join(self.request(method, target) for method, target, _status, _body in cases)
            sock.sendall(blob)
            rest = b""
            for method, target, status, body in cases:
                got_status, headers, got_body, rest = read_response(sock, rest)
                self.assertEqual(got_status, status, target)
                self.assertEqual(got_body, body, target)
                self.assertEqual(int(headers["content-length"]), len(got_body))
                self.assertNotIn("connection", headers)
            # Still the same socket: a seventh request is answered.
            sock.sendall(self.request("GET", "/div?a=9&b=3"))
            status, _headers, body, _rest = read_response(sock, rest)
            self.assertEqual((status, body), (200, b"3"))
            sock.getpeername()
        finally:
            sock.close()

    def test_feature_set(self):
        cases = [
            (self.request("GET", "/add?a=2&b=3"), 200, b"5"),
            (self.request("GET", "/sub?a=10&b=4"), 200, b"6"),
            (self.request("GET", "/mul?a=6&b=7"), 200, b"42"),
            (self.request("GET", "/div?a=9&b=3"), 200, b"3"),
            (self.request("GET", "/div?a=1&b=0"), 400, b"bad request"),
            (self.request("GET", "/add?a=x&b=3"), 400, b"bad request"),
            (self.request("GET", "/add?a=2"), 400, b"bad request"),
            (self.request("GET", "/add?b=3"), 400, b"bad request"),
            (self.request("GET", "/pow?a=2&b=8"), 404, b"not found"),
            (self.request("POST", "/add"), 405, b"method not allowed"),
            (http_request("GET", "/add?a=2&b=3"), 400, b"host required"),
        ]
        sock = connect(self.port)
        try:
            rest = b""
            for raw, status, body in cases:
                sock.sendall(raw)
                got_status, _headers, got_body, rest = read_response(sock, rest)
                self.assertEqual((got_status, got_body), (status, body))
                self.assertEqual(rest, b"")
        finally:
            sock.close()

    def test_body_containing_header_terminator_does_not_eat_next_request(self):
        # Content-Length is 4, and those 4 bytes are themselves a blank line.
        bad = (
            b"GET /add?a=2&b=3\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: 4\r\n"
            b"\r\n"
            b"\r\n\r\n"
        )
        good = self.request("GET", "/mul?a=6&b=7")
        sock = connect(self.port)
        try:
            sock.sendall(bad + good)
            status, _headers, body, rest = read_response(sock)
            self.assertEqual(status, 400)
            status, _headers, body, rest = read_response(sock, rest)
            self.assertEqual((status, body), (200, b"42"))
        finally:
            sock.close()

    def test_post_body_is_consumed_before_next_request(self):
        sock = connect(self.port)
        try:
            sock.sendall(self.request("POST", "/add", body=b"hello") + self.request("GET", "/add?a=2&b=3"))
            status, _headers, body, rest = read_response(sock)
            self.assertEqual((status, body), (405, b"method not allowed"))
            status, _headers, body, rest = read_response(sock, rest)
            self.assertEqual((status, body), (200, b"5"))
        finally:
            sock.close()

    def test_byte_at_a_time(self):
        raw = self.request("GET", "/sub?a=10&b=4")
        sock = connect(self.port)
        try:
            for index in range(len(raw)):
                sock.send(raw[index : index + 1])
            status, _headers, body, _rest = read_response(sock)
            self.assertEqual((status, body), (200, b"6"))
        finally:
            sock.close()

    def _add_request(self):
        raw = self.request("GET", "/add?a=2&b=3")
        self.assertTrue(raw.startswith(b"GET /add?a=2&b=3 "))
        return raw

    def _expect(self, sock, status, body, rest=b""):
        got_status, _headers, got_body, rest = read_response(sock, rest)
        self.assertEqual((got_status, got_body), (status, body))
        sock.getpeername()
        return rest

    def test_partial_split_inside_method(self):
        raw = self._add_request()
        sock = connect(self.port)
        try:
            send_parts(sock, (raw[:2], raw[2:]))
            self._expect(sock, 200, b"5")
        finally:
            sock.close()

    def test_partial_split_inside_path(self):
        raw = self._add_request()
        cut = raw.index(b"/add") + 2
        sock = connect(self.port)
        try:
            send_parts(sock, (raw[:cut], raw[cut:]))
            self._expect(sock, 200, b"5")
        finally:
            sock.close()

    def test_partial_split_inside_header(self):
        raw = self._add_request()
        cut = raw.index(b"localhost") + 3
        sock = connect(self.port)
        try:
            send_parts(sock, (raw[:cut], raw[cut:]))
            self._expect(sock, 200, b"5")
        finally:
            sock.close()

    def test_partial_split_around_header_terminator(self):
        raw = self._add_request()
        cut = raw.index(b"\r\n\r\n")
        sock = connect(self.port)
        try:
            send_parts(sock, (raw[:cut], raw[cut : cut + 2], raw[cut + 2 : cut + 4]))
            self._expect(sock, 200, b"5")
        finally:
            sock.close()

    def test_partial_body_then_next_request(self):
        posted = self.request("POST", "/add", body=b"hello")
        cut = posted.index(b"\r\n\r\n") + 4
        self.assertEqual(posted[cut:], b"hello")
        sock = connect(self.port)
        try:
            send_parts(sock, (posted[:cut], posted[cut : cut + 2], posted[cut + 2 :]))
            sock.sendall(self.request("GET", "/add?a=2&b=3"))
            rest = self._expect(sock, 405, b"method not allowed")
            self._expect(sock, 200, b"5", rest)
        finally:
            sock.close()

    def test_two_requests_in_one_write(self):
        sock = connect(self.port)
        try:
            sock.sendall(self.request("GET", "/add?a=2&b=3") + self.request("GET", "/sub?a=10&b=4"))
            rest = self._expect(sock, 200, b"5")
            self._expect(sock, 200, b"6", rest)
        finally:
            sock.close()

    def test_partial_first_request_then_second_immediately(self):
        raw = self._add_request()
        sock = connect(self.port)
        try:
            send_parts(
                sock,
                (
                    raw[:2],
                    raw[2:15],
                    raw[15:],
                    self.request("GET", "/mul?a=6&b=7"),
                ),
            )
            rest = self._expect(sock, 200, b"5")
            self._expect(sock, 200, b"42", rest)
        finally:
            sock.close()

    def test_chunked_body_is_rejected_and_closed(self):
        raw = (
            b"GET /add?a=2&b=3 HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
        )
        sock = connect(self.port)
        try:
            sock.sendall(raw)
            status, headers, body, _rest = read_response(sock)
            self.assertEqual((status, body), (400, b"bad request"))
            self.assertEqual(headers.get("connection"), "close")
            self.assertEqual(sock.recv(1), b"")
        finally:
            sock.close()

    def test_connection_close(self):
        sock = connect(self.port)
        try:
            sock.sendall(self.request("GET", "/add?a=2&b=3", {"Connection": "close"}))
            status, headers, body, _rest = read_response(sock)
            self.assertEqual((status, body), (200, b"5"))
            self.assertEqual(headers["connection"], "close")
            self.assertEqual(sock.recv(1), b"")
        finally:
            sock.close()


if __name__ == "__main__":
    unittest.main()
