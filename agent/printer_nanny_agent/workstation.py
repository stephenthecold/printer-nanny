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

``driverless``       bind the Windows inbox IPP class driver to an IPP port.
                     Nothing is installed. Preferred -- as of 2026-07-01 Windows
                     ranks that driver ahead of third-party ones by default.
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

        The script is fed on **stdin**, never as an argv element, so its text is
        fixed at import time and no caller-supplied string can extend it.
        """
        env = os.environ.copy()
        env.update(build_env(variables or {}))
        # -NoProfile: a machine profile must not change our behaviour.
        # -NonInteractive: never block a service on a prompt.
        # -Command -: read the script from stdin.
        argv = [
            self.executable,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-Command", "-",
        ]
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, no interpolation
            argv,
            input=script,
            capture_output=True,
            text=True,
            timeout=self.timeout_s,
            env=env,
            shell=False,
        )
        if proc.returncode != 0:
            raise PowerShellError(
                "powershell exited {}".format(proc.returncode),
                proc.returncode,
                proc.stdout,
                proc.stderr.strip()[:2000],
            )
        return proc.stdout


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


def ensure_driverless_queue(
    runner: PowerShellRunner,
    name: str,
    ipp_uri: str,
    comment: str = "",
    location: str = "",
) -> str:
    """Tier 1: bind the inbox IPP class driver. Installs nothing.

    Returns one of ``created`` / ``updated`` / ``unchanged`` so the caller can
    log what actually happened instead of asserting success blindly.
    """
    port = port_name_for(ipp_uri)
    return _converge(runner, name, port, IPP_CLASS_DRIVER, ipp_uri, comment, location)


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
        except PowerShellError as exc:
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
