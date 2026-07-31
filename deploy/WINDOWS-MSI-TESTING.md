# Windows MSI — build & test procedure

Central builds **two** self-contained installers, and they are different products
with different jobs:

| | **Site agent** | **Workstation client** |
|---|---|---|
| Built from | Agents page, per agent | Machines page, per **client** |
| Runs on | one server per site | every PC |
| Does | polls printers over SNMP | provisions print queues |
| Service | `PrinterNannyAgent` | `PrinterNannyWorkstation` |
| Installs to | `…\Printer Nanny\Agent\` | `…\Printer Nanny\Workstation\` |
| Credential | one agent's key / claim code | that client's **enrollment key** |

They carry **different `UpgradeCode`s on purpose**. Windows Installer treats a
shared UpgradeCode as the same product, so if they matched, installing one would
silently uninstall the other — and a box legitimately runs both (an MSP's own
server polls printers *and* prints from them). Confirming that they coexist is a
smoke step below, because nothing in CI can prove it.

The agent sections come first; **the workstation client starts at
[Workstation client MSI](#workstation-client-msi)**.

---

## Site agent MSI

The central server can build a self-contained **Windows `.msi`** for any enrolled
agent (`central/msi_builder.py`, surfaced as a **Download Windows MSI** button on
the Agents page right after you enroll or rotate a key). The MSI bundles
everything the target needs, so a Windows Server box requires **no Python, no
winget, and only outbound HTTPS to central**:

- the official **Python embeddable runtime** (standalone `python.exe`),
- the **agent package + dependencies** (`printer_nanny_agent`, `httpx`,
  `pysnmp`, …) installed into the runtime's `Lib\site-packages`,
- **NSSM** (the service wrapper, mirrored through central),
- a generated **`config.toml`** with this agent's enrollment baked in
  (`central_url` / `agent_id` / `api_key`).

It installs to `C:\Program Files\Printer Nanny\Agent\` and registers the
`PrinterNannyAgent` Windows service. **Targets Server 2016, 2019, 2022, and
2025** (x64).

---

## How the service is wired (no install-time custom action)

The whole install is **declarative** — there is no embedded PowerShell or custom
action that runs during install, which is what keeps it portable across Server
2016→2025 and friendly to GPO/SCCM deployment:

| MSI table | What it does |
|-----------|--------------|
| `File` / `Component` / `Directory` | lay down `python\`, `nssm.exe`, `config.toml` under the install dir |
| `ServiceInstall` | registers service `PrinterNannyAgent`, binary = `nssm.exe`, **Arguments = `PrinterNannyAgent`**, auto-start, LocalSystem |
| `ServiceControl` | start on install (`Wait=no`), stop + delete on uninstall |
| `Registry` | NSSM's run parameters under `HKLM\SYSTEM\CurrentControlSet\Services\PrinterNannyAgent\Parameters` |

NSSM, when the SCM launches it, reads **its own service name from `argv[1]`**
(that's why `nssm install <name>` writes `ImagePath = "...\nssm.exe" <name>`), then
loads that service's run parameters from the registry:

Values are written as REG_SZ — `[INSTALLDIR]` is expanded by Windows Installer
at write time, so the stored paths are already absolute (no `%VAR%` to expand,
and wixl emits REG_SZ regardless of the declared type):

| Registry value | Value |
|----------------|-------|
| `Application` | `[INSTALLDIR]python\python.exe` |
| `AppParameters` | `-m printer_nanny_agent --config "[INSTALLDIR]config.toml" run` |
| `AppDirectory` | `[INSTALLDIR]` |
| `AppStdout` / `AppStderr` | `[INSTALLDIR]agent.log` (rotated at 5 MB) |
| `AppExit\Default` | `Restart` |

So the running service is effectively
`python.exe -m printer_nanny_agent --config config.toml run`, supervised and
auto-restarted by NSSM.

---

## Air-gapped / no python.org access

The build fetches the Python embeddable from python.org and NSSM from central's
mirror once, then caches both under `PN_CACHE_DIR`. For sites that can't reach
python.org, set **Settings → Agents → "Windows MSI: Python embeddable URL"** to:

- an **internal mirror** URL, or
- a **`file://`** path / plain filesystem path to a pre-downloaded
  `python-X.Y.Z-embed-amd64.zip`, or
- just drop that zip into `PN_CACHE_DIR` under its canonical name.

---

## Validate a built MSI without Windows (CI / dev box)

`msitools` (installed in the central image) inspects the artifact structure.
`central.msi_builder.validate_msi()` wraps these and the test suite asserts on
them (`tests/test_msi_builder.py::test_build_msi_end_to_end`, skipped when
msitools is absent):

```bash
msiinfo tables   printer-nanny-agent-<id>.msi      # File, Component, ServiceInstall, ServiceControl, Registry present
msiinfo export   printer-nanny-agent-<id>.msi ServiceInstall   # Name=PrinterNannyAgent, Arguments=PrinterNannyAgent
msiinfo export   printer-nanny-agent-<id>.msi Registry         # Application -> python.exe, AppParameters -> printer_nanny_agent
msiextract       printer-nanny-agent-<id>.msi                  # config.toml carries this agent's central_url/agent_id/api_key
```

This proves the **structure**. It does **not** prove the service actually starts
— that needs a real Windows host (below).

---

## Manual smoke on Windows Server (the part CI can't do)

Run on a clean Server 2016 / 2019 / 2022 / 2025 VM. Steps 4–5 are the ones the
in-container tests cannot cover.

1. **Install** (elevated):
   ```powershell
   msiexec /i printer-nanny-agent-<id>.msi /l*v install.log
   # silent: msiexec /i printer-nanny-agent-<id>.msi /qn /l*v install.log
   ```
2. **Files** landed: `C:\Program Files\Printer Nanny\Agent\` contains
   `python\python.exe`, `nssm.exe`, and `config.toml` (open it — your
   `central_url` / `agent_id` / `api_key` should be present).
3. **Service registered**:
   ```powershell
   Get-Service PrinterNannyAgent          # Status should be Running
   sc.exe qc PrinterNannyAgent            # BINARY_PATH_NAME ends: \nssm.exe PrinterNannyAgent
   ```
4. **Service runs the agent** (KEY CHECK — exercises the NSSM↔registry contract):
   ```powershell
   Get-Content 'C:\Program Files\Printer Nanny\Agent\agent.log' -Tail 30
   ```
   Expect agent startup lines and successful heartbeats. The agent should appear
   **online** with a fresh version on the central Agents page within ~1 minute.
   - If the service flaps: confirm `HKLM\SYSTEM\CurrentControlSet\Services\PrinterNannyAgent\Parameters\Application`
     points at the bundled `python.exe` and `AppParameters` is
     `-m printer_nanny_agent --config "...config.toml" run`.
5. **Connectivity selftest** (optional, direct):
   ```powershell
   & 'C:\Program Files\Printer Nanny\Agent\python\python.exe' -m printer_nanny_agent `
       --config 'C:\Program Files\Printer Nanny\Agent\config.toml' selftest
   ```
6. **Upgrade in place**: build a newer MSI (bump the program version) and
   `msiexec /i` it — the shared `UpgradeCode` should replace the old install
   without a second entry in Programs & Features.
7. **Uninstall**:
   ```powershell
   msiexec /x printer-nanny-agent-<id>.msi /qn
   Get-Service PrinterNannyAgent   # should error: service not found
   ```
   Install dir + service removed.

### Compatibility notes
- **x64 only** (Server 2016+ is x64). The embeddable runtime is `amd64`.
- The bundled Python embeddable needs the **Universal C Runtime**, present on
  Server 2016+ by default (and via Windows Update). No VC++ redist needed for
  the embeddable build.
- The `api_key` lives in `config.toml` under `C:\Program Files\...`, readable
  only by Administrators/SYSTEM by default ACLs. (The `.ps1` installer
  additionally tightens the ACL; the MSI relies on the Program Files default —
  acceptable on a server, hardening via a future custom action is possible.)

---

# Workstation client MSI

Built from **Machines → Windows installer**, one per **client** (not per machine).
`central/msi_builder.py::build_workstation_msi` shares the agent's runtime cache —
same embeddable Python, same wheel, same NSSM — and differs by a `ProductProfile`
and the config baked in. It installs to
`C:\Program Files\Printer Nanny\Workstation\` and registers the
`PrinterNannyWorkstation` service, running as **LocalSystem**.

What it does on each poll: ask central what this machine and its signed-in user
should have, then converge the spooler onto that answer.

## Each build mints its own enrollment key

Enrollment keys are SHA-256 at rest, so an existing one **cannot be read back**
to bake in — pressing that button mints a fresh key and bakes *that*.

Two consequences worth knowing before you hand an installer to anyone:

- **Every build is separately revocable.** An MSI that leaks — a file share, a
  ticket attachment, a laptop that left with someone — is revoked on the
  Machines page without disturbing any other installer or any machine already
  enrolled. Revocation stops *new* enrollments only; enrolled machines
  authenticate with their own per-machine credentials and keep working.
- **The audit row records the key id, never the key** (`workstation.msi_build`,
  `detail=ok enroll_key=<id> …`). That id is how you tie an installer in the
  wild back to the row you revoke.

Unlike the agent's claim code, this key is **multi-use** — that is the point,
since one MSI installs on hundreds of PCs. What bounds it is what it can *do*:
it can only enroll a machine. It cannot read printers, people, or other
machines, and `client_id` is fixed at mint time so a holder cannot pick a
tenant.

## Where the key lives, and why that matters more here than on a server

`workstation.toml` beside the runtime, **not** on the service command line:

| Registry value | Value |
|----------------|-------|
| `Application` | `[INSTALLDIR]python\python.exe` |
| `AppParameters` | `-m printer_nanny_agent.workstation_cli --config "[INSTALLDIR]workstation.toml"` |
| `AppDirectory` | `[INSTALLDIR]` |
| `AppStdout` / `AppStderr` | `[INSTALLDIR]workstation.log` (rotated at 5 MB) |
| `AppExit\Default` | `Restart` |

A service's command line is readable by **any logged-in user** (Task Manager's
*Command line* column, `Get-CimInstance Win32_Process`, `wmic process`), so a key
passed as an argument would be published to everyone who sits at the machine.
Only the file's *path* appears there.

> **This is a workstation, so the agent doc's ACL reasoning does not carry over.**
> The site agent's note says the Program Files default ACL is "acceptable on a
> server" — true, because ordinary users do not log into one. Ordinary users log
> into *these*, and the Program Files default grants **Users: Read**. Treat the
> enrollment key as readable by anyone who sits at the PC. That is survivable
> because the key is enroll-only, per-installer and revocable in one click — but
> if your threat model does not accept it, tighten the ACL on
> `workstation.toml` by GPO after deployment and verify the service still starts.

## Manual smoke on Windows (the part CI cannot do)

Run on a real client OS you actually deploy to. **Everything above
`PowerShellRunner` is tested with a fake**, so a green CI run proves nothing
here — this section is the only evidence that any of it works.

> **2026-07-30 — done once, and it found two defects.** Windows 11 26200
> (ARM64, so the `python-3.12.10-embed-amd64` runtime ran under x64 emulation),
> MSI installed with `msiexec /qn`, service enrolled as LocalSystem, provisioning
> against a real Brother MFC-L8900CDW.
>
> 1. **The assigned default printer had never once applied.** Fixed — see
>    `_stop_windows_managing_default` and PR #94.
> 2. **A queue that passed every check could not print.** This one is not fixed,
>    because it is a design question rather than a bug. Read the next section
>    before trusting any green result here.

---

## The installed credential's ACL

`workstation.toml` (and the agent's `config.toml`) carry a live credential, which
is why they are files rather than command-line arguments — *a service's command
line is readable by any logged-in user*. That only helps if the file is not
equally readable, and originally it was: the MSI set no ACL, the file inherited
Program Files' default, and on a real install `icacls` reported

    BUILTIN\Users:(I)(RX)
    APPLICATION PACKAGE AUTHORITY\ALL APPLICATION PACKAGES:(I)(RX)

on a file containing that client's enrollment key — on every PC in the fleet.

It is now locked at build time. `wixl` cannot express a file ACL (it rejects
WiX's `<Permission>` element), so `msibuild -i` imports a `LockPermissions` table
into the finished package and Windows Installer applies it. Confirm after any
install:

    icacls "C:\Program Files\Printer Nanny\Workstation\workstation.toml"

Expected — and nothing else:

    NT AUTHORITY\SYSTEM:(OI)(CI)(F)
    BUILTIN\Administrators:(OI)(CI)(F)

`LockPermissions` **replaces** the ACL rather than adding to it, so the absence
of `Users` is the fix working, and `AreAccessRulesProtected` should be `True`.
SYSTEM is on the list because the service runs as LocalSystem and must read its
own config — check the service actually reaches `Running`, not just that the ACL
looks tight.

> **A trap when re-testing this.** Do not delete `C:\Program Files\Printer
> Nanny` by hand between installs. The product stays registered, the next `/i`
> is treated as a *reconfigure*, every component reports `Installed: Local /
> Action: Null`, and msiexec exits **0** having laid down nothing. Uninstall by
> ProductCode (`msiexec /x {…} /qn`) instead — and note two builds of the same
> version have different ProductCodes but the same UpgradeCode, so a leftover
> registration from an earlier build silently swallows the next install.

---

## The check can pass on a queue that cannot print

`scripts/windows_provision_check.py` passed against a real printer — port not
derived, monitor not Standard TCP/IP, inbox IPP class driver, second pass a
no-op — and **the queue silently discarded every job**.

What the spooler actually recorded, once
`Microsoft-Windows-PrintService/Operational` was enabled (it is **off by
default**, which is why this is invisible):

    id=800  Spooling job 3.
    id=801  Printing job 3.
    id=842  ... print processor MS_XPS_PROC ... driver Microsoft IPP Class Driver
            ... Win32 error code returned by the print processor: 0x80004005.
    (no id=307 "Document printed")

Meanwhile `PrinterStatus=Normal`, `DetectedErrorState=0`, `WorkOffline=False`,
zero jobs pending. Nothing surfaced to the user, the queue, or central.

The device was never in doubt: raw bytes written to TCP/9100 on the same address
from the same VM printed immediately.

**Why the check missed it.** Two separate reasons, both now worth knowing:

- **It asserted against a label, not the transport.** `Get-PrinterPort` returns
  `Description` *and* `PortMonitor`, and on a `-IppURL` queue they disagree:
  `Description = "IPP Port"`, `PortMonitor = "WSD Port Monitor"`. Everything read
  the description, so the Standard-TCP/IP disproof was matching a string that
  says what we hoped. Fixed: `ws.port_transport()` prefers the monitor, and the
  check now prints description, monitor and host address separately.
- **The port has no address.** Its registry entry is
  `Printer UUID = e3248000-…`, `Install Protocol = 1`, and `PrinterHostAddress`
  is **empty**. `Add-Printer -IppURL` interrogates the device when the queue is
  built, then stores a device *identity* rather than an address. That is real and
  worth knowing — but see the next section: it is **not** why this queue failed.

### The routed-hop theory was tested, and it is wrong

The first reading of this defect blamed the address-less port: a UUID must be
re-resolved by discovery, WS-Discovery is link-local, therefore an `-IppURL`
queue cannot survive a routed hop. That was a plausible story and it is **false**.
It was tested with `ippeveprinter` (ships with macOS, CUPS 2.3.4) as a
conforming IPP Everywhere target, driving a real Windows 11 client:

| device | position | result |
|---|---|---|
| ippeveprinter | **link-local** (same /24, `Get-NetNeighbor` = Reachable) | **printed**, `id=307`, 46,525 bytes arrived |
| ippeveprinter — *same instance* | **routed hop** (via NAT, no neighbor entry) | **printed**, `id=307`, 46,526 bytes arrived |
| Brother MFC-L8900CDW | routed hop, no NAT (client bridged onto the LAN) | **`0x80004005`, no `id=307`** |
| Brother MFC-L8900CDW | routed hop, raw TCP/9100 from the same client | **printed** |

Only the network position changed between rows 1 and 2, and it made no
difference. **`-IppURL` prints across a routed hop.**

A second theory — that the Brother fails because it does not advertise
`application/pdf` (Windows sends PDF to the emulator) — was tested the same way
and is also false: with PDF removed from the emulator's
`document-format-supported`, Windows negotiated down to `image/pwg-raster` on its
own and printed (19,726 bytes of `.pwg` received). Format negotiation is fine.

**What is actually left.** The failure is specific to this real device. The queue
is created, converges, reports healthy, and the job dies in the print processor —
while the same Brother prints happily from CUPS over the same IPP endpoint, and
from the same Windows client over raw 9100. The one structural difference still
standing is what the device advertises about itself:

    Brother    ipp-features-supported = airprint-1.6, wfds-print-1.0
    emulator   ipp-features-supported = ipp-everywhere

**The actionable consequence.** The probe marks this printer `driverless` on the
strength of "IPP 2.0 with image/pwg-raster". That is demonstrably **not
sufficient** to predict that Windows' inbox IPP class driver can print to it. If
tier 1 is to be trusted, either the probe needs a stronger criterion (an
`ipp-everywhere` feature check is the obvious candidate, though unverified) or
the client must stop treating a successful `Add-Printer` as evidence.

**The only sufficient check is a printed page.** Every proxy short of paper —
`Get-Printer`, the port name, the monitor, the driver, convergence, an empty
queue, `DetectedErrorState` — has now returned a clean answer for a queue that
could not print.

### Enable the print log before you believe anything

    $ch = 'Microsoft-Windows-PrintService/Operational'
    $c = New-Object System.Diagnostics.Eventing.Reader.EventLogConfiguration $ch
    $c.IsEnabled = $true; $c.SaveChanges()

Then look for **`id=307`**. Its absence is the failure; `id=842` with a non-zero
`Win32 error code` names it.

1. **Install** (elevated):
   ```powershell
   msiexec /i printer-nanny-workstation-<client>.msi /l*v ws-install.log
   # silent: msiexec /i printer-nanny-workstation-<client>.msi /qn /l*v ws-install.log
   ```
2. **Files** landed in `C:\Program Files\Printer Nanny\Workstation\`:
   `python\python.exe`, `nssm.exe`, `workstation.toml` (open it — `server` and
   `enroll_key` present).
3. **Service registered and running**:
   ```powershell
   Get-Service PrinterNannyWorkstation
   sc.exe qc PrinterNannyWorkstation      # BINARY_PATH_NAME ends: \nssm.exe PrinterNannyWorkstation
   ```
4. **It enrolled** (KEY CHECK — the NSSM↔registry contract plus the API):
   ```powershell
   Get-Content 'C:\Program Files\Printer Nanny\Workstation\workstation.log' -Tail 40
   Get-Content "$env:PROGRAMDATA\PrinterNanny\machine.json"   # machine_uid + machine_id + api_key
   ```
   The machine should appear on **Machines** within a poll, and the enrollment
   key's *Last used* should update. Central's audit log gets `machine.enroll`.
5. **The key is not on the command line** (asserted in CI against the WXS, but
   verify the real service):
   ```powershell
   (Get-CimInstance Win32_Process -Filter "Name='python.exe'").CommandLine
   ```
   Must show `--config "...workstation.toml"` and **no `pnw_…` value**.
6. **Assign a printer** to the machine (or to the signed-in person) on the
   Machines / People page, wait a poll, then:
   ```powershell
   Get-Printer | Where-Object Name -like 'PN *'
   ```
7. **Prove the queue is real, not merely present** — this is the check that
   caught tier 1 being broken for its entire existence:
   ```powershell
   python scripts\windows_provision_check.py --ip <printer-ip>
   ```
   `Get-Printer` returning the queue proves *nothing*. What matters:
   - the **port name is one Windows chose** (a `WSD-<guid>`-style name), **not**
     one we derived;
   - the **monitor is not `Standard TCP/IP Port`** — that monitor speaks only
     RAW/LPR on 9100 and cannot carry IPP, so landing there means the queue
     cannot print;
   - the driver is the **inbox IPP class driver**.

   Then actually print a test page. A queue that converges and cannot print is
   the exact failure this whole path exists to prevent.
8. **Convergence is idempotent** — restart the service (or wait two polls) and
   re-run step 6. The queue must be **unchanged**, not recreated. A fresh port
   per run is how workstations end up with forty dead ports.
9. **Both products coexist** (only if you deploy both to one box): install the
   site agent MSI too, then confirm **both** services exist and **both** entries
   appear in Programs & Features. If installing the second removed the first,
   the `UpgradeCode`s have collided — that is a release-blocking regression.
10. **Uninstall**:
    ```powershell
    msiexec /x printer-nanny-workstation-<client>.msi /qn
    Get-Service PrinterNannyWorkstation    # should error: service not found
    ```
    Expected, and **not** a bug:
    - **Provisioned `PN ` queues remain.** Uninstalling a management tool should
      not rip printers out from under whoever is using them. Remove them by hand
      if you want them gone.
    - **`%PROGRAMDATA%\PrinterNanny\machine.json` remains**, so reinstalling
      re-adopts the same machine identity and its assignments rather than
      creating a second row for one PC.

      Deleting it (or re-imaging, which wipes it) no longer costs the PC its
      printers: it enrolls with a fresh GUID and central adopts the previous
      record **by computer name**, provided that record has not checked in for
      `workstation.adopt_stale_after_min` (default 60) and exactly one record in
      that client carries the name. Worth testing deliberately — delete
      `machine.json`, wait out the staleness window, restart the service, and
      confirm the machine count does **not** grow and the queues come back. The
      audit row is `machine.adopt`. Turn it off with **Settings → Agents →
      Workstations → "Re-imaged PCs keep their printers"** if you would rather a
      re-image always produce a new machine.

## Behaviour to confirm is *reported*, not silently assumed

Both of these do real work now, and both can fail in ways that look like
success from central. The smoke test is that the client tells you which happened
rather than assuming the good case:

- **The user's default printer IS set**, by impersonating the console session.
  Verify it *as the signed-in user, not elevated* — the whole point is that it is
  per-user state:

  ```powershell
  Get-CimInstance Win32_Printer -Filter "Default=TRUE" | Select-Object Name
  Get-ItemProperty 'HKCU:\Software\Microsoft\Windows NT\CurrentVersion\Windows' `
      -Name LegacyDefaultPrinterMode      # 1 = Windows no longer manages it
  ```

  The client turns **"Let Windows manage my default printer"** off first, because
  it ships ON and re-points the default at whatever was printed to last — without
  that, an assigned default appears to apply and then quietly does not. It
  overrides a user preference, so it is controlled by **Settings → Agents →
  Workstations → "Set the user's default printer"**.

  Three checks worth doing deliberately, none of which CI can perform:
  1. With the setting ON, print to a *different* queue, then confirm the assigned
     default **stays put**. That is the behaviour the whole feature exists for.
  2. Turn the setting OFF, re-run, and confirm `workstation.log` reports the
     desired default as **NOT applied with a reason** rather than claiming it.
  3. Break a queue (unplug the printer, or assign a `driver_required` one with no
     package) and confirm it **never becomes the default** — central showing a
     default the user does not have is the failure this path is built to avoid.
- **Vendor drivers are staged only when a package is uploaded.** With no
  matching package a `driver_required` printer is skipped with a stated reason,
  never bound to a wrong driver — check `workstation.log` and confirm the printer
  is visibly absent *and* explained. With a package (Machines → Vendor driver
  packages), verify the whole path on a real machine, because none of it has run
  against a real driver store:

  ```powershell
  Get-Content 'C:\Program Files\Printer Nanny\Workstation\workstation.log' -Tail 40
  pnputil /enum-drivers | Select-String -Context 0,4 'BRPRF'   # your INF name
  Get-PrinterDriver -Name '<the driver name you typed on upload>'
  ```

  The **driver name must match the INF exactly**: staging succeeds and the bind
  then fails if it does not, which shows up as `Add-PrinterDriver` erroring in
  the log while `pnputil` reported success. Then print a test page — a bound
  queue is not a working one.

  Worth testing deliberately, since it is the failure an operator will hit:
  corrupt the package on the server volume, delete
  `%PROGRAMDATA%\PrinterNanny\drivers`, and confirm the next poll reports
  `driver package unusable: ... checksum mismatch` and provisions **nothing**
  rather than unpacking it.

Also verify an `ipp_disabled` printer is described as **IPP disabled on the
device (port 631 refused)** and never as a driver problem — that distinction
sends a technician to the printer's web UI instead of the driver store.

## Compatibility

- **x64 only**; the embeddable runtime is `amd64`.
- **`Add-Printer -IppURL` is required and is probed at runtime, not inferred
  from an OS version.** It is present in the Server 2022/2025 cmdlet sets and
  **absent on Server 2019**, whose second parameter set is WSD-only. Where it is
  missing the client **refuses outright rather than falling back** — falling back
  is what produced the silent breakage. Part of this smoke is establishing which
  of *your* target OSes actually have it; do not assume from the version number.
- The Server 2016→2025 matrix in the agent sections above is about the **site
  agent**, which is an SNMP poller and never touches this path. Do not read it
  as a support statement for the workstation client.
- **CI cannot stand in for the LocalSystem context.** The Windows runner is an
  elevated *user*, not SYSTEM, and console-user detection (`WTSQuerySessionInformation`
  + `TranslateName`) behaves differently there. Domain-joined behaviour, GPO
  interaction with `RestrictDriverInstallationToAdministrators`, and vendor
  driver packages are all manual too.
