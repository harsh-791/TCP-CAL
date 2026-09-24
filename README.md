# Calculator on one connection, and a small binary file protocol

Two programs, both plain Python sockets. No web framework.

The calculator speaks HTTP/1.1. The file server speaks the protocol in `protocol-spec.md`. That spec is the contract: a client written from the spec, not from reading this server, should still work.

## Layout

```
TCP-Cal/
├── httpcalc.py          HTTP/1.1 calculator
├── httpcalc             launcher
├── bproto.py            frame encode/decode shared by server and client
├── bserve.py            binary file server
├── bcurl.py             binary client
├── bserve               launcher: ./bserve ./www 9000
├── bcurl                launcher: ./bcurl -v localhost:9000/index.html
├── www/index.html       the worked example, exactly the bytes hi\n
├── www/hello.txt
├── protocol-spec.md     two-page protocol spec
├── hexdump.md           annotated request and response
├── runtests             ./runtests
└── tests/
```

## Commands

No compile step. The launchers find a working `python3`.

Calculator, port 8080, as the marker expects:

```bash
./httpcalc
```

Another port: `./httpcalc 8080`.

Binary server and client:

```bash
./bserve ./www 9000
./bcurl -v localhost:9000/index.html
./bcurl localhost:9000/hello.txt
```

`bcurl` writes the file to stdout. `-v` writes a hex dump of every frame to stderr, so the dump does not mix into the file. Exit status is non-zero on status 400 or higher. One run opens one TCP connection.

Tests:

```bash
./runtests
```

That is `python3 -m unittest discover -s tests -v`, using a Python the launcher can actually start.

The annotated dump is `hexdump.md`. It matches `www/index.html`.

`./bcurl -v localhost:9000/index.html` writes this on stdout:

```text
hi
```

Those are the three bytes `68 69 0a`. The frame dump goes to stderr and starts with `c1 a1`. `content-length` in that response is `3`. The whole response frame is 34 bytes. A missing file writes nothing on stdout, writes `bcurl: 404` on stderr, and exits 1. `./runtests` should finish with `OK`.

## Calculator

| Request | Status | Body |
|---|---|---|
| `GET /add?a=2&b=3` | 200 | `5` |
| `GET /sub?a=10&b=4` | 200 | `6` |
| `GET /mul?a=6&b=7` | 200 | `42` |
| `GET /div?a=9&b=3` | 200 | `3` |
| `GET /div?a=1&b=0` | 400 | `bad request` |
| `GET /add?a=x&b=3` | 400 | `bad request` |
| `GET /add` with `a` or `b` missing | 400 | `bad request` |
| `GET /pow?a=2&b=8` | 404 | `not found` |
| `POST /add` | 405 | `method not allowed` |
| `GET /add?a=2&b=3` with no `Host` | 400 | `host required` |

Operands are integers. Division is integer division (`9/3` is `3`). A successful body is the digits only, with no extra newline.

`a` and `b` missing is not on the slide. It is handled as 400, same as `a=x`, because the request is not a valid sum.

HTTP/1.1 keeps the connection open. The marker’s check is six responses on one handshake, then the socket is still open.

A request ends at the blank line (`\r\n\r\n`). If `Content-Length` is present, that many extra bytes are the body, and they are removed even when the request is rejected. The following bytes are the next request. The server does not close the socket to discover the end.

`Connection: close` is honoured: the response says `Connection: close`, then the server closes. A `Transfer-Encoding` header is rejected and the connection closes, because this server does not implement chunked bodies. There is no idle timeout.

## Binary server

```bash
./bserve ./www 9000
```

`./www` is the root. A request path is joined under that directory. `..`, a symlink that points outside the root, and odd characters are 400. A missing file or a directory is 404. The file bytes are the response body. `content-length` is the file size. The frame length is larger, because it also counts the headers.

An unknown frame type is skipped. The connection stays up, and the next frame is parsed normally.

## What the tests check

Run `./runtests`. Each test is one requirement. Partial reads are covered as well: an HTTP request split inside the method, the path, a header, and the blank line; a body split across writes; two HTTP requests in one write; and a binary frame split inside its 8-byte header and inside its payload.

Calculator, all on one TCP connection unless noted:

| Test | Expect | Requirement |
|---|---|---|
| add `2+3` | 200, body `5` | addition |
| sub `10-4` | 200, body `6` | subtraction |
| mul `6*7` | 200, body `42` | multiplication |
| div `9/3` | 200, body `3` | division |
| div `1/0` | 400 | division by zero |
| `a=x` | 400 | not an integer |
| `a` or `b` missing | 400 | incomplete query |
| `GET /pow` | 404 | unknown operation |
| `POST /add` | 405 | method |
| `GET /add` with no Host | 400 | HTTP/1.1 Host |
| six marker requests, then another | six matching responses, socket still open | one handshake |
| body whose bytes are `\r\n\r\n`, then a real GET | 400, then 200 `42` | Content-Length, not a scan for the next header |
| POST body `hello`, then GET add | 405, then 200 `5` | body consumed before the next request |
| request sent one byte at a time | 200 `6` | partial reads |
| `Connection: close` | 200, then the server closes | optional close |

Binary protocol:

| Test | Expect | Requirement |
|---|---|---|
| encode `/index.html` | the 65 request bytes in `hexdump.md` | spec matches code |
| encode `hi\n` | the 34 response bytes | content-length is `3`, frame length is 26 |
| fetch `index.html` | 200, body `hi\n`, type `text/html` | existing file |
| fetch `hello.txt` | 200, body `hello\n` | second type |
| missing file, and a directory | 404, empty body | not found |
| magic wrong, length claims 16MB, no payload sent | 400, then the socket closes, without waiting | bad magic, length not trusted |
| version 2 | 400, then close | unknown version |
| type 127, then a real request | only the file response | unknown type skipped |
| empty payload, then a real request | 400, then 200 | empty payload |
| length 1 but header count 5, then a real request | 400, then 200 | payload does not match its own headers |
| flags not 0, then a real request | 400, then 200 | flags |
| two paths on one socket | both files | connection stays open |
| `..`, `%2e%2e`, `/` | 400 | illegal path |
| symlink to a file outside the root | 400, body is not that file | stay inside the root |
| 300000-byte file, then a file that starts with `C1 A1` | both bodies exact | large payload; magic inside a body is not a new frame |
| frame sent one byte at a time | 200 | partial reads |
| `:method` `POST` | 400 | method |
| `./bcurl -v` | stdout is the file, stderr contains `c1 a1`, exit 0 | client and verbose dump |
| `./bcurl` missing file | stdout empty, exit 1 | 4xx |
| server sends an unknown frame, then the response | client prints the file | client skips unknown types on the same connection |
