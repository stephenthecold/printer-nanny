"""Create and reconcile Windows print queues, without ever prompting the user.

WHY THIS EXISTS
---------------
Driver installation is what strands most workstation setups. Since KB5005652
(Aug 2021) ``RestrictDriverInstallationToAdministrators`` defaults to 1, so
Point and Print demands local admin: the user gets a UAC prompt they cannot
satisfy, and the usual escape -- setting that key to 0 -- reopens PrintNightmare.

So this never uses Point and Print. It runs inside a service under LocalSystem
and provisions the queue itself, which is why the user is never prompted and
never needs rights. Two paths, chosen by the tier the IPP probe recorded:

``driverless``       hand the IPP URL to ``Add-Printer -IppURL`` and let
                     Windows discover the device, choose the class driver and
                     create the port. Nothing is installed. Preferred -- as of
                     2026-07-01 Windows ranks that driver ahead of third-party
                     ones by default. We do NOT create the port ourselves:
                     ``Add-PrinterPort`` can only make RAW/LPR ports, which
                     cannot carry IPP. See ``ensure_driverless_queue``.
``driver_required``  stage a vendor driver into the DriverStore with pnputil,
                     register it, then build a TCP/IP queue on it.

NOTHING IS INTERPOLATED INTO A SHELL
------------------------------------
Printer names, locations and comments originate from devices on customer LANs
via SNMP/IPP, and from operator free-text. They are hostile input. A queue named
``x"; Remove-Item -Recurse C:\\ #`` must be inert.

So no value is ever formatted into the PowerShell text. Scripts are constant
strings; values travel as **environment variables** and are read inside the
script as ``$env:PN_NAME``. PowerShell does no further parsing of an environment
variable's contents, so quoting cannot be escaped out of. ``build_env`` is the
only place values cross the boundary, and the script bodies below contain no
``{}`` or ``%s``.

IDEMPOTENCE IS THE CONTRACT
---------------------------
The client re-runs this on every assignment change, every service start, and
after every central poll. Creating a queue that already exists must be a no-op,
not an error, and a queue whose port or driver has drifted must be corrected
rather than duplicated. Every operation here is therefore written as
"inspect, then converge".
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
from typing import Dict, List, Optional, Sequence

log = logging.getLogger(__name__)

#: The inbox class driver. Present on Windows 10 21H2+ / Server 2022+, and the
#: default-ranked driver since 2026-07-01. Named exactly as Windows registers it.
IPP_CLASS_DRIVER = "Microsoft IPP Class Driver"

#: Environment prefix for values handed to PowerShell. Namespaced so a script
#: cannot accidentally read an unrelated variable from the service's environment.
ENV_PREFIX = "PN_"

DEFAULT_TIMEOUT_S = 120


class PowerShellError(RuntimeError):
    """A PowerShell invocation failed. Carries stderr for diagnostics."""

    def __init__(self, message: str, returncode: int, stdout: str, stderr: str) -> None:
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class PowerShellRunner:
    """The seam between our logic and the Windows spooler.

    Kept deliberately thin: everything above it is pure and unit-testable on any
    platform, and only this class needs a Windows host to exercise. Tests
    substitute a fake runner.
    """

    def __init__(self, executable: Optional[str] = None, timeout_s: int = DEFAULT_TIMEOUT_S) -> None:
        self.executable = executable or self._find_powershell()
        self.timeout_s = timeout_s

    @staticmethod
    def _find_powershell() -> str:
        # pwsh (7+) first where present, else Windows PowerShell 5.1, which is
        # what every supported Server build ships with.
        for candidate in ("pwsh", "powershell"):
            found = shutil.which(candidate)
            if found:
                return found
        return "powershell"

    def run(self, script: str, variables: Optional[Dict[str, str]] = None) -> str:
        """Execute ``script``, exposing ``variables`` as $env:PN_* inside it.

        The script is delivered **base64-encoded in one argv element**, and its
        text is one of this module's constants -- fixed at import time, so no
        caller-supplied string can extend it. Caller values never appear in the
        script at all; they arrive by environment.

        Failures are detected from the *script*, not from the process exit code
        alone. PowerShell exits 0 even when a cmdlet throws, including under
        ``$ErrorActionPreference = 'Stop'`` -- so trusting returncode meant a
        failed Add-Printer was reported as success and the caller happily
        recorded "created" for a queue that does not exist. The Windows CI job
        caught exactly that; a fake runner never could.

        So the script is wrapped in try/catch with an explicit ``exit 1``, and
        native executables (pnputil) are checked via $LASTEXITCODE. The wrapper
        composes our own constant scripts -- never caller data, which still
        travels only by environment.
        """
        env = os.environ.copy()
        env.update(build_env(variables or {}))
        # -NoProfile: a machine profile must not change our behaviour.
        # -NonInteractive: never block a service on a prompt.
        # -EncodedCommand: the script as base64 UTF-16LE in a single argv slot.
        #
        # NOT `-Command -` on stdin: that parses input line by line, so a
        # multi-line construct is incomplete on its first line and the script
        # silently produces nothing. That regression was real -- wrapping the
        # scripts in try/catch made every call return empty output until this
        # changed. Encoding sidesteps command-line parsing entirely, which also
        # means newlines, quotes and braces survive exactly as written.
        argv = [
            self.executable,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-EncodedCommand", encode_command(wrap_script(script)),
        ]
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, no interpolation
            argv,
            capture_output=True,
            text=True,
            timeout=self.timeout_s,
            env=env,
            shell=False,
        )
        stdout = proc.stdout or ""
        if proc.returncode != 0 or _FAILURE_MARKER in stdout:
            raise PowerShellError(
                "powershell failed (exit {})".format(proc.returncode),
                proc.returncode,
                stdout.replace(_FAILURE_MARKER, "").strip(),
                (proc.stderr or "").strip()[:2000],
            )
        return stdout


#: Emitted by the wrapper when the script threw. Belt-and-braces alongside the
#: exit code, because PowerShell hosts have historically been unreliable about
#: propagating `exit` out of a non-interactive invocation -- and a runner that
#: reports success on failure is worse than no runner at all.
_FAILURE_MARKER = "__PN_SCRIPT_FAILED__"

_WRAP_HEAD = "$ErrorActionPreference = 'Stop'\n$ProgressPreference = 'SilentlyContinue'\ntry {\n"
_WRAP_TAIL = (
    "\n}\ncatch {\n"
    "  [Console]::Error.WriteLine($_.Exception.Message)\n"
    "  Write-Output '" + _FAILURE_MARKER + "'\n"
    "  exit 1\n"
    "}\n"
    # A native executable (pnputil) that fails does not throw, so its exit code
    # has to be inspected separately or a failed driver stage looks like success.
    "if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {\n"
    "  [Console]::Error.WriteLine('native command exited ' + $LASTEXITCODE)\n"
    "  Write-Output '" + _FAILURE_MARKER + "'\n"
    "  exit $LASTEXITCODE\n"
    "}\n"
)


def encode_command(script: str) -> str:
    """Base64 UTF-16LE, the encoding -EncodedCommand expects.

    Carries an arbitrary multi-line script through a single argv element with no
    shell or command-line parsing in between. Only our own constant scripts are
    encoded; caller values still travel by environment.
    """
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def wrap_script(script: str) -> str:
    """Wrap one of our constant scripts so a thrown error becomes a failure.

    Only ever applied to module-level script constants. Caller data never
    appears here -- it travels by environment, which is what keeps the
    composition safe.
    """
    return _WRAP_HEAD + script + _WRAP_TAIL


def build_env(variables: Dict[str, str]) -> Dict[str, str]:
    """Namespace and stringify values for the PowerShell environment.

    This is the ONLY place caller data crosses into the shell's world, and it
    crosses as an environment value rather than as script text -- which is what
    makes the injection question moot rather than merely handled.
    """
    out: Dict[str, str] = {}
    for key, value in variables.items():
        if value is None:
            continue
        name = ENV_PREFIX + key.upper()
        # NUL cannot appear in an environment value on any platform; strip it
        # rather than letting the subprocess layer raise on it.
        out[name] = str(value).replace("\x00", "")
    return out


# --- scripts ----------------------------------------------------------------
# Constant strings. No formatting, no f-strings, no .format(). Values arrive as
# $env:PN_*. Keep it that way -- see the module docstring.

_SCRIPT_QUEUE_EXISTS = """
$ErrorActionPreference = 'Stop'
$p = Get-Printer -Name $env:PN_NAME -ErrorAction SilentlyContinue
if ($null -eq $p) { 'ABSENT' } else { 'PRESENT|' + $p.PortName + '|' + $p.DriverName }
"""

_SCRIPT_PORT_EXISTS = """
$ErrorActionPreference = 'Stop'
$port = Get-PrinterPort -Name $env:PN_PORT -ErrorAction SilentlyContinue
if ($null -eq $port) { 'ABSENT' } else { 'PRESENT' }
"""

_SCRIPT_ADD_TCP_PORT = """
$ErrorActionPreference = 'Stop'
if ($null -eq (Get-PrinterPort -Name $env:PN_PORT -ErrorAction SilentlyContinue)) {
  Add-PrinterPort -Name $env:PN_PORT -PrinterHostAddress $env:PN_HOST
}
'OK'
"""

_SCRIPT_ADD_QUEUE = """
$ErrorActionPreference = 'Stop'
Add-Printer -Name $env:PN_NAME -DriverName $env:PN_DRIVER -PortName $env:PN_PORT
'OK'
"""

_SCRIPT_SET_QUEUE = """
$ErrorActionPreference = 'Stop'
Set-Printer -Name $env:PN_NAME -DriverName $env:PN_DRIVER -PortName $env:PN_PORT
'OK'
"""

_SCRIPT_REMOVE_QUEUE = """
$ErrorActionPreference = 'Stop'
if ($null -ne (Get-Printer -Name $env:PN_NAME -ErrorAction SilentlyContinue)) {
  Remove-Printer -Name $env:PN_NAME
}
'OK'
"""

_SCRIPT_SET_COMMENT = """
$ErrorActionPreference = 'Stop'
Set-Printer -Name $env:PN_NAME -Comment $env:PN_COMMENT -Location $env:PN_LOCATION
'OK'
"""

_SCRIPT_DRIVER_PRESENT = """
$ErrorActionPreference = 'Stop'
$d = Get-PrinterDriver -Name $env:PN_DRIVER -ErrorAction SilentlyContinue
if ($null -eq $d) { 'ABSENT' } else { 'PRESENT' }
"""

_SCRIPT_STAGE_DRIVER = """
$ErrorActionPreference = 'Stop'
# Stage the package into the DriverStore. This is the step Point and Print
# would have demanded admin for; we are already SYSTEM, so no prompt appears.
& pnputil.exe /add-driver $env:PN_INF /install | Out-String
Add-PrinterDriver -Name $env:PN_DRIVER
'OK'
"""

_SCRIPT_LIST_QUEUES = """
$ErrorActionPreference = 'Stop'
Get-Printer | ForEach-Object { $_.Name + '|' + $_.PortName + '|' + $_.DriverName }
"""

# --- tier 1: driverless, via Windows' own directed IPP discovery -------------
# Add-Printer's -IppURL belongs to a parameter set that is DISJOINT from
# -DriverName / -PortName. Windows performs the IPP query itself, selects the
# class driver, and creates a port of its own choosing. So there is nothing for
# us to name here, and passing a driver or port name alongside -IppURL does not
# merely get ignored -- PowerShell fails to bind the parameters at all.

_SCRIPT_IPPURL_SUPPORTED = """
$ErrorActionPreference = 'Stop'
if ((Get-Command Add-Printer).Parameters.ContainsKey('IppURL')) { 'YES' } else { 'NO' }
"""

_SCRIPT_ADD_IPP_QUEUE = """
$ErrorActionPreference = 'Stop'
if ($null -eq (Get-Printer -Name $env:PN_NAME -ErrorAction SilentlyContinue)) {
  Add-Printer -Name $env:PN_NAME -IppURL $env:PN_IPPURL
}
$p = Get-Printer -Name $env:PN_NAME
'OK|' + $p.PortName + '|' + $p.DriverName
"""

#: Read a port's monitor so a queue can be checked for the failure this whole
#: rework exists to prevent: a Standard TCP/IP (RAW 9100) port with an IPP class
#: driver bolted on, which is created successfully and can never print.
_SCRIPT_PORT_DETAIL = """
$ErrorActionPreference = 'Stop'
$p = Get-PrinterPort -Name $env:PN_PORT -ErrorAction SilentlyContinue
if ($null -eq $p) { 'ABSENT' }
else { 'PRESENT|' + [string]$p.Description + '|' + [string]$p.PrinterHostAddress }
"""


# --- pure helpers -----------------------------------------------------------


def port_name_for(uri_or_host: str) -> str:
    """Derive a stable port name.

    Deterministic so a re-run finds the port it made last time rather than
    accumulating one port per run -- the most common way naive provisioning
    leaves a workstation with forty dead ports.
    """
    cleaned = uri_or_host.strip()
    for prefix in ("ipp://", "ipps://", "http://", "https://"):
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    return "PN_" + cleaned.replace("/", "_").replace(":", "_").strip("_")


class QueueState:
    """What Windows currently has for one queue name."""

    __slots__ = ("present", "port", "driver")

    def __init__(self, present: bool, port: str = "", driver: str = "") -> None:
        self.present = present
        self.port = port
        self.driver = driver

    def matches(self, port: str, driver: str) -> bool:
        return self.present and self.port == port and self.driver == driver

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "QueueState(present={!r}, port={!r}, driver={!r})".format(
            self.present, self.port, self.driver
        )


def parse_queue_state(output: str) -> QueueState:
    """Decode _SCRIPT_QUEUE_EXISTS output."""
    text = (output or "").strip()
    if not text or text.startswith("ABSENT"):
        return QueueState(False)
    parts = text.split("|")
    if parts[0] != "PRESENT":
        return QueueState(False)
    return QueueState(True, parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else "")


def parse_queue_list(output: str) -> List[Dict[str, str]]:
    rows = []
    for line in (output or "").splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|")
        rows.append(
            {
                "name": parts[0],
                "port": parts[1] if len(parts) > 1 else "",
                "driver": parts[2] if len(parts) > 2 else "",
            }
        )
    return rows


# --- operations -------------------------------------------------------------


def queue_state(runner: PowerShellRunner, name: str) -> QueueState:
    return parse_queue_state(runner.run(_SCRIPT_QUEUE_EXISTS, {"name": name}))


def list_queues(runner: PowerShellRunner) -> List[Dict[str, str]]:
    return parse_queue_list(runner.run(_SCRIPT_LIST_QUEUES))


def driver_present(runner: PowerShellRunner, driver: str) -> bool:
    return runner.run(_SCRIPT_DRIVER_PRESENT, {"driver": driver}).strip().startswith("PRESENT")


#: Name of the Standard TCP/IP port monitor, as Windows reports it. A tier-1
#: queue sitting on this monitor is the defect this module was rewritten to
#: prevent -- see ensure_driverless_queue.
STANDARD_TCP_MONITOR = "standard tcp/ip"


class IppProvisionError(RuntimeError):
    """Tier 1 could not be provisioned.

    ``retryable`` distinguishes "the printer was asleep" from "this will never
    work", because central must be able to say which. A device that is merely
    off gets tried again; a machine whose Windows build has no -IppURL never
    will, and telling those apart is the difference between a self-healing
    fleet and a technician dispatched for nothing.
    """

    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def ippurl_supported(runner: PowerShellRunner) -> bool:
    """Does this Windows build's Add-Printer offer -IppURL?

    Present in the Server 2022 and 2025 cmdlet sets, absent in 2019 (whose
    second parameter set is WSD-only). Probed rather than inferred from an OS
    version string, because the cmdlet is the thing that actually has to work.
    """
    return runner.run(_SCRIPT_IPPURL_SUPPORTED).strip().upper().startswith("YES")


def port_detail(runner: PowerShellRunner, port: str) -> Dict[str, str]:
    """Monitor description + host address for a port, or ``{}`` when absent."""
    text = runner.run(_SCRIPT_PORT_DETAIL, {"port": port}).strip()
    if not text or text.startswith("ABSENT"):
        return {}
    parts = text.split("|")
    return {
        "description": parts[1] if len(parts) > 1 else "",
        "host_address": parts[2] if len(parts) > 2 else "",
    }


def ensure_driverless_queue(
    runner: PowerShellRunner,
    name: str,
    ipp_uri: str,
    comment: str = "",
    location: str = "",
) -> str:
    """Tier 1: let Windows discover the device over IPP and bind its class driver.

    WHY THIS DOES NOT CREATE A PORT
    -------------------------------
    It used to, and that was wrong in a way nothing here could catch. The old
    path derived a port name from the URI and ran::

        Add-PrinterPort -Name <derived> -PrinterHostAddress ipp://host:631/ipp/print
        Add-Printer -Name <name> -DriverName "Microsoft IPP Class Driver" -PortName <derived>

    ``Add-PrinterPort`` has four parameter sets -- local, tcp, tcplpr, lpr --
    and **none of them makes an IPP port**. The Standard TCP/IP monitor speaks
    only RAW or LPR, default TCP 9100. So the best case was a RAW:9100 port with
    an IPP class driver bolted onto it: a queue that is created, appears in
    ``Get-Printer``, satisfies every convergence check, and cannot print. That is
    precisely the "central shows provisioned, the user has nothing to print to"
    failure this module's docstring says must never happen.

    ``Add-Printer -IppURL`` is the supported path. Windows performs the IPP query
    itself, picks the class driver and creates a port of its own choosing --
    which is why ``-IppURL`` is in a parameter set disjoint from ``-DriverName``
    and ``-PortName``. We were not passing a bad value; we were using the wrong
    cmdlet shape.

    CONSEQUENCES WORTH KNOWING
    --------------------------
    * **Windows names the port, so we cannot predict it.** Tier 1 therefore does
      not share ``_converge`` -- that compares against a derived port name, which
      here would never match, so every poll would re-run ``Add-Printer``. State
      is read back from the queue instead of assumed.
    * **This touches the network.** The old path never did, so a sleeping printer
      is a new failure mode; it raises ``IppProvisionError(retryable=True)``
      rather than being recorded as broken.
    * **There is no fallback to a TCP port.** Falling back would reintroduce
      exactly the silent breakage above. Failing loudly is the point.

    Returns ``created`` or ``unchanged``.
    """
    if not ipp_uri or "://" not in ipp_uri:
        raise IppProvisionError(
            "tier 1 needs a full IPP URL (got {!r}); a bare host cannot be "
            "discovered over IPP".format(ipp_uri)
        )

    if not ippurl_supported(runner):
        raise IppProvisionError(
            "this Windows build's Add-Printer has no -IppURL, so driverless "
            "provisioning is unavailable (documented for Server 2022/2025; "
            "absent on 2019). Refusing to fall back to a RAW TCP port, which "
            "would create a queue that cannot print.",
            retryable=False,
        )

    if not driver_present(runner, IPP_CLASS_DRIVER):
        # Tier 3 checks its driver; tier 1 never did, so a machine missing the
        # inbox driver failed with a bare cmdlet error instead of a state an
        # operator could act on.
        raise IppProvisionError(
            "the inbox {!r} is not registered on this machine".format(IPP_CLASS_DRIVER),
            retryable=False,
        )

    state = queue_state(runner, name)
    existed = state.present
    if existed:
        detail = port_detail(runner, state.port) if state.port else {}
        on_tcp = STANDARD_TCP_MONITOR in (detail.get("description") or "").lower()
        driver_ok = (state.driver or "").strip() == IPP_CLASS_DRIVER

        if driver_ok and not on_tcp:
            # Converged. Deliberately WITHOUT calling Add-Printer -IppURL again:
            # that is a live network query, and this runs on every assignment
            # change, service start and poll. Re-querying would mean an IPP round
            # trip per printer per poll, and would make a sleeping printer fail a
            # queue that already works perfectly well.
            return "unchanged"

        # Wrong driver, or -- the case that matters for anyone who ran the older
        # build -- a queue the previous code left sitting on a RAW:9100 Standard
        # TCP/IP port. Neither can be repaired in place: for tier 1 Windows owns
        # the port, so there is no Set-Printer that can move it onto a real IPP
        # one. Removing and letting discovery rebuild is the only correct repair,
        # and it is what silently converts those broken queues into working ones.
        log.info(
            "rebuilding tier-1 queue %r (driver=%r standard_tcp_port=%s)",
            name, state.driver, on_tcp,
        )
        remove_queue(runner, name)

    try:
        out = runner.run(_SCRIPT_ADD_IPP_QUEUE, {"name": name, "ippurl": ipp_uri})
    except PowerShellError as exc:
        detail = ((exc.stderr or "") + " " + (exc.stdout or "")).lower()
        # A device that is asleep, powered off or firewalled is a different
        # problem from a machine that can never do this, and only the first is
        # worth retrying.
        retryable = any(
            token in detail
            for token in ("timed out", "timeout", "unreachable", "no response",
                          "could not be found", "network path")
        )
        raise IppProvisionError(
            "Add-Printer -IppURL failed for {}: {}".format(
                ipp_uri, (exc.stderr or exc.stdout or "").strip()[:400]
            ),
            retryable=retryable,
        ) from exc

    parts = out.strip().split("|")
    got_port = parts[1] if len(parts) > 1 else ""
    got_driver = parts[2] if len(parts) > 2 else ""

    # The guard that makes the failure above impossible to reintroduce quietly:
    # if what we ended up with is a Standard TCP/IP port, the queue cannot speak
    # IPP no matter what the driver says, so it is an error rather than a queue.
    if got_port:
        detail = port_detail(runner, got_port)
        description = (detail.get("description") or "").lower()
        if STANDARD_TCP_MONITOR in description:
            raise IppProvisionError(
                "queue {!r} was created on a Standard TCP/IP port ({!r}), which "
                "speaks only RAW/LPR and cannot carry IPP -- refusing to report "
                "this as provisioned".format(name, got_port),
                retryable=False,
            )

    if got_driver and got_driver.strip() != IPP_CLASS_DRIVER:
        log.warning(
            "queue %r bound %r rather than %r; Windows chose the driver",
            name, got_driver.strip(), IPP_CLASS_DRIVER,
        )

    if comment or location:
        runner.run(
            _SCRIPT_SET_COMMENT,
            {"name": name, "comment": comment, "location": location},
        )
    return "updated" if existed else "created"


def ensure_vendor_queue(
    runner: PowerShellRunner,
    name: str,
    host: str,
    driver: str,
    inf_path: Optional[str] = None,
    comment: str = "",
    location: str = "",
) -> str:
    """Tier 3: stage a vendor driver if needed, then build a TCP/IP queue.

    ``inf_path`` is staged only when the driver is not already registered --
    re-staging on every run is slow and churns the DriverStore for no gain.
    """
    if not driver_present(runner, driver):
        if not inf_path:
            raise PowerShellError(
                "driver {!r} is not installed and no INF was supplied".format(driver),
                0, "", "",
            )
        runner.run(_SCRIPT_STAGE_DRIVER, {"inf": inf_path, "driver": driver})

    port = port_name_for(host)
    return _converge(runner, name, port, driver, host, comment, location)


def _converge(
    runner: PowerShellRunner,
    name: str,
    port: str,
    driver: str,
    host: str,
    comment: str,
    location: str,
) -> str:
    """Inspect, then make reality match -- never blindly create.

    Blind Add-Printer throws when the queue exists, which on a client that
    re-runs constantly would mean either a crash loop or a swallowed exception
    that hides genuine failures. Converging also repairs a queue whose port or
    driver drifted (device re-addressed, driver replaced) instead of leaving a
    broken queue beside a new one.
    """
    state = queue_state(runner, name)
    if state.matches(port, driver):
        result = "unchanged"
    else:
        runner.run(_SCRIPT_ADD_TCP_PORT, {"port": port, "host": host})
        if state.present:
            runner.run(_SCRIPT_SET_QUEUE, {"name": name, "driver": driver, "port": port})
            result = "updated"
        else:
            runner.run(_SCRIPT_ADD_QUEUE, {"name": name, "driver": driver, "port": port})
            result = "created"

    if comment or location:
        runner.run(
            _SCRIPT_SET_COMMENT,
            {"name": name, "comment": comment, "location": location},
        )
    return result


def remove_queue(runner: PowerShellRunner, name: str) -> None:
    """Remove a queue if present. Absent is success, not an error.

    Deprovisioning runs whenever an assignment is withdrawn, and a queue an
    operator already deleted by hand must not fail the whole reconcile pass.
    """
    runner.run(_SCRIPT_REMOVE_QUEUE, {"name": name})


def reconcile(
    runner: PowerShellRunner,
    desired: Sequence[dict],
    managed_prefix: str = "",
) -> Dict[str, str]:
    """Make the machine's queues match ``desired``.

    Each entry: ``{"name", "tier", "uri"|"host", "driver"?, "inf"?, "comment"?,
    "location"?}``.

    Only queues we created are ever removed. ``managed_prefix`` scopes that:
    without it this would happily delete the user's own locally-added printers,
    which is the kind of thing that gets a print-management tool uninstalled.
    An empty prefix therefore disables removal entirely rather than defaulting
    to "delete everything unrecognised".
    """
    outcomes: Dict[str, str] = {}
    wanted = set()

    for spec in desired:
        name = spec["name"]
        wanted.add(name)
        try:
            if spec.get("tier") == "driverless":
                outcomes[name] = ensure_driverless_queue(
                    runner, name, spec["uri"],
                    spec.get("comment", ""), spec.get("location", ""),
                )
            else:
                outcomes[name] = ensure_vendor_queue(
                    runner, name, spec["host"], spec["driver"], spec.get("inf"),
                    spec.get("comment", ""), spec.get("location", ""),
                )
        except (PowerShellError, IppProvisionError) as exc:
            # One bad queue must not abort the rest of the reconcile. The
            # failure is recorded per-queue so central can show which printer
            # failed and why, rather than "provisioning failed".
            log.warning("queue %s failed: %s", name, exc)
            outcomes[name] = "error: {}".format(exc)

    if managed_prefix:
        for row in list_queues(runner):
            if row["name"].startswith(managed_prefix) and row["name"] not in wanted:
                try:
                    remove_queue(runner, row["name"])
                    outcomes[row["name"]] = "removed"
                except PowerShellError as exc:
                    outcomes[row["name"]] = "error: {}".format(exc)

    return outcomes
