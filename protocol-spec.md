# Binary file protocol — version 1

A TCP connection carries frames. Each frame is an 8-byte header plus a payload. The length in the header is how the receiver finds the next frame. One file fetch is one `REQUEST` and one `RESPONSE` on that same connection. The server then reads another frame. A client fetching one URL opens one connection.

Multi-byte integers are big-endian. The number 57 is the bytes `00 00 39`.

## Frame

| Offset | Size | Field | Value |
|---:|---:|---|---|
| 0 | 2 | Magic | `C1 A1` |
| 2 | 1 | Version | `1` |
| 3 | 1 | Type | `1` request, `2` response, else unknown |
| 4 | 1 | Flags | `0` |
| 5 | 3 | Length | payload bytes, 0 … 16777215 |
| 8 | Length | Payload | parsed only for a known type |

A frame is `8 + Length` bytes. Length does not include the header. There is no stream id: one request is handled at a time, so that field would always be zero.

If magic is not `C1 A1` or version is not `1`, the length is not trustworthy. Send one `400` and close. If flags are not `0` on a known type, consume the payload, send `400`, and keep the connection.

| Type | Action |
|---:|---|
| 1 REQUEST | Parse the payload as a request. |
| 2 RESPONSE | Parse the payload as a response. |
| other | Read exactly Length bytes, ignore them, read the next header. Send nothing. |

Skipping an unknown type is how a later version can add frames. The old receiver stays in sync because it still knows where the frame ends.

## Headers

The payload is: header count (1 byte), that many headers, then the body. The body is every payload byte left after the headers. A request body is empty.

| ID | Name | ID | Name |
|---:|---|---:|---|
| 1 | `:method` | 4 | `host` |
| 2 | `:path` | 5 | `content-length` |
| 3 | `:status` | 6 | `content-type` |

ID `0` means the name is written in the frame.

Known name: `ID` (1) + value length (2) + value.

Literal name: `00` + name length (1) + name + value length (2) + value.

A value is at most 65535 bytes. A literal name is 1 … 255 bytes. Any other ID is malformed. A repeated `:method`, `:path`, `:status`, `content-length`, or `content-type` is malformed.

`content-length` is the body size. The frame length is headers plus body. Both appear, and they are different numbers.

`:method` = `GET` is `01 00 03 47 45 54`.

Literal `user-agent` = `bcurl` is `00 0A 75 73 65 72 2D 61 67 65 6E 74 00 05 62 63 75 72 6C`.

## Request and response

A request is type `1`, flags `0`, empty body. `:path` is required. It starts with `/` and each segment is letters, digits, `.`, `_`, or `-`. A segment of `..`, `.`, or empty is malformed. The server joins the path to its root. The resolved file must stay inside that root. `host` may be sent and is not used to choose the file. `:method`, if present, must be `GET`.

| Status | When | Body |
|---:|---|---|
| 200 | the path is a regular file inside the root | the file bytes |
| 400 | malformed frame, bad flags, illegal path, or method is not GET | empty |
| 404 | the path is legal but the file is missing, or it is a directory | empty |

Every response is type `2` and includes `:status` (`200`, `400`, or `404`) and `content-length`. A `200` also includes `content-type`: `.html` `text/html`, `.txt` `text/plain`, `.css` `text/css`, `.js` `text/javascript`, `.png` `image/png`, `.jpg` and `.jpeg` `image/jpeg`, otherwise `application/octet-stream`.

The client writes the body to stdout and exits non-zero when the status is 400 or higher. With `-v` it hex-dumps every frame it sends or receives, including a skipped unknown frame.

## Connection

After a normal response the server reads the next header on the same socket. End of file before a header means the peer left. End of file in the middle of a payload closes the connection. An unknown type does not close it.

## Example

`www/index.html` is the three bytes `68 69 0A` (`hi` and a newline). Request: `GET /index.html`, host `localhost:9000`, literal `user-agent: bcurl`. The request is 65 bytes, payload 57. The response is 34 bytes, payload 26. The body starts at response offset `1F`. The next frame would start at offset `22`. `content-length` is the character `3`. The frame length is 26. Notes for every byte are in `hexdump.md`.

```
c1 a1 01 01 00 00 00 39 04 01 00 03 47 45 54
02 00 0b 2f 69 6e 64 65 78 2e 68 74 6d 6c
04 00 0e 6c 6f 63 61 6c 68 6f 73 74 3a 39 30 30 30
00 0a 75 73 65 72 2d 61 67 65 6e 74 00 05 62 63 75 72 6c

c1 a1 01 02 00 00 00 1a 03 03 00 03 32 30 30
05 00 01 33 06 00 09 74 65 78 74 2f 68 74 6d 6c 68 69 0a
```
