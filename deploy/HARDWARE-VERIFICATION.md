# Hardware verification brief — the work that cannot be done from a container

**Nothing in this document has been executed.** Not one step. That is the whole
reason it exists as a brief rather than a record, and it is stated first so that
nobody reads a procedure and infers a result from it.

The reason is not scheduling. Every task below requires a physical object that a
build container does not have:

| Task | What it needs that no CI runner has |
|---|---|
| Windows IPP attribute search | A Brother MFC-L8900CDW, a Windows 11 / Server 2022+ box, a second host to serve replays, and **~38 sheets of paper that a human looks at** |
| macOS checklist (31 items) | A Mac console session, a second local account, a reboot, an MDM tenant, a directory-bound Mac, an Apple Developer ID certificate |
| The macOS exit-2 trap | A decision, then a real `launchctl` to prove the decision took |

The standing rule this repo has now paid for **four** times applies to all of it:
a queue that exists, lists and converges is not a queue that prints. Every proxy
short of paper — `Get-Printer`, the port name, the port monitor, the driver name,
`PrinterStatus`, `DetectedErrorState`, `lpstat`, an empty job queue, a clean
convergence pass — has already returned a healthy answer for a queue that could
not print. **The only sufficient check is a printed page.**

Read, in this order, before doing anything:

1. `deploy/WINDOWS-MSI-TESTING.md` — the record of what has been run on Windows,
   what each attempt got wrong, and the three explanations already tested and
   killed.
2. `deploy/WINDOWS-IPP-BISECT-PROMPT.md` — the step-by-step brief for the search.
   Part 1 below is a superset of it, not a replacement; where they differ, that
   file is the more detailed one and this one is the more current.
3. `deploy/MACOS-CLIENT-TESTING.md` — the full macOS checklist with its `[x]`/`[ ]`
   state. Part 2 below is an *ordering* of the open items, not a second copy;
   tick the boxes **there**, since that file is the one the rest of the repo
   cites.

---

# Part 1 — The Windows IPP attribute search

## 1.1 What is actually unknown

A Brother MFC-L8900CDW that our probe classifies `driverless` accepts a job from
Windows' inbox IPP class driver and **discards it in the print processor** —
`PrintService/Operational` `id=842`, Win32 `0x80004005`, and no `id=307`
"Document printed" — while reporting `PrinterStatus=Normal`,
`DetectedErrorState=0` and zero jobs pending. The same device prints from CUPS
over the same IPP endpoint, and from the same Windows box over raw TCP/9100.

What `scripts/ipp_replay.py` has already established, with a harness whose guards
were fixed first:

- Replaying the Brother's captured `Get-Printer-Attributes` response reproduces
  the failure **3/3**, with the client demonstrably querying the replay.
- The replay server **never receives a job**. Windows fails while *rendering*,
  before it contacts the device — so the signal a probe would need is already in
  data the probe collects.
- Importing all **78** non-identity attributes from a working device's capture
  makes it **print**.
- It is a **combination**: neither half of those 78 suffices alone.

So the failure is fully determined by what the device advertises, and the
responsible attributes are **not identified**. Finding them is the task.

**Three explanations are dead. Do not re-propose them without new evidence.**
The routed hop defeating link-local discovery; a missing `application/pdf`; and a
bare `ipp-features-supported` gate. All three were tested and killed, and the
third was killed twice — an early pass appeared to *exclude* it using a fixed 30s
wait, which manufactures false negatives, so that exclusion was withdrawn and the
question folded back into this search.

**Do not change the `driverless` criterion as part of this task.** Tightening it
on the leading hunch would downgrade every working AirPrint-only printer to
`driver_required` — vendor package or skip — for no benefit. That trade needs a
decision after the answer exists, not before.

## 1.2 Why this is delta debugging and not a bisect

"Neither half is sufficient" is a **halt condition**, not a partial result. A
binary search recurses into whichever half still shows the effect; when both
halves come back negative it has nowhere to go — and both halves coming back
negative is the *definition* of an interaction, which is exactly what the
evidence says is present. The earlier search was therefore not merely unfinished;
it was being run with an algorithm that cannot converge on this input.

`ddmin` can. When no subset passes it tests the **complements**, and when those
fail too it **refines the partition** rather than stopping, returning a
**1-minimal** set — one where removing any single member loses the effect.
`scripts/ipp_bisect.py` implements it and is unit-tested in
`tests/test_ipp_bisect.py`, including a test that a bisect provably halts on this
input shape and ddmin does not.

**Measured cost**, over 40 random placements in 78 attributes:

| cause size | distinct trials (median) | p90 | max |
|---|---|---|---|
| 1 attribute | 10 | 11 | 12 |
| **2 attributes** | **38** | **44** | **47** |
| 3 attributes | 70 | 83 | 88 |
| 4 attributes | 100 | 117 | 123 |

A trial is a physical print job. At a couple of minutes each, a two-attribute
cause is **about an afternoon**. Results are memoised and journalled — that
roughly halves the physical jobs (90 oracle calls collapse to ~45 prints) and
lets an interrupted run resume without reprinting anything.

## 1.3 The rig

- A **Windows 11** machine, or Server 2022/2025. **Not Server 2019** —
  `Add-Printer -IppURL` does not exist there, and tier 1 refuses outright when it
  is missing. It must reach the replay host over TCP.
- A **second host** running `scripts/ipp_replay.py` (Linux or macOS; pure-stdlib
  Python 3). The known-working topology is replay-on-host, Windows-in-a-VM.
  Running the replay on the Windows box itself against `127.0.0.1` is **untested**
  — if you do it, prove the client really queried it before trusting a verdict.
- The **Brother MFC-L8900CDW**, reachable over IPP. This is the subject.
- A **working** IPP Everywhere target to donate attributes. `ippeveprinter`
  (ships with CUPS 2.3.4 and with macOS) was used before and is known to print
  from this same Windows client.
- Paper, and a human willing to look at it.

## 1.4 Step 1 — enable the print log, before believing anything

It is **off by default**, and that is why this defect went unnoticed for as long
as it did.

```powershell
$ch = 'Microsoft-Windows-PrintService/Operational'
$c = New-Object System.Diagnostics.Eventing.Reader.EventLogConfiguration $ch
$c.IsEnabled = $true; $c.SaveChanges()
```

`id=307` is "Document printed". Its **absence** is the failure; `id=842` with a
non-zero Win32 error code names it.

## 1.5 Step 2 — capture both devices

```bash
python3 scripts/ipp_replay.py capture <brother-ip> brother.ipp
# start ippeveprinter on the replay host, then:
python3 scripts/ipp_replay.py capture 127.0.0.1 good.ipp 8632 /ipp/print
```

**Sanity-check the donor first.** If `good.ipp` came from a device this Windows
client cannot actually print to, the whole search measures nothing. The premise
check in §1.7 will catch it, but catching it here is cheaper.

## 1.6 Step 3 — write the oracle

This is the only piece not in the repo, and its absence is deliberate: it spans
two machines, and how you reach the Windows box (WinRM, SSH, a shared folder, a
person at the keyboard) is site-specific. Any version committed here would be a
claim that had never run.

### The contract, verbatim

```
The oracle command is run once per trial.

  stdin               the attribute names to import from the donor, one per line
                      (may be empty, meaning "import nothing")
  $PN_TRIAL_UUID      a fresh printer-uuid to serve, so Windows cannot answer
                      from its device cache
  $PN_TRIAL_IDENTITY  a fresh printer-make-and-model to serve, so a server left
                      over from the previous trial can be told apart from the
                      one you meant to start
  stdout              must contain, on lines of their own:
                          VERDICT: PASS | FAIL | INDETERMINATE
                          QUERIES: <number of Get-Printer-Attributes seen>
                          IDENTITY: <printer-make-and-model read back over IPP>
  exit code           ignored; the VERDICT line is what is read
```

To print that from the tool itself:

```bash
python3 scripts/ipp_bisect.py <subject> <donor> --oracle <cmd> --contract
```

> **Documentation defect found while writing this brief, not yet fixed.**
> `WINDOWS-IPP-BISECT-PROMPT.md` (Step 3) and `WINDOWS-MSI-TESTING.md` both
> document this as bare `python3 scripts/ipp_bisect.py --contract`. That form
> **fails with exit 2 and prints usage**: argparse declares `subject`, `donor`
> and `--oracle` as required and evaluates them before the `--contract` early
> return. Pass three placeholders as above; nothing is read from them.

### What the oracle must do, per trial

1. Read attribute names from **stdin**, one per line. The list may be empty.
2. Start the replay server with those names imported from the donor, serving the
   fresh UUID and identity the driver handed you:

   ```bash
   python3 scripts/ipp_replay.py serve brother.ipp <bind-ip> 8631 airprint \
       --from good.ipp "<comma,separated,names>" \
       printer-uuid="$PN_TRIAL_UUID" \
       printer-make-and-model="$PN_TRIAL_IDENTITY"
   ```

   `--from` **must be the first argument after the mode** — `ipp_replay.py`
   positionally consumes `sys.argv[6:]` and checks `argv[0] == "--from"`, so
   putting it later silently skips the import and you will be testing the
   subject's own attributes on every trial. An empty subset is passed as an
   empty string: `--from good.ipp ""`.

   Keep the mode as `airprint` — that is the Brother's real value. When
   `ipp-features-supported` is in the subset the donor's value wins over the
   mode, so the search covers that withdrawn question rather than needing a
   separate experiment for it. What it can conclude about it is bounded; see
   §1.8.
3. On Windows, tear the previous queue down and create a fresh one:

   ```powershell
   Get-Printer -Name 'PN-BISECT' -ErrorAction SilentlyContinue | Remove-Printer
   Add-Printer -Name 'PN-BISECT' -IppURL "ipp://<replay-host>:8631/ipp/print"
   ```
4. Print something real, and **poll** `PrintService/Operational`, filtered to
   events after the job started, for `id=307` (PASS) or `id=842` with a non-zero
   Win32 error (FAIL).
5. Emit the three lines on stdout.

### The three guards, each of which has already produced a confident wrong answer

Two of them are enforced by `ipp_bisect.py` in code. The third is yours, and it
is the only one the driver cannot see.

1. **Never sleep a fixed interval — this one is yours.** A 30s wait recorded
   `DID NOT PRINT` whenever a job merely took longer, and that single coercion
   invalidated an entire earlier search. Poll for the outcome. If your deadline
   passes with neither event, report **`INDETERMINATE`** — never FAIL. An
   `INDETERMINATE` is retried and then **stops the whole run**; that is correct
   behaviour, not a failure of the run. Coercing "the job had not finished yet"
   into evidence of absence is precisely the mistake being guarded against.
2. **`pkill` + `sleep 1` does not free the port.** The replacement server hits
   `Address already in use`, dies in the background, and the **previous
   configuration keeps serving** — so verdicts get attributed to a config that
   was never running. Wait for the port to actually close, check the server log
   for a traceback, then **read `printer-make-and-model` back from the server
   over IPP**. Echoing `$PN_TRIAL_IDENTITY` back satisfies the check and defeats
   the entire point of it. A reported `IDENTITY` that is not the one this trial
   minted is forced to `INDETERMINATE` however the oracle voted.
3. **Confirm Windows actually queried you.** Windows keys IPP devices on
   `printer-uuid`; reuse one and it answers from its cache without re-reading
   your attributes. Mint a fresh UUID per trial (`$PN_TRIAL_UUID` is provided,
   and the queue teardown in step 3 is part of this) and report the count.
   **`QUERIES: 0` is forced to `INDETERMINATE`** however the oracle voted.

A `PASS` obtained without these guards means nothing, and a `FAIL` means less.

## 1.7 Step 4 — run the search

```bash
python3 scripts/ipp_bisect.py brother.ipp good.ipp \
    --oracle ./run-trial.sh --journal bisect.json
```

**The premise check runs first, before any search**, and it is not ceremony:
importing **everything must PASS** and importing **nothing must FAIL**. If some
donor attribute *breaks* the effect rather than fixing it, the full set does not
print and the whole search is measuring something other than what was recorded.
One trial telling you that is worth three hours of not knowing.

Exit codes:

| code | meaning |
|---|---|
| `0` | a 1-minimal set was found; it is printed with the trial count |
| `2` | **premise violated** — stop and investigate the donor, the device or the oracle. Do not "just run it again" |
| `3` | a trial was inconclusive after its retries and the run **refused to guess** |

The journal means an interrupted run resumes without reprinting. Keep it.

## 1.8 Step 5 — what the answer does and does not license

Report the 1-minimal set **and the trial log**.

`ddmin` returns *a* 1-minimal set — one where removing any single member loses
the effect — not *the* unique cause, and not every minimal cause. A different
partition order can land on a different set. So an attribute's **absence** from
the result means only "not necessary alongside the ones that survived". It is
**not** proof that no minimal cause involves it.

That applies squarely to `ipp-features-supported`. If it does not appear, the
honest statement is *"not required in the cause we found"*, which is strictly
weaker than settling the `ipp-everywhere` question. **Say the weaker thing.**

Two further bounds come from how `ipp_replay.py` substitutes attributes:

- It rewrites the **subject's** attribute list, so a donor attribute the Brother
  does not advertise at all is never added and can never appear in the result.
- Importing a name the **donor** lacks *drops* it from the served response — so
  such a member of the result set means "the Brother advertising this at all is
  part of the cause", which is a different claim from "this value is wrong".

Only once the set exists does the real question open: whether the `driverless`
criterion can be tightened to predict this failure **without** downgrading
working AirPrint-only printers. That needs a decision, not a hunch, and it is a
separate piece of work.

---

# Part 2 — The 31 open macOS checklist items

These are the unchecked boxes in `deploy/MACOS-CLIENT-TESTING.md` as of this
writing (23 checked, 31 unchecked — counted, not estimated). What follows is an
**ordering**, not a second copy: run them here, tick them **there**.

The ordering rule is cost of setup, ascending, with everything that needs the
same prerequisite grouped so the prerequisite is paid for once. The two items
needing an Apple Developer ID certificate are last and together, because that is
a purchase and an enrolment, not an afternoon.

Baseline already established on 2026-07-30 (macOS 26.5.2, Apple Silicon, system
Python 3.9.6, no Homebrew): the `.pkg` builds, installs, the LaunchDaemon runs as
root, a queue is provisioned from a live IPP query, the console user's default is
set by root through `sudo -u`, and **a page came out of a real printer** with the
device's own supply telemetry coming back through the queue while it printed.
Everything below is what that run did not reach.

## Tier A — same Mac, same session, no new prerequisites (8 items)

Cheapest first. Nothing here needs more than the rig that already ran.

**A1–A3. Skips must be reported, never silent** (§5)
- [ ] A `driver_required` printer with **no macOS package** is skipped with a
      stated reason, and no queue is created for it.
- [ ] A `driver_required` printer does **not** cause a *Windows* driver package
      to be downloaded onto the Mac. Confirm the Machines page shows the Mac as
      `macos`, then confirm nothing appears under
      `/Library/Application Support/PrinterNanny/drivers`.
- [ ] Ten unreachable printers do not stop the poll completing, and the ones not
      reached **say so**. A queue silently absent from the outcomes reads to
      central as a queue that was never assigned.

**A4–A6. The root-command guards** (§6, "Guards worth confirming by hand")

These protect `lpadmin -P`, which copies whatever path it is handed into
`/etc/cups/ppd` — unconstrained, that text field is "read any root-readable
file". They are refusals, so they need **no vendor driver at all**: a bogus path,
a symlink and a hand-built zip are enough.
- [ ] A system PPD path outside `/Library/Printers/` etc. is refused.
- [ ] A symlink inside an allowed directory pointing elsewhere is refused
      (`realpath` runs **before** the root check — that ordering is the point).
- [ ] A `.ppd` path that escapes the archive is refused.

**A7. The `pkg` shape while it is switched off** (§6)
- [ ] With `workstation.allow_macos_pkg_install` **off** (the shipped default), a
      `pkg` package is a stated error mentioning the setting, and `installer`
      never runs. Needs a package *record*, not a working vendor package.

**A8. Second local account** (§3) — one `sysadminctl` away.
- [ ] After a **fast user switch**, the next poll sets the default for the user
      now at the console, and does **not** touch the other's.

## Tier B — needs the printer power-cycled (2 items, §2)

Both are re-checks of shipped defect 3, the CUPS spelling of the tier-1 bug:
`cupsd` commits `device-uri` and *then* runs the `-m everywhere` query, so a
repair against an unreachable printer used to move the queue and generate no PPD
— leaving a queue that exists, lists, converges "unchanged" forever, and cannot
print.
- [ ] Re-addressing the printer in central repairs the queue **in place** — one
      queue, not a broken one beside a new one.
- [ ] Powering the printer **off** and re-addressing it leaves the existing queue
      exactly as it was (URI unchanged, **still prints**) and reports an error.

## Tier C — needs a log-out or a reboot (6 items)

**C1. The login window** (§3)
- [ ] At the login window it reports "nobody is signed in" rather than an error.

**C2. A non-English Mac** (§3) — System Settings → Language & Region → Deutsch,
then reboot. This is shipped defect 2: every string CUPS prints is translated, so
enumeration matched nothing and every poll re-created every queue. The unit tests
cover the parse; only a real Mac covers the **daemon's inherited locale**.
- [ ] Queues still converge and the default still applies.

**C3–C5. Uninstall** (§8) — needs a reboot to prove the daemon stays down.
```
sudo bash deploy/install-workstation-macos.sh --uninstall
```
- [ ] The daemon stops and does **not** come back after a reboot.
- [ ] The **state directory survives** — it holds the machine's GUID, and
      removing it turns a reinstall into a brand-new machine, stranding this
      Mac's assignments on a record nobody will look at again.
- [ ] Reinstalling keeps the **same** machine record on the Machines page.
- [ ] Queues are **not** removed, and the uninstaller says so with the command to
      remove them.

*(Four boxes, one prerequisite — the reinstall check shares the reboot.)*

## Tier D — needs a vendor driver you can hold (6 items, §6)

Two of the three driver shapes. Group them: one PostScript printer and one vendor
archive covers most of it.

**D1–D3. A `.ppd` in an uploaded package**
- [ ] Upload a zip containing a `.ppd`, with its path inside the archive.
- [ ] For a **PostScript** printer this produces a working queue with no vendor
      package installed at all. **Print a page.**
- [ ] For a printer needing vendor filters the queue is **refused** with an error
      naming the missing filter — *not* created. This is the one that matters:
      confirm no queue is left behind, and that the next poll **retries** rather
      than reporting it converged. (`printer-state-reasons` carries
      `cups-missing-filter-warning` exactly here, and is an IPP keyword list
      rather than prose, so it does not translate.)

**D4–D6. A vendor `.pkg`, with the gate switched on**

`installer -pkg` runs arbitrary pre/postinstall scripts as root — genuinely
broader than `pnputil /add-driver` — which is why the gate exists and ships off.
- [ ] Turn `workstation.allow_macos_pkg_install` on. The client installs the
      package as root and binds the PPD path you recorded.
- [ ] A **second** poll does not reinstall it — check `/var/log/install.log` has
      one entry, not one per poll.
- [ ] An archive containing two `.pkg` files is **refused** rather than guessed.

## Tier E — needs an MDM tenant (6 items)

**E1–E5. The `system` PPD shape** (§6) — the recommended path, and the only one
that reaches a full vendor driver *with* its filters at zero code execution.
- [ ] Push a vendor driver `.pkg` with your MDM (Jamf / Mosyle / Kandji).
- [ ] Find the PPD it installed, usually under
      `/Library/Printers/PPDs/Contents/Resources/`.
- [ ] Record it on the Machines page: platform macOS, kind "PPD my MDM already
      installed", and that absolute path. **No file upload.**
- [ ] The queue appears within one poll, and **prints a test page**.
- [ ] `lpoptions -p <queue> | tr ' ' '\n' | grep state-reasons` does **not**
      contain `cups-missing-filter-warning`.

**E6. MDM push of the unsigned package** (§7) — this is the path that does not
need signing, which is exactly why it is worth proving before buying a
certificate.
- [ ] An MDM `InstallEnterpriseApplication` push of the **unsigned** package
      installs successfully.

## Tier F — needs a directory-bound Mac (1 item, §4)

- [ ] On a Mac bound to AD/Entra, `dscl . -read /Users/$USER NetworkUser` returns
      a UPN and the user's **own** assignments resolve — not just the machine's.

*(The standalone-Mac half of this section is already done: `dscl` ran, found no
UPN, logged one INFO line, and the machine's own printers provisioned normally.)*

## Tier G — needs an Apple Developer ID certificate (2 items, §7)

Grouped and last because the prerequisite is an Apple Developer Program
enrolment plus a Developer ID **Installer** certificate in a keychain, not an
afternoon of testing. Three things to carry into it:

- **Signing and notarization are different things, and Gatekeeper on 10.15+ needs
  both.** Signed-but-unnotarized is still refused.
- **Stapling** is what makes the ticket work offline. Without it a Mac that
  cannot reach Apple refuses a perfectly notarized package — which describes a
  segmented client VLAN, i.e. exactly where these install.
- Credentials come from the environment (`PN_SIGN_IDENTITY`, `PN_TEAM_ID`,
  `PN_ASC_KEY_ID` / `PN_ASC_ISSUER_ID` / `PN_ASC_KEY_PATH`), never from
  arguments — a process's command line is readable by every user on the machine.
  `set -x` is never enabled in `build-macos-pkg.sh` for the same reason, and a
  test asserts it.

- [ ] `--notarize` produces a package where `spctl --assess --type install -vv`
      accepts it, and `xcrun stapler validate` passes.
- [ ] A **double-click** on the notarized package opens Installer with no
      Gatekeeper warning.

*(The other half of that second box is already done and is the more surprising
one: on the **unsigned** build, `spctl --assess --type install -vv` returns
`rejected`, `source=no usable signature`, exit 3, and `pkgutil --check-signature`
reports `Status: no signature`. Gatekeeper refusing the unsigned build is the
correct behaviour and is confirmed — if it had *accepted* it, the signing story
was being tested wrong.)*

---

# Part 3 — The macOS exit-2 trap (unresolved; needs a decision, then a rig)

This is not a checklist item. It is a contradiction between two files that ship
together, and it is currently documented as unresolved.

**The contradiction.** `workstation_cli` returns exit **2** for a refused
enrollment key, commented *"restarting on a bad key just retries forever and
buries the reason in a restart loop"* — a deliberate signal to a service manager
not to loop. The LaunchDaemon plist next to it sets
`KeepAlive{SuccessfulExit=false}`, which restarts on **any** non-zero exit. So on
macOS it loops anyway — just one stated line per restart instead of a traceback.
**launchd cannot express "restart unless the exit code is 2".** The comment
therefore describes something that does not happen.

Why it matters more than it looks: a refused key is *terminal*, not transient.
Everything else in the poll loop is retried on purpose, and the eighth defect
found on the real Mac was precisely that enrollment sat **outside** that retry
handler — an unresolvable central killed the process, launchd respawned it every
60s, and the log grew **9.8 MB/day** of tracebacks naming no reason. Moving
`ensure_enrolled` inside the loop fixed that and dropped it to ~44 KB/day. A
refused *key* still re-raises, because terminal is not transient — and that is
exactly the path this trap sits on.

**This needs a decision before it needs a test.** The options, with what each
costs:

| Option | What it costs |
|---|---|
| Exit **0** on a refused key after logging once | launchd stops restarting (`SuccessfulExit=false` only restarts on failure), but the process now claims success — and anything reading the exit code is told the wrong thing |
| Drop `KeepAlive` to a plain `true`/`false` and manage restarts ourselves | Gives up launchd's supervision for every other failure mode, which is the one thing it does well |
| Write a sentinel on refusal and exit 0; refuse to start while it exists | Keeps the loop dead **and** keeps the reason, but adds a state file an operator must know to delete after re-minting a key |
| Leave it, and fix the **comment** so it stops describing a behaviour that does not occur | Free, honest, and leaves a fleet-wide restart loop reachable by one revoked key |

Whichever is chosen, the verification is not a unit test:

- [ ] Install with a **deliberately revoked** enrollment key.
- [ ] `launchctl print system/com.printernanny.workstation` and confirm what
      launchd actually does over ~5 minutes — count restarts, do not infer them.
- [ ] Measure the log growth in `/Library/Logs/PrinterNanny/workstation.log` and
      compare against the ~44 KB/day the fixed enrollment path produces.
- [ ] Confirm the **reason** appears exactly once and is legible, not buried.

Note the related decision already taken and worth not re-opening casually: log
rotation was considered and **not** shipped, because `StandardOutPath` means
launchd opens the file and the process inherits the descriptor — a `newsyslog`
rename would leave the daemon writing to the rotated inode unless it also learns
to reopen on a signal. That is a real change needing its own verification, and
with the growth rate down ~230× it is not urgent.

---

# Reporting

For every item above, report what you **observed**, not what you concluded — and
where the two differ, say so. That is not a style preference: this codebase has
shipped the same "converges clean, cannot print" failure four times, and each
time a proxy for "it works" was reported as the thing itself.

Specifically:

- A Windows verdict is a **printed page** or an event-log id, never
  `Get-Printer`, a port name, a port monitor, `PrinterStatus` or a convergence
  result.
- A macOS queue verdict is a **printed page**, and for the driver shapes,
  `printer-state-reasons` **without** `cups-missing-filter-warning`.
- A trial that could not be run is `INDETERMINATE`, and an `INDETERMINATE` stops
  the run. It is never a FAIL.
- If a step could not be attempted, say which prerequisite was missing. An
  untried step recorded as anything other than untried is how the two stale
  planning documents in `docs/` came to list shipped work as open.

`CLAUDE.md` governs everything else: verify rather than assume
(`ruff check central agent tests scripts migrations`, then `pytest` unpiped),
report failures honestly with the output, and bump the agent version only if
`agent/` actually changes.
