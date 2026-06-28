# Windows MSI — build & test procedure

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
