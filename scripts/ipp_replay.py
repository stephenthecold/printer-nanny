"""Replay a real printer's IPP attributes, so a client fault can be isolated.

WHY THIS EXISTS
---------------
A Brother MFC-L8900CDW got a queue from ``Add-Printer -IppURL`` that reported
healthy and could not print: the job died in the print processor with
``0x80004005`` and no ``id=307``. The same device printed from CUPS over the same
IPP endpoint, and from the same Windows box over raw 9100. So the fault was
somewhere between Windows' inbox class driver and *something the device says
about itself* -- and there are ~400 of those.

    capture   pull a real device's Get-Printer-Attributes response, raw
    serve     replay it verbatim, with named attributes overridden or imported
              wholesale from a second (working) capture

Because everything except the named attributes is byte-identical to what the real
device said, a behaviour change is attributable to those attributes.

WHAT IS ESTABLISHED
-------------------
With a *verified* harness (see the traps below), replaying the Brother's captured
attributes reproduces the failure reliably -- 3/3, with the client demonstrably
querying this server. Importing all 78 non-identity attributes from a working
device's capture makes it print. So **the failure is determined by what the
device advertises**, and a probe could in principle predict it.

It is a *combination*: neither half of those 78 is sufficient alone. The
individual attribute(s) have NOT been identified.

READ THIS BEFORE TRUSTING A RESULT
----------------------------------
Three harness bugs each produced confident, wrong answers before being caught.
A runner around this script must guard all three:

1. **Poll for the outcome; never sleep a fixed interval.** A 30s wait recorded
   ``DID NOT PRINT`` whenever a job merely took longer, which made every negative
   result untrustworthy. Poll ``PrintService/Operational`` for ``id=307`` (that
   channel is OFF by default -- enable it first).
2. **Verify the running server is the one you just configured.** ``pkill`` +
   ``sleep 1`` is not enough: the new process hits ``Address already in use``,
   dies in the background, and the PREVIOUS configuration keeps serving. Wait for
   the port to actually free, check the log for a traceback, and then confirm over
   IPP that the server answers with the identity you set.
3. **Confirm the client actually queried you.** A verdict from a run where this
   server saw zero ``Get-Printer-Attributes`` is a verdict about a cached device,
   not about your configuration.

Also: Windows keys an IPP device on ``printer-uuid``. Reuse one and it will
reuse the cached device instead of re-reading your attributes; a capture whose
UUID matches an already-installed printer fails with "printer already exists".
Override it with a fresh UUID per run.

USAGE
-----
    python3 scripts/ipp_replay.py capture 10.0.0.5 brother.ipp
    python3 scripts/ipp_replay.py capture 127.0.0.1 good.ipp 631 /printers/Queue

    python3 scripts/ipp_replay.py serve brother.ipp 10.211.55.2 8631 airprint \
        --from good.ipp media-supported,print-color-mode-supported \
        printer-uuid=urn:uuid:$(uuidgen) \
        printer-make-and-model="Bisect Device"

``airprint`` / ``everywhere`` selects the ``ipp-features-supported`` value. An
override with an empty value drops the attribute. Self-referential URIs are
re-pointed at this server automatically -- otherwise the client goes back to the
real printer and the experiment tests nothing.
"""
from __future__ import annotations

import http.server
import socket
import struct
import sys

# IPP delimiter tags
OPERATION_ATTRS, JOB_ATTRS, END_ATTRS, PRINTER_ATTRS = 0x01, 0x02, 0x03, 0x04
KEYWORD, URI, ENUM, INTEGER, CHARSET, NATURAL_LANG, TEXT = (
    0x44, 0x45, 0x23, 0x21, 0x47, 0x48, 0x41,
)
OP_GET_PRINTER_ATTRS, OP_VALIDATE_JOB = 0x000B, 0x0004
OP_PRINT_JOB, OP_CREATE_JOB, OP_SEND_DOC = 0x0002, 0x0005, 0x0006
OP_GET_JOB_ATTRS = 0x0009
STATUS_OK = 0x0000


def _attr(tag: int, name: bytes, value: bytes) -> bytes:
    return struct.pack(">BH", tag, len(name)) + name + struct.pack(">H", len(value)) + value


def _addl(tag: int, value: bytes) -> bytes:
    """An additional value of a 1setOf: same tag, zero-length name."""
    return struct.pack(">BHH", tag, 0, len(value)) + value


def build_request(printer_uri: str) -> bytes:
    body = struct.pack(">BBHI", 2, 0, OP_GET_PRINTER_ATTRS, 1)
    body += bytes([OPERATION_ATTRS])
    body += _attr(CHARSET, b"attributes-charset", b"utf-8")
    body += _attr(NATURAL_LANG, b"attributes-natural-language", b"en")
    body += _attr(URI, b"printer-uri", printer_uri.encode())
    body += _attr(KEYWORD, b"requested-attributes", b"all")
    body += bytes([END_ATTRS])
    return body


def capture(host: str, out: str, port: int = 631, path: str = "/ipp/print") -> None:
    uri = f"ipp://{host}:{port}{path}"
    body = build_request(uri)
    req = (
        f"POST {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        f"Content-Type: application/ipp\r\nContent-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode() + body
    s = socket.create_connection((host, port), timeout=20)
    s.sendall(req)
    chunks = []
    while True:
        b = s.recv(65536)
        if not b:
            break
        chunks.append(b)
    s.close()
    raw = b"".join(chunks)
    head, _, payload = raw.partition(b"\r\n\r\n")
    if b"Transfer-Encoding: chunked" in head:
        payload = _dechunk(payload)
    open(out, "wb").write(payload)
    print(f"captured {len(payload)} bytes of IPP from {host}")


def _dechunk(data: bytes) -> bytes:
    out = b""
    while data:
        line, _, rest = data.partition(b"\r\n")
        try:
            n = int(line.split(b";")[0], 16)
        except ValueError:
            break
        if n == 0:
            break
        out += rest[:n]
        data = rest[n + 2:]
    return out


def parse(body: bytes):
    """-> (version, status, request_id, [('d', tag) | ('a', tag, name, value)])"""
    ver_major, ver_minor, status, rid = struct.unpack(">BBHI", body[:8])
    i, items = 8, []
    while i < len(body):
        tag = body[i]
        if tag <= 0x05:  # delimiter
            items.append(("d", tag))
            i += 1
            if tag == END_ATTRS:
                break
            continue
        nlen = struct.unpack(">H", body[i + 1:i + 3])[0]
        name = body[i + 3:i + 3 + nlen]
        j = i + 3 + nlen
        vlen = struct.unpack(">H", body[j:j + 2])[0]
        value = body[j + 2:j + 2 + vlen]
        items.append(("a", tag, name, value))
        i = j + 2 + vlen
    return (ver_major, ver_minor), status, rid, items


def serialise(version, status, rid, items) -> bytes:
    out = struct.pack(">BBHI", version[0], version[1], status, rid)
    for it in items:
        if it[0] == "d":
            out += bytes([it[1]])
        else:
            _, tag, name, value = it
            out += struct.pack(">BH", tag, len(name)) + name
            out += struct.pack(">H", len(value)) + value
    return out


OVERRIDES: dict = {}
DONOR: dict = {}


def rewrite(items, *, features: list, my_uri: str):
    """Substitute ipp-features-supported and re-point the device's self-URIs.

    Only these change. Everything else -- every media size, margin, resolution,
    operation and quirk the real device reports -- is passed through untouched,
    which is the whole point: any difference in Windows' behaviour is
    attributable to the substituted attribute.
    """
    out, i = [], 0
    dropped = {b"printer-more-info", b"printer-supply-info-uri", b"printer-icons",
               b"printer-strings-uri", b"printer-privacy-policy-uri"}
    while i < len(items):
        it = items[i]
        if it[0] != "a":
            out.append(it)
            i += 1
            continue
        _, tag, name, value = it
        # Collect this attribute's continuation values (zero-length names).
        run = [it]
        j = i + 1
        while j < len(items) and items[j][0] == "a" and items[j][2] == b"":
            run.append(items[j])
            j += 1

        nm = name.decode(errors="replace")
        if nm in DONOR:
            # Replace this attribute with the donor's version verbatim (or drop
            # it if the donor does not have it). This is what makes a bisect
            # between a failing and a working device possible.
            out.extend(DONOR[nm])
        elif name.decode(errors="replace") in OVERRIDES:
            vals = OVERRIDES[name.decode(errors="replace")]
            if vals == ["<<DROP>>"]:
                pass
            else:
                out.append(("a", tag, name, vals[0].encode()))
                for extra in vals[1:]:
                    out.append(("a", tag, b"", extra.encode()))
        elif name == b"ipp-features-supported":
            out.append(("a", KEYWORD, name, features[0].encode()))
            for extra in features[1:]:
                out.append(("a", KEYWORD, b"", extra.encode()))
        elif name in (b"printer-uri-supported",):
            out.append(("a", URI, name, my_uri.encode()))
        elif name in dropped:
            pass  # would send Windows back to the real device
        else:
            out.extend(run)
        i = j
    return out


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    ATTRS: list = []
    JOBS: list = []

    def log_message(self, *a):  # quieter
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        if len(body) < 8:
            self.send_error(400)
            return
        # Echo the client's IPP version. Replying 2.0 to a 1.1 request is a
        # protocol error some clients reject outright, and this fake must not
        # differ from the real device in any way except the one attribute.
        ver = (body[0], body[1])
        op = struct.unpack(">H", body[2:4])[0]
        rid = struct.unpack(">I", body[4:8])[0]

        if op == OP_GET_PRINTER_ATTRS:
            items = [("d", OPERATION_ATTRS),
                     ("a", CHARSET, b"attributes-charset", b"utf-8"),
                     ("a", NATURAL_LANG, b"attributes-natural-language", b"en")]
            items += [("d", PRINTER_ATTRS)] + self.ATTRS + [("d", END_ATTRS)]
            resp = serialise(ver, STATUS_OK, rid, items)
            print(f"  -> Get-Printer-Attributes ok ({len(self.ATTRS)} attrs)", flush=True)
        elif op in (OP_VALIDATE_JOB,):
            resp = self._simple(ver, rid)
            print("  -> Validate-Job ok", flush=True)
        elif op in (OP_PRINT_JOB, OP_CREATE_JOB, OP_SEND_DOC):
            self.JOBS.append(len(body))
            items = [("d", OPERATION_ATTRS),
                     ("a", CHARSET, b"attributes-charset", b"utf-8"),
                     ("a", NATURAL_LANG, b"attributes-natural-language", b"en"),
                     ("d", JOB_ATTRS),
                     ("a", INTEGER, b"job-id", struct.pack(">i", 1)),
                     ("a", URI, b"job-uri", b"ipp://fake/jobs/1"),
                     ("a", ENUM, b"job-state", struct.pack(">i", 9)),  # completed
                     ("a", KEYWORD, b"job-state-reasons", b"job-completed-successfully"),
                     ("d", END_ATTRS)]
            resp = serialise(ver, STATUS_OK, rid, items)
            name = {OP_PRINT_JOB: "Print-Job", OP_CREATE_JOB: "Create-Job",
                    OP_SEND_DOC: "Send-Document"}[op]
            print(f"  -> {name}: ACCEPTED {len(body)} bytes  *** JOB ARRIVED ***", flush=True)
        elif op == OP_GET_JOB_ATTRS:
            items = [("d", OPERATION_ATTRS),
                     ("a", CHARSET, b"attributes-charset", b"utf-8"),
                     ("a", NATURAL_LANG, b"attributes-natural-language", b"en"),
                     ("d", JOB_ATTRS),
                     ("a", INTEGER, b"job-id", struct.pack(">i", 1)),
                     ("a", ENUM, b"job-state", struct.pack(">i", 9)),
                     ("d", END_ATTRS)]
            resp = serialise(ver, STATUS_OK, rid, items)
        else:
            resp = self._simple(ver, rid)
            print(f"  -> op 0x{op:04x} ok", flush=True)

        self.send_response(200)
        self.send_header("Content-Type", "application/ipp")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def _simple(self, ver, rid):
        items = [("d", OPERATION_ATTRS),
                 ("a", CHARSET, b"attributes-charset", b"utf-8"),
                 ("a", NATURAL_LANG, b"attributes-natural-language", b"en"),
                 ("d", END_ATTRS)]
        return serialise(ver, STATUS_OK, rid, items)


def main():
    if sys.argv[1] == "capture":
        capture(sys.argv[2], sys.argv[3],
                int(sys.argv[4]) if len(sys.argv) > 4 else 631,
                sys.argv[5] if len(sys.argv) > 5 else "/ipp/print")
        return
    raw = open(sys.argv[2], "rb").read()
    bind, port, mode = sys.argv[3], int(sys.argv[4]), sys.argv[5]
    version, status, rid, items = parse(raw)
    printer_items = []
    seen_printer = False
    for it in items:
        if it[0] == "d":
            seen_printer = it[1] == PRINTER_ATTRS
            continue
        if seen_printer:
            printer_items.append(it)
    argv = sys.argv[6:]
    if argv and argv[0] == "--from":
        donor_raw = open(argv[1], "rb").read()
        _, _, _, ditems = parse(donor_raw)
        names = set(argv[2].split(",")) if len(argv) > 2 and argv[2] else set()
        argv = argv[3:]
        dmap, cur = {}, None
        inp = False
        for it in ditems:
            if it[0] == "d":
                inp = it[1] == PRINTER_ATTRS
                continue
            if not inp:
                continue
            if it[2]:
                cur = it[2].decode(errors="replace")
                dmap[cur] = [it]
            elif cur:
                dmap[cur].append(it)
        for n in names:
            DONOR[n] = dmap.get(n, [])
        print(f"importing {len(names)} attrs from donor "
              f"({sum(1 for n in names if n in dmap)} found)")
    for spec in argv:
        n, _, v = spec.partition("=")
        OVERRIDES[n] = v.split(",") if v else ["<<DROP>>"]
    if OVERRIDES:
        print("overrides:", OVERRIDES)
    features = (["ipp-everywhere"] if mode == "everywhere"
                else ["airprint-1.6", "wfds-print-1.0"])
    my_uri = f"ipp://{bind}:{port}/ipp/print"
    Handler.ATTRS = rewrite(printer_items, features=features, my_uri=my_uri)
    print(f"serving {len(Handler.ATTRS)} attrs; ipp-features-supported = {features}")
    http.server.ThreadingHTTPServer((bind, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
