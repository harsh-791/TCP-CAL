# Annotated frame dump

Worked example: `./bcurl -v localhost:9000/index.html` when `www/index.html` is the three bytes `hi` plus a newline.

These are the bytes `bproto.build_request` and `bproto.build_response` produce. The tests lock them.

## Request — 65 bytes

Payload length is 57 (`00 00 39`). The payload starts at offset `08` and ends at offset `40`. The next frame would start at offset `41`.

| Offset | Hex | Meaning |
|---|---|---|
| `00` | `C1 A1` | Magic |
| `02` | `01` | Version 1 |
| `03` | `01` | Type REQUEST |
| `04` | `00` | Flags, must be 0 |
| `05` | `00 00 39` | Payload length 57. Does not include this 8-byte header |
| `08` | `04` | Four headers follow. Payload starts here |
| `09` | `01` | Header id 1, name `:method` |
| `0A` | `00 03` | Value length 3 |
| `0C` | `47 45 54` | `GET` |
| `0F` | `02` | Header id 2, name `:path` |
| `10` | `00 0B` | Value length 11 |
| `12` | `2F 69 6E 64 65 78 2E 68 74 6D 6C` | `/index.html` |
| `1D` | `04` | Header id 4, name `host` |
| `1E` | `00 0E` | Value length 14 |
| `20` | `6C 6F 63 61 6C 68 6F 73 74 3A 39 30 30 30` | `localhost:9000` |
| `2E` | `00` | Id 0: the name is not in the table |
| `2F` | `0A` | Name length 10 |
| `30` | `75 73 65 72 2D 61 67 65 6E 74` | `user-agent` |
| `3A` | `00 05` | Value length 5 |
| `3C` | `62 63 75 72 6C` | `bcurl` |

Full frame:

```
0000  c1 a1 01 01 00 00 00 39 04 01 00 03 47 45 54 02
0010  00 0b 2f 69 6e 64 65 78 2e 68 74 6d 6c 04 00 0e
0020  6c 6f 63 61 6c 68 6f 73 74 3a 39 30 30 30 00 0a
0030  75 73 65 72 2d 61 67 65 6e 74 00 05 62 63 75 72
0040  6c
```

How the receiver reads it:

1. Read 8 bytes. Magic `C1 A1` and version `1` match, so the length field is trustworthy.
2. Type is `1`, flags are `0`, length is 57.
3. Read exactly 57 more bytes. That slice is the payload. The next frame, if any, starts after it.
4. First payload byte is `04`, so read four headers.
5. Id `01` is `:method`. The next two bytes say the value is 3 bytes: `GET`.
6. Id `02` is `:path`, 11 bytes: `/index.html`.
7. Id `04` is `host`, 14 bytes: `localhost:9000`.
8. Id `00` means a literal name. The next byte says the name is 10 bytes (`user-agent`). The two bytes after the name say the value is 5 bytes (`bcurl`).
9. No payload bytes remain, so the body is empty.

`host` is not used to find the file. `:path` is.

## Response — 34 bytes

Payload length is 26 (`00 00 1A`). Headers occupy the first 23 payload bytes. The body starts at offset `1F` and is 3 bytes. The next frame would start at offset `22`.

| Offset | Hex | Meaning |
|---|---|---|
| `00` | `C1 A1` | Magic |
| `02` | `01` | Version 1 |
| `03` | `02` | Type RESPONSE |
| `04` | `00` | Flags |
| `05` | `00 00 1A` | Payload length 26 |
| `08` | `03` | Three headers. Payload starts here |
| `09` | `03` | Header id 3, name `:status` |
| `0A` | `00 03` | Value length 3 |
| `0C` | `32 30 30` | `200` |
| `0F` | `05` | Header id 5, name `content-length` |
| `10` | `00 01` | Value length 1 |
| `12` | `33` | `3` — size of the file, not of the frame |
| `13` | `06` | Header id 6, name `content-type` |
| `14` | `00 09` | Value length 9 |
| `16` | `74 65 78 74 2F 68 74 6D 6C` | `text/html` |
| `1F` | `68 69 0A` | Body `hi\n`. Payload ends after this byte |

Full frame:

```
0000  c1 a1 01 02 00 00 00 1a 03 03 00 03 32 30 30 05
0010  00 01 33 06 00 09 74 65 78 74 2f 68 74 6d 6c 68
0020  69 0a
```

`content-length` is the character `3` because the file is 3 bytes. The frame length is 26 because it also counts the header block. A receiver that treated 26 as the file size would be wrong. A receiver that treated 3 as the distance to the next frame would also be wrong.

## Where the next frame is

Request: `8 + 57 = 65` (`0x41`).
Response: `8 + 26 = 34` (`0x22`).

An unknown type uses the same rule. Read 8 bytes, take the 3-byte length, skip that many bytes, and continue. The bytes inside the skipped payload are not parsed, even if they contain `C1 A1`.
