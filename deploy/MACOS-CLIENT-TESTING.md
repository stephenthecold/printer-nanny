# macOS workstation client — verification procedure

The macOS sibling of `WINDOWS-MSI-TESTING.md`. Same structure, same honesty: what
is proven, by what, and what is still outstanding.

**Status.** The queue, vendor-driver and default-printer logic is verified against
a **real CUPS scheduler** (see below — that verification, plus the required
end-to-end smoke, found **seven** defects).

**It has now also run on a real Mac** — macOS 26.5.2, Apple Silicon, system
Python 3.9.6, no Homebrew — including an actual `installer -pkg` of a `.pkg`
built by `pkgbuild` on that machine, the LaunchDaemon loaded by launchd and
running as root, and a queue provisioned and a console user's default set by that
daemon — **and a page has come out of a real printer.** That run found an
**eighth** defect, which is described below and which nothing short of an install
could have found: enrollment sat outside the poll loop's retry handler, so a
central it could not resolve killed the process, launchd respawned it every 60s,
and the daemon log grew ~9.8 MB/day of tracebacks. What is *still* unverified is
listed at the end: MDM, notarization, a non-English Mac, fast user switching, the
login-window case, a directory-bound Mac and Intel hardware remain untouched.

| | **Site agent** | **Workstation client** |
|---|---|---|
| Runs as | LaunchAgent (per user) | **LaunchDaemon** (root) |
| Label | `com.printernanny.agent` | `com.printernanny.workstation` |
| Does | polls printers over SNMP | provisions CUPS queues |
| Installed by | `scripts/install-local-agent-macos.sh` | `deploy/install-workstation-macos.sh` |
| Credential | agent key / claim code | that client's **enrollment key** |
| Needs root | no | **yes** (`lpadmin`, state dir, `sudo -u`) |

---

## Why a LaunchDaemon, not a LaunchAgent

Three reasons, each of which rules the Agent out on its own:

- **`lpadmin` needs root** (or the `lpadmin` group), and queues are machine-wide.
  A per-user Agent would provision a queue only while that user is logged in.
- **The machine's credential** lives in
  `/Library/Application Support/PrinterNanny` at mode 0700, and must not be
  readable by the user whose printers it manages.
- **The default printer is per-user**, and the client sets it for whoever is at
  the console *now*. An Agent already running as one person could not switch
  after a fast user switch.

The site agent's plist next door *is* a LaunchAgent, correctly: it only reads
SNMP and needs no privilege at all.

## Where the key lives, and why

`ProgramArguments` and `EnvironmentVariables` are readable by **any local user**
(`launchctl print system/com.printernanny.workstation`), and launchd requires the
plist itself to be world-readable. So the enrollment key goes in
`workstation.toml` at mode 0600 under the state directory, and only its *path*
appears in the plist. This is the same rule as the Windows service, whose command
line is likewise readable by any logged-in user — reached independently, for the
same reason.

`tests/test_macos_deployment.py` asserts this against both plists: the reviewable
one in `deploy/` and the one the installer generates, because they are two files
and only one of them ships.

---

## What is already proven, and by what

### Unit tests — `tests/test_workstation_macos.py`

Run on any platform with a fake `_run`. Exhaustive below the seam, and **worth
nothing above it** — a fake returns what it is told. That is not a hedge; it is
the lesson this repo paid for once already, when every `tests/windows/` test
passed while tier 1 could not print.

    pytest tests/test_workstation_macos.py tests/test_macos_deployment.py

### Real-scheduler check — `scripts/macos_provision_check.py`

The macOS sibling of `windows_provision_check.py`, and the only thing that talks
to a scheduler. **On its first run it found four defects the entire unit suite had
passed over**, every one of which would have shipped:

1. **`read_default_printer` could never succeed.** It ran `lpoptions -d` with no
   destination, which is a *usage error* (exit 1, prints usage). The read-back
   always returned `None`, so `set_default_printer` *always* raised "default did
   not stick". The assigned-default feature had never once worked. The read is
   `lpstat -d`. (A *successful* `lpoptions -d NAME` prints the queue's whole
   option list, so parsing that output for a name yields `copies=1`.)
2. **Every parse was locale-dependent.** `lpstat -p` on a German Mac reads
   `Drucker X ist im Leerlauf`, so enumeration matched nothing: every poll
   re-created every queue — a live IPP query per printer per poll, exactly the
   round trip the code promises not to make — and no stale queue was ever
   removed. Fixed by forcing `LC_ALL=C` on every command (including through
   `sudo`, which scrubs the environment) and enumerating with `lpstat -v`, which
   yields name *and* URI in one call.
3. **A failed repair stranded a queue that could never recover.** `cupsd` commits
   `device-uri` and *then* runs the `-m everywhere` query, so a repair against an
   unreachable printer exited 1 having already moved the queue and generated no
   PPD. That queue existed, was listed, matched what central wanted, and
   converged as "unchanged" **forever** while being unable to print — the macOS
   spelling of the tier-1 Windows bug. A failed repair now restores the previous
   URI (a `-v`-only `lpadmin` is instant, needs no network, and leaves the PPD
   byte-identical — measured), and a failed create removes the carcass.
4. **An unreachable printer cost 30s**, and the module's single 30s timeout
   collided with `cupsd`'s own, so the failure surfaced as our useless "timed
   out" instead of `lpadmin`'s message naming the address. Live queries now have
   their own larger timeout, and a whole pass is bounded so a rack of sleeping
   printers cannot outlast the poll interval.

A **fifth** is verified there too, though it was found by reading the code:
`cups_queue_name` strips trailing underscores, so the shipped `MANAGED_PREFIX =
"PN "` sanitised to `"PN"` — which matches a user's own `PNMyPrinter` and would
have deleted it, the precise failure the prefix exists to prevent.

A **seventh** was the same failure a third time, introduced by the fix for the
third one: binding a vendor PPD replaces the queue's PPD *before* cupsd's verdict
exists, so restoring the URI on failure left the queue on its old address with the
new broken PPD — right URI, converges forever, cannot print. A PPD is now tried on
a throwaway probe queue and the real queue is only touched once it is proven. The
general rule: **if a failed operation cannot be undone, do not start it.**

A **sixth** needed the end-to-end smoke rather than this script, because it lives
*above* the `_run` seam and so no fake could show it: `skipped` and
`desired_default` carried the Windows spelling of a queue name while `outcomes`
carried the CUPS one, so `outcomes.get(desired_default)` missed every time. On a
Mac the assigned default could never be applied, and the reason reported was "its
queue was not provisioned (skipped)" *even when the queue had been created
perfectly*. Backends now expose `queue_name()` and the whole report is keyed
through it.

Also proven there: the sanitised queue name is one CUPS accepts *and* CUPS
rejects the unsanitised one (so the substitution is load-bearing); a
shell-shaped name is created literally with a canary file left untouched;
removal is scoped to the managed prefix and a lookalike like `PNMyPrinter` is
never touched; and the per-user default really is per-user — root's default is
unaffected by the console user's, which is why reading it as root would let you
convince yourself a write worked when it had not.

#### Running it without a Mac

This is how all four defects above were found. CUPS is CUPS: macOS ships the same
daemon and client tools, so command shapes, exit codes, translated prose and
`~/.cups/lpoptions` precedence all behave identically.

    # Debian/Ubuntu
    sudo apt-get install -y cups-daemon cups-client
    sudo scripts/macos_cups_testbed.sh start
    sudo PYTHONPATH=agent python3 scripts/macos_provision_check.py --as-user pntest
    sudo scripts/macos_cups_testbed.sh stop

The testbed listens on `/run/cups/cups.sock` — the *default* socket —
deliberately: `sudo` scrubs the environment, so a `CUPS_SERVER` pointing
elsewhere would not reach the per-user `lpoptions` call and the default-printer
check would silently test nothing. It also creates a second account, because one
account cannot demonstrate per-user isolation.

Add `--dead-uri` pointing at a genuinely blackholed address on a routed subnet to
exercise the timeout ordering; TEST-NET often fails fast instead of timing out.

#### Running it on a Mac, against a real printer

**This is the step that has not been done, and it is the point of the script.**

    sudo python3 scripts/macos_provision_check.py \
        --printer-uri ipp://10.0.0.5:631/ipp/print --as-user alice

`--printer-uri` is what makes `-m everywhere` do its real work: CUPS queries the
device's IPP attributes and generates the PPD from them. The check then asserts a
PPD was generated, that a second pass is `unchanged` and costs no round trip, and
that the queue removes cleanly. Every queue it creates carries `PNCHK_` and it
touches nothing else; it saves and restores the default printer of whoever
`--as-user` names — including the "they had no default" case, which needs the
file edited because CUPS has no "unset the default" command, and which the script
asserts rather than assumes.

A green run **still does not prove a page comes out.** Print one.

---

### The eighth defect — what only an actual install could find

Every check above passes on a machine that can reach central. The `.pkg` that was
installed to find this one deliberately could not: it pointed at
`https://central.invalid`, which is what a typo'd server URL, a VLAN that is not
up yet, or a first boot behind a captive portal all look like.

`run()` documents its contract — *"Transport failures are logged and retried on
the next tick rather than killing the service"* — and the poll loop honoured it.
But `ensure_enrolled` was called **above** the loop, outside that `try`. So the
rule held for every poll and failed at enrollment, which is precisely the moment
a freshly imaged machine is least likely to have a network. Observed on the Mac:

- an unhandled `httpx.ConnectError` traceback, exit 1;
- launchd's `KeepAlive{SuccessfulExit=false}` respawning on `ThrottleInterval`
  — every **60s**, five times more often than a successful poll;
- `/Library/Logs/PrinterNanny/workstation.log` growing **9.8 MB/day**
  (measured: 118.6 B/s over 75s), root-owned, with **no rotation** configured in
  the plist or `/etc/newsyslog.d`;
- and no stated reason anywhere in it — just httpx internals.

Fixed by moving `ensure_enrolled` inside the loop, which costs nothing (once
enrolled it adopts the stored credential and makes no request). A refused **key**
still re-raises, because that is terminal and must be reported once rather than
retried forever. After the fix, the same unreachable central produces one
`cycle failed, will retry in 300s: …` line per interval and no traceback —
~44 KB/day, a ~230× reduction, verified by running it.

**A related trap, not yet resolved.** `workstation_cli` returns exit **2** for a
refused key, commented "restarting on a bad key just retries forever and buries
the reason in a restart loop". On macOS the plist defeats that:
`KeepAlive{SuccessfulExit=false}` restarts on *any* non-zero exit, so exit 2
loops too — just with one line instead of a traceback. launchd cannot express
"restart unless the exit code is 2". Worth deciding deliberately rather than
leaving the comment describing something that does not happen.

Log rotation was considered and **not** shipped: `StandardOutPath` means launchd
opens the file and the process inherits the descriptor, so a `newsyslog` rename
would leave the daemon writing to the rotated inode unless it also learns to
reopen on a signal. That is a real change needing its own verification, and with
the growth rate down 230× it is no longer urgent.

---

## Install

    curl -fsSL https://CENTRAL/install-workstation-macos.sh | sudo bash -s -- \
      --server https://CENTRAL --enroll-key pnw_xxxxx

Mint the enrollment key on the **Machines** page. Re-running upgrades in place and
keeps the machine's identity, which is what stops an upgrade looking like a new
machine to central.

## The `.pkg` installer

For MDM deployment, build a `.pkg` instead. **Machines → macOS installer → Download
macOS bundle** gives you a per-client bundle; run its build script *on a Mac*:

    tar xzf printer-nanny-workstation-<client>-macos-bundle.tar.gz
    cd <extracted>
    set -a; . ./pkg.env; set +a
    ./build-macos-pkg.sh              # unsigned: MDM-installable, not double-clickable
    ./build-macos-pkg.sh --notarize   # signed + notarized + stapled

**Why central does not just hand you the `.pkg`.** `pkgbuild`, `productbuild`,
`productsign`, `notarytool` and `stapler` are macOS-only, and `notarytool` is a
closed Apple binary talking to an Apple service. Signing also needs *your* Developer
ID certificate in a keychain. So a Mac is required regardless, and central does the
part only central can — mint this client's enrollment key and assemble the payload.
Hand-assembling an unsigned `.pkg` on Linux with `xar`+`bomutils` was rejected:
neither is packaged in current Ubuntu, and the result would still need the Mac.

**Signing and notarization are separate things, and you need both** for a package a
human can double-click. Signed-but-not-notarized is still refused by Gatekeeper on
10.15+. Stapling matters too: without it a Mac that cannot reach Apple refuses a
perfectly notarized package, which describes a segmented client VLAN — i.e. exactly
where these get installed.

    PN_SIGN_IDENTITY   "Developer ID Installer: Acme Inc (AB12CD34EF)"
    PN_TEAM_ID         AB12CD34EF
    PN_ASC_KEY_ID / PN_ASC_ISSUER_ID / PN_ASC_KEY_PATH   (preferred: no 2FA prompt)
    PN_APPLE_ID / PN_APPLE_PASSWORD                      (app-specific password)

Credentials come from the environment, never from arguments — a process's command
line is readable by every user on the machine. `set -x` is never enabled in that
script for the same reason, and a test asserts it.

**The bundle contains a live credential.** `workstation.toml` holds this client's
enrollment key at mode 0600. Don't commit the bundle, don't attach it to a ticket,
and delete it once the `.pkg` is built — the `.pkg` carries the same key and is
equally sensitive. Each build mints its **own** key, so revoking one installer
leaves every other install working.

`.github/workflows/macos-pkg.yml` builds and inspects the package on a
`macos-latest` runner, and signs it when the repo has the secrets. That is the only
automated coverage `pkgbuild` has; nothing on Linux can provide any.

---

## Manual smoke on a real Mac (the part nothing above can do)

Checked boxes were done on 2026-07-30 against macOS 26.5.2 / Apple Silicon /
system Python 3.9.6 / no Homebrew, with a `.pkg` built by `pkgbuild` on that
machine and installed with `installer -pkg … -target /`. Everything still
unchecked is genuinely outstanding.

### 1. Install and enrollment

- [x] The installer completes on Apple Silicon. *(Intel still untested.)*
- [x] It completes on a Mac with **no Homebrew** — `/usr/bin/python3` is a stub
      that can trigger the Command Line Tools prompt, which is fatal in an MDM
      push. The installer prefers a real interpreter; confirm what it picked.
      *(Host had no Homebrew and no python.org build; the loop fell through all
      three preferred candidates to `/usr/bin/python3` (3.9.6) and said so.)*
- [x] `launchctl print system/com.printernanny.workstation` shows it loaded.
- [x] The machine appears on the **Machines** page within one poll.
      *(Enrolled and checked in, `platform='macos'` recorded by check-in.)*
- [x] `/Library/Application Support/PrinterNanny` is `0700 root:wheel` and
      `workstation.toml` is `0600`.
- [x] As an ordinary user, `launchctl print system/com.printernanny.workstation`
      shows **no enrollment key**, and `cat` of `workstation.toml` is denied.
      *(Both confirmed; the plist carries only the path.)*

### 2. Queues

- [x] A `driverless` printer assigned to the machine appears within one poll.
      *(Created by the root LaunchDaemon, not by a hand-run client.)*
- [x] It prints a test page. *(Nothing short of this proves the path works.)*
      **Done 2026-07-30**, and it is the only item here that could not be
      substituted for: a Brother MFC-L8900CDW at
      `ipp://BRW30C9AB0B3F71.local.:631/ipp/print`, queue provisioned by the root
      LaunchDaemon, PPD generated from the device's own attributes
      (`MFC-L8900CDW series - IPP Everywhere`), job ~39s in `now printing`, then
      completed with nothing in `error_log`. Two things made it evidence rather
      than a green log line: the device's **real supply telemetry** came back
      through the queue during the print (`marker-types=toner,toner,toner,toner`,
      `marker-levels=0,20,20,20`, `printer-state-reasons=toner-low-warning`),
      which no loopback or synthetic queue produces — and the **paper was
      confirmed by hand**.
- [x] `lpstat -v` shows the URI central sent.
- [x] The queue is **not shared** (`lpoptions -p NAME | tr ' ' '\n' | grep shared`
      → `printer-is-shared=false`). A shared queue advertises a workstation's
      printers to the whole subnet. *(Confirmed, and `printer-state-reasons=none`.)*
- [x] Unassigning it removes the queue on the next poll.
- [x] A printer the **user** added themselves survives a poll, and is not
      mentioned in the outcomes. *(All 13 pre-existing queues on the host
      survived every pass untouched.)*
- [ ] Re-addressing the printer in central repairs the queue in place — one
      queue, not a broken one beside a new one.
- [ ] Powering the printer **off** and re-addressing it leaves the existing queue
      exactly as it was (URI unchanged, still prints) and reports an error. This
      is defect 3 above; it is the one worth re-checking by hand.

### 3. The default printer

- [x] An assigned default becomes the console user's default.
      *(Set by the **root** daemon via `sudo -u`, which is the production path.)*
- [x] `lpstat -d` **as that user** agrees. *(And the write landed in the user's
      `~/.cups/lpoptions`, not root's `/etc/cups/lpoptions` — which is the check
      that proves it impersonated rather than setting root's own default.)*
- [x] Central reports it as applied — and reports a *reason* when it is not.
      *(Reported under the **CUPS** queue name, so defect 6 stays fixed.)*
- [ ] At the login window it reports "nobody is signed in" rather than an error.
- [ ] After a **fast user switch**, the next poll sets the default for the user
      now at the console, and does not touch the other's.
- [ ] On a **non-English** Mac (System Settings → Language & Region → Deutsch,
      then reboot): queues still converge and the default still applies. This is
      defect 2; the unit tests cover the parse but only a real Mac covers the
      daemon's inherited locale.

### 4. Directory-bound Macs

- [ ] On a Mac bound to AD/Entra, `dscl . -read /Users/$USER NetworkUser` returns
      a UPN and the user's **own** assignments resolve (not just the machine's).
- [x] On a **standalone** Mac with no UPN, machine-scoped printers still
      provision and the user-scoped ones are simply absent — not an error.
      *(`dscl` ran, found no UPN, logged one INFO line, and the machine's own
      printers provisioned normally.)*

### 5. Things that must be reported, not silently assumed

- [ ] A `driver_required` printer with **no macOS package** is skipped with a
      stated reason, and no queue is created for it.
- [ ] A `driver_required` printer does **not** cause a *Windows* driver package to
      be downloaded onto the Mac — check the Machines page shows the Mac as
      `macos`, then confirm nothing appears under
      `/Library/Application Support/PrinterNanny/drivers`.
- [x] An unreachable printer produces an error naming the address, and the poll
      still checks in. *(`lpadmin: Unable to connect to "192.0.2.77:631"` —
      lpadmin's own words, not our "timed out"; no carcass; the reachable queue
      alongside it was created in the same pass.)*
- [ ] Ten unreachable printers do not stop the poll completing; the ones not
      reached say so.

### 6. Vendor drivers

Three shapes, and they need testing separately because they differ in privilege
rather than in outcome.

**MDM-installed PPD (`system`) — the recommended path.**

- [ ] Push a vendor driver `.pkg` with your MDM (Jamf/Mosyle/Kandji).
- [ ] Find the PPD it installed, usually under
      `/Library/Printers/PPDs/Contents/Resources/`.
- [ ] Record it on the Machines page: platform macOS, kind "PPD my MDM already
      installed", and that absolute path. **No file upload.**
- [ ] The queue appears within one poll, and **prints a test page**.
- [ ] `lpoptions -p <queue> | tr ' ' '\n' | grep state-reasons` does **not**
      contain `cups-missing-filter-warning`.

**A `.ppd` in an uploaded package.**

- [ ] Upload a zip containing a `.ppd`, with its path inside the archive.
- [ ] For a **PostScript** printer this should produce a working queue with no
      vendor package installed at all. Print a page.
- [ ] For a printer needing vendor filters, the queue is **refused** with an error
      naming the missing filter — *not* created. This is the check that matters:
      confirm no queue is left behind, and that the next poll retries rather than
      reporting it converged.

**A vendor `.pkg` (off by default).**

- [ ] With `workstation.allow_macos_pkg_install` **off**, a `pkg` package is a
      stated error mentioning the setting, and `installer` never runs.
- [ ] Turn it on. The client installs the package as root and binds the PPD path
      you recorded.
- [ ] A **second** poll does not reinstall it — check the install log
      (`/var/log/install.log`) has one entry, not one per poll.
- [ ] An archive containing two `.pkg` files is **refused** rather than guessed.

**Guards worth confirming by hand**, since they protect a root command:

- [ ] A system PPD path outside `/Library/Printers/` etc. is refused.
- [ ] A symlink inside an allowed directory pointing elsewhere is refused
      (`realpath` runs before the root check).
- [ ] A `.ppd` path that escapes the archive is refused.

### 7. The `.pkg` installer

Everything here is outstanding, and the `.pkg` path has **no** local automated
coverage — `installer`, `pkgbuild` and `notarytool` do not exist off macOS.

- [x] `./build-macos-pkg.sh` produces a `.pkg` on a Mac with the Xcode command line
      tools, and `pkgutil --expand` shows `com.printernanny.workstation` and the
      agent wheel in the payload. *(Also: exactly one `.pkg` in `out/`, min OS
      10.12, `preinstall`/`postinstall` present and executable and parsing under
      real macOS `bash` 3.2.57, plist passing `plutil -lint` with no credential
      in it.)*
- [x] `sudo installer -pkg <pkg> -target /` installs, and
      `launchctl print system/com.printernanny.workstation` shows it loaded.
- [x] `/Library/Application Support/PrinterNanny/workstation.toml` is **0600
      root:wheel** after install. As an ordinary user, `cat` of it is denied.
- [x] The install worked **offline** — every wheel is in the payload and
      `postinstall` runs pip with `--no-index`. *(Verified as a distinct step:
      a venv from `/usr/bin/python3` + the exact `--no-index --find-links`
      invocation installed `printer-nanny-agent 0.16.0` and a working entry
      point. All 12 wheels are `py3-none-any`.)*
- [x] On a Mac with **no Homebrew and no python.org build**, the postinstall either
      picks `/usr/bin/python3` successfully or fails with the stated reason. It must
      not hang on a Command Line Tools prompt.
- [x] Reinstalling over an existing install works: `preinstall` stops the daemon,
      the venv is rebuilt, and the machine keeps its identity (same row on the
      Machines page, not a new one). *(The machine UID minted before the reinstall
      was the one used after it — `machine.json` is not in the payload, so it
      survives, which is the mechanism.)*
- [ ] `--notarize` produces a package where `spctl --assess --type install -vv`
      accepts it, and `xcrun stapler validate` passes.
- [ ] A **double-click** on the notarized package opens Installer without a
      Gatekeeper warning. On the *unsigned* one, confirm Gatekeeper refuses it — if
      it doesn't, the signing story is being tested wrong.
      *(Second half **done**: `spctl --assess --type install -vv` on the unsigned
      build → `rejected`, `source=no usable signature`, exit 3; `pkgutil
      --check-signature` → `Status: no signature`. The notarized half still needs
      a Developer ID certificate.)*
- [ ] An MDM `InstallEnterpriseApplication` push of the **unsigned** package
      installs successfully, since that is the path that does not need signing.

### 8. Uninstall

    sudo bash deploy/install-workstation-macos.sh --uninstall

- [ ] The daemon stops and does not come back after a reboot.
- [ ] The **state directory survives** — it holds the machine's GUID, and
      removing it turns a reinstall into a brand-new machine, stranding this Mac's
      assignments on a record nobody will look at again.
- [ ] Reinstalling keeps the same machine record on the Machines page.
- [ ] Queues are **not** removed, and the uninstaller says so with the command to
      remove them.

---

## Compatibility

- **macOS 10.12+** for `lpadmin -m everywhere` (no `everywhere` model before it;
  refused rather than fudged).
- **Python 3.9+**; the client is 3.9-compatible like the rest of the agent.
- `sudo`, `stat`, `dscl`, `lpadmin`, `lpstat`, `lpoptions` are all in the base OS.
  Nothing is installed beyond the venv.
- No IPv6 requirement, no Bonjour requirement, no Homebrew requirement.
