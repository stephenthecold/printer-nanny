# macOS workstation client — verification procedure

The macOS sibling of `WINDOWS-MSI-TESTING.md`. Same structure, same honesty: what
is proven, by what, and what is still outstanding.

**Status.** The queue, vendor-driver and default-printer logic is verified against
a **real CUPS scheduler** (see below — that verification, plus the required
end-to-end smoke, has now found **seven** defects). What is *not*
verified is a **real Mac**: launchd, `/dev/console`, `dscl`, a real printer, and
MDM interaction have never been exercised. Treat the manual smoke section as
outstanding work, not as a record of work already done.

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

## Install

    curl -fsSL https://CENTRAL/install-workstation-macos.sh | sudo bash -s -- \
      --server https://CENTRAL --enroll-key pnw_xxxxx

Mint the enrollment key on the **Machines** page. Re-running upgrades in place and
keeps the machine's identity, which is what stops an upgrade looking like a new
machine to central.

There is deliberately **no signed, notarized `.pkg`** yet. An MSP pushing this
through MDM wants one; building it needs an Apple Developer ID and a notarization
round trip, so it is out of scope rather than half-built. A `.pkg` postinstall
would end up running this script.

---

## Manual smoke on a real Mac (the part nothing above can do)

Everything in this section is **outstanding**.

### 1. Install and enrollment

- [ ] The installer completes on Apple Silicon **and** Intel.
- [ ] It completes on a Mac with **no Homebrew** — `/usr/bin/python3` is a stub
      that can trigger the Command Line Tools prompt, which is fatal in an MDM
      push. The installer prefers a real interpreter; confirm what it picked.
- [ ] `launchctl print system/com.printernanny.workstation` shows it loaded.
- [ ] The machine appears on the **Machines** page within one poll.
- [ ] `/Library/Application Support/PrinterNanny` is `0700 root:wheel` and
      `workstation.toml` is `0600`.
- [ ] As an ordinary user, `launchctl print system/com.printernanny.workstation`
      shows **no enrollment key**, and `cat` of `workstation.toml` is denied.

### 2. Queues

- [ ] A `driverless` printer assigned to the machine appears in
      **System Settings → Printers & Scanners** within one poll.
- [ ] It prints a test page. *(Nothing short of this proves the path works.)*
- [ ] `lpstat -v` shows the URI central sent.
- [ ] The queue is **not shared** (`lpoptions -p NAME | tr ' ' '\n' | grep shared`
      → `printer-is-shared=false`). A shared queue advertises a workstation's
      printers to the whole subnet.
- [ ] Unassigning it removes the queue on the next poll.
- [ ] A printer the **user** added themselves survives a poll, and is not
      mentioned in the outcomes.
- [ ] Re-addressing the printer in central repairs the queue in place — one
      queue, not a broken one beside a new one.
- [ ] Powering the printer **off** and re-addressing it leaves the existing queue
      exactly as it was (URI unchanged, still prints) and reports an error. This
      is defect 3 above; it is the one worth re-checking by hand.

### 3. The default printer

- [ ] An assigned default becomes the console user's default in System Settings.
- [ ] `lpstat -d` **as that user** agrees.
- [ ] Central reports it as applied — and reports a *reason* when it is not.
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
- [ ] On a **standalone** Mac with no UPN, machine-scoped printers still
      provision and the user-scoped ones are simply absent — not an error.

### 5. Things that must be reported, not silently assumed

- [ ] A `driver_required` printer with **no macOS package** is skipped with a
      stated reason, and no queue is created for it.
- [ ] A `driver_required` printer does **not** cause a *Windows* driver package to
      be downloaded onto the Mac — check the Machines page shows the Mac as
      `macos`, then confirm nothing appears under
      `/Library/Application Support/PrinterNanny/drivers`.
- [ ] An unreachable printer produces an error naming the address, and the poll
      still checks in.
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

### 7. Uninstall

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
