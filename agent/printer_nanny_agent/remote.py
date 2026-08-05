"""Remote hands, agent side: fetch one device page, probe writability, write.

The agent is the only thing with a route to a printer on a customer LAN, so
central hands it an instruction and it does the reaching. That makes this file
the last line of a chain whose earlier links are all on the other side of the
network, which is the reason for the rule that shapes everything below:

**An agent must be able to defend itself against a central it authenticates.**

Central already validates the address, the path, the port, the scheme and the
write operation before it ever enqueues a command. This module validates them
again anyway. That is not belt-and-braces for its own sake -- it is the same
stance ``definitions.py`` takes about signed definition feeds: a signature (or a
TLS session, or a bearer token) proves who produced bytes, never that the bytes
are safe. A central that has been compromised, or simply one running a newer
version with a bug, must not be able to turn an agent inside a customer's
network into an arbitrary HTTP client.

WHAT THE GUARDS ARE, AND WHAT EACH ONE STOPS
--------------------------------------------
* **The host must be an IP literal, and it is never resolved.** No DNS anywhere
  on this path, so there is no resolve-then-connect gap and no rebinding.
* **Loopback, link-local, multicast, unspecified and reserved are refused.**
  ``169.254.169.254`` is the cloud instance-metadata service on every major
  provider; an agent frequently runs on a VM that has one. Loopback is the agent
  host itself -- whatever is listening on 127.0.0.1 there is not a printer.
* **The port comes from a fixed allowlist.** Without it, "fetch a page from the
  printer" becomes "connect to any TCP port on that host and tell me what came
  back", which is a port scanner with an authenticated operator's blessing.
* **Redirects are never followed.** A device answering ``302 Location:
  http://169.254.169.254/`` is the textbook defeat of a pre-flight address
  check. The status and the Location are reported as data instead.
* **The body is bounded while it is read**, not after. A cap applied to a
  fully-buffered response has already paid for the response.
* **No credential of ours is ever sent, and no header of theirs is ever kept.**
  The request carries a fixed User-Agent and Accept and nothing else -- no
  cookie, no Authorization, nothing derived from the agent's own API key. Of the
  response, only the status, the content type and (for a redirect) the Location
  are recorded; ``Set-Cookie`` in particular is dropped where it arrives rather
  than travelling to central to be reasoned about later.

THE PROBE
---------
A write that changes nothing: read ``sysLocation``, SET it back to itself. An
accepted SET means this device is writable with the credentials this agent
holds. A refused one -- ``noAccess``, ``notWritable``, ``authorizationError``,
or no answer at all -- means it is not, which on a Secure-by-Default HP is the
correct and expected answer rather than a failure.

If neither probe OID can be read, the verdict is *no verdict*: the request fails
with a reason and central leaves capability exactly as it found it. Reporting
"read-only" when we could not ask is the failure mode this whole feature is
arranged to avoid -- an inference wearing an observation's clothes.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Dict, Optional, Tuple

import httpx

from printer_nanny_agent.snmp import SnmpBackend, SnmpError, SnmpParams

log = logging.getLogger("printer_nanny_agent.remote")

#: Mirrors ``central.remote.MAX_BODY_BYTES``. Two copies is a real cost, and the
#: alternative -- letting central push the cap down in the command payload --
#: would mean the bound is chosen by the party we are bounding.
MAX_BODY_BYTES = 256 * 1024

#: Mirrors ``central.remote.ALLOWED_PORTS``, for the same reason.
ALLOWED_PORTS = (80, 443, 8000, 8080, 8443)

#: sysLocation, then sysContact -- both RFC 1213 read-write scalars.
PROBE_OIDS = ("1.3.6.1.2.1.1.6.0", "1.3.6.1.2.1.1.4.0")

FETCH_TIMEOUT_SECONDS = 12.0

#: Fixed, and it says what we are. A device's log showing "Printer Nanny agent"
#: against a request an operator made is the point.
USER_AGENT = "PrinterNannyAgent/remote-hands"


class RemoteRefused(Exception):
    """This agent will not carry out the instruction it was given.

    Distinct from a failure to reach the device: one is "we declined", the other
    is "we tried and the device did not answer", and an operator needs to be
    able to tell them apart.
    """


def check_address(ip: str) -> str:
    """Refuse any address this agent must not be aimed at. Returns it normalised."""
    raw = (ip or "").strip()
    if not raw:
        raise RemoteRefused("no address supplied")
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        raise RemoteRefused("%r is not an IP address; remote hands never resolves names" % raw)
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        # ::ffff:127.0.0.1 is loopback wearing IPv6 clothing, and
        # IPv6Address.is_loopback is False for it.
        addr = mapped
    if addr.is_loopback:
        raise RemoteRefused("%s is loopback -- that is this host, not a printer" % addr)
    if addr.is_unspecified:
        raise RemoteRefused("%s is the unspecified address" % addr)
    if addr.is_link_local:
        raise RemoteRefused(
            "%s is link-local (this range carries cloud instance metadata)" % addr
        )
    if addr.is_multicast:
        raise RemoteRefused("%s is a multicast address" % addr)
    if addr.is_reserved:
        raise RemoteRefused("%s is an IETF-reserved address" % addr)
    return str(addr)


def check_port(port) -> int:
    try:
        value = int(port)
    except (TypeError, ValueError):
        raise RemoteRefused("port %r is not a number" % (port,))
    if value not in ALLOWED_PORTS:
        raise RemoteRefused("port %d is not an allowed embedded-web-server port" % value)
    return value


def check_path(path: str) -> str:
    raw = (path or "/").strip() or "/"
    if not raw.startswith("/"):
        raise RemoteRefused("path must start with '/'")
    if raw.startswith("//"):
        raise RemoteRefused("a path starting '//' would change the host")
    if len(raw) > 512:
        raise RemoteRefused("path is too long")
    if any(ord(c) < 0x21 or ord(c) == 0x7F for c in raw):
        raise RemoteRefused("path contains whitespace or control characters")
    return raw


def check_scheme(scheme: str) -> str:
    value = (scheme or "http").strip().lower()
    if value not in ("http", "https"):
        raise RemoteRefused("scheme must be http or https")
    return value


def _decode(content: bytes, content_type: str) -> str:
    """Decode a device response to text, never raising.

    The declared charset is tried first and the bytes win on disagreement: this
    has to produce *something* renderable for every possible device, and a
    UnicodeDecodeError here would turn a diagnostic into an outage of the
    diagnostic. ``errors="replace"`` is the honest choice -- a replacement
    character visibly says "these bytes were not what the device claimed".
    """
    charset = ""
    for part in (content_type or "").split(";")[1:]:
        name, _, value = part.strip().partition("=")
        if name.strip().lower() == "charset":
            charset = value.strip().strip('"').strip("'")
            break
    for candidate in (charset, "utf-8"):
        if not candidate:
            continue
        try:
            return content.decode(candidate, errors="replace")
        except LookupError:
            continue
    return content.decode("utf-8", errors="replace")


async def fetch_page(payload: dict) -> dict:
    """GET one page from a device's embedded web server. Returns a result dict.

    Never raises for a device-side outcome: an unreachable printer, a TLS error
    and a 500 are all things the operator asked to find out, so they come back as
    a stated result rather than an exception. Only a refusal (the guards above)
    is exceptional, and even that is caught by the caller and reported.
    """
    ip = check_address(payload.get("ip"))
    scheme = check_scheme(payload.get("scheme"))
    port = check_port(payload.get("port"))
    path = check_path(payload.get("path"))

    # IPv6 literals need brackets in a URL authority.
    host = "[%s]" % ip if ":" in ip else ip
    url = "%s://%s:%d%s" % (scheme, host, port, path)

    detail: Dict[str, object] = {"url": url}
    try:
        # verify=False: printer web servers ship self-signed certificates
        # universally, and refusing them would make https unusable against every
        # real device. This is not a downgrade of anything -- the content is
        # treated as hostile either way, which is the only assumption that
        # survives a self-signed cert.
        #
        # follow_redirects=False is load-bearing, not a default we inherited.
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_SECONDS, verify=False, follow_redirects=False
        ) as client:
            async with client.stream(
                "GET",
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
            ) as resp:
                content_type = resp.headers.get("content-type", "")[:120]
                if resp.is_redirect:
                    detail["location"] = (resp.headers.get("location") or "")[:400]
                    detail["redirect_not_followed"] = True
                chunks = []
                total = 0
                truncated = False
                async for chunk in resp.aiter_bytes():
                    remaining = MAX_BODY_BYTES - total
                    if remaining <= 0:
                        truncated = True
                        break
                    if len(chunk) > remaining:
                        chunks.append(chunk[:remaining])
                        total += remaining
                        truncated = True
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                raw = b"".join(chunks)
                return {
                    "ok": True,
                    "http_status": resp.status_code,
                    "content_type": content_type,
                    "body": _decode(raw, content_type),
                    "body_bytes": total,
                    "truncated": truncated,
                    "detail": detail,
                }
    except (httpx.HTTPError, OSError) as exc:
        # The exception text can quote the URL, which is fine here (it is the
        # printer's own address, and the operator supplied the path) but is
        # bounded so a verbose transport error cannot become the payload.
        return {
            "ok": False,
            "error": ("could not reach %s: %s" % (url, exc))[:600],
            "detail": detail,
        }


async def probe_writable(
    backend: SnmpBackend, ip: str, params: SnmpParams
) -> dict:
    """Establish whether this device accepts a write, by making one that changes nothing."""
    ip = check_address(ip)
    read_oid: Optional[str] = None
    current: Optional[str] = None
    read_errors = []
    for oid in PROBE_OIDS:
        try:
            values = await backend.get(ip, [oid], params)
        except SnmpError as exc:
            read_errors.append("%s: %s" % (oid, exc))
            continue
        value = values.get(oid)
        if value is not None:
            read_oid, current = oid, value
            break
        read_errors.append("%s: not present" % oid)

    if read_oid is None:
        # NO VERDICT. Central leaves capability untouched -- see
        # services.apply_remote_result. "We could not ask" must never be
        # recorded as "the answer is no".
        return {
            "ok": False,
            "error": ("could not read a writable probe OID from %s (%s)"
                      % (ip, "; ".join(read_errors)))[:600],
            "detail": {"read_errors": read_errors[:4]},
        }

    try:
        echoed = await backend.set(ip, read_oid, current or "", "string", params)
    except SnmpError as exc:
        return {
            "ok": True,
            "writable": False,
            "error": ("the device refused a no-op write of %s: %s" % (read_oid, exc))[:600],
            "detail": {"probe_oid": read_oid},
        }
    return {
        "ok": True,
        "writable": True,
        "detail": {"probe_oid": read_oid, "echoed": str(echoed)[:200]},
    }


async def perform_write(
    backend: SnmpBackend, payload: dict, params: SnmpParams
) -> dict:
    """Carry out one named write, and read it back where a read-back means anything.

    A SET that returns without error is not evidence the device applied it --
    the same lesson ``SetDefaultPrinter`` returning non-zero taught on Windows
    and ``lpoptions -d`` taught on macOS. So a verifiable write reads the OID
    back and compares, and reports ``verified`` honestly either way.

    A restart has nothing to read back (the device is rebooting), so it reports
    ``verified: None`` -- explicitly "there was nothing to compare", never
    ``True``. The UI says "issued" for that case rather than "confirmed".
    """
    ip = check_address(payload.get("ip"))
    oid = str(payload.get("oid") or "")
    value = str(payload.get("value") or "")
    snmp_type = str(payload.get("snmp_type") or "string")
    verify = bool(payload.get("verify"))

    # The OID is re-checked here rather than trusted from the payload: this is
    # the character loop the definitions validator uses, for the same reason --
    # it is the gate that keeps anything that is not an OID out of an SNMP call.
    if not oid or not all(c.isdigit() or c == "." for c in oid) or ".." in oid:
        raise RemoteRefused("%r is not a valid OID" % oid)
    if snmp_type not in ("string", "int"):
        raise RemoteRefused("unsupported write type %r" % snmp_type)

    try:
        echoed = await backend.set(ip, oid, value, snmp_type, params)
    except SnmpError as exc:
        return {
            "ok": False,
            "writable": False,
            "error": ("the device refused the write: %s" % exc)[:600],
            "detail": {"oid": oid},
        }

    result = {
        "ok": True,
        "writable": True,
        "verified": None,
        "detail": {"oid": oid, "echoed": str(echoed)[:200]},
    }
    if not verify:
        return result
    try:
        read_back = (await backend.get(ip, [oid], params)).get(oid)
    except SnmpError as exc:
        result["verified"] = False
        result["error"] = ("wrote it, but could not read it back to confirm: %s" % exc)[:600]
        return result
    result["verified"] = (read_back or "") == value
    result["detail"]["read_back"] = str(read_back)[:200]
    if not result["verified"]:
        result["error"] = (
            "the device accepted the write but reports %r afterwards" % (read_back,)
        )[:600]
    return result


def result_for_exception(exc: Exception) -> Tuple[dict, str]:
    """Turn a refusal or an unexpected error into a reportable result.

    Everything reports. A remote request that produced no answer at all leaves
    an operator watching a spinner and guessing, which is the same failure as a
    queue that silently skips a printer: absence read as success.
    """
    if isinstance(exc, RemoteRefused):
        return {"ok": False, "error": ("refused by the agent: %s" % exc)[:600]}, "refused"
    return {"ok": False, "error": ("the agent could not carry this out: %s" % exc)[:600]}, "error"
