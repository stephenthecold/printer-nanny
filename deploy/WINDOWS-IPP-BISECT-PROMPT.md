# Brief: finish the IPP attribute search on real hardware

**Status: not started.** This is a task brief for whoever runs the search, not a
record of a run. Nothing below has been executed end to end — the tooling it
drives is unit-tested, the oracle it asks you to write does not exist yet, and no
1-minimal set has been found. `WINDOWS-MSI-TESTING.md` is the record of what
*has* happened; this file is the instruction for the part that has not.

---

## The task

A Brother MFC-L8900CDW that our probe marks `driverless` accepts a print job from
Windows' inbox IPP class driver and discards it in the print processor —
`0x80004005`, no `id=307` — while reporting `PrinterStatus=Normal`. The same
device prints fine from CUPS over the same IPP endpoint, and from the same
Windows box over raw TCP/9100.

Three explanations have already been proposed, tested and **killed**: a routed
hop defeating link-local discovery, a missing `application/pdf`, and
`ipp-features-supported`. Do not re-propose them without new evidence.

What *is* established, with a verified harness:

- Replaying the Brother's captured IPP attributes reproduces the failure **3/3**.
- The replay server **never receives a job** — Windows fails while *rendering*,
  before it contacts the device.
- Importing all **78** non-identity attributes from a working device's capture
  makes it **print**.
- It is a **combination**: neither half of those 78 is sufficient alone.

So the fault is fully determined by what the device advertises, and a probe could
in principle predict it — but **which attributes are responsible is unknown**.
Your job is to find them.

Read `deploy/WINDOWS-MSI-TESTING.md` before doing anything. It is the record of
what has been tried and what each attempt got wrong.

## Why this is not a bisect (do not "just binary search it")

"Neither half is sufficient" is a **halt condition**, not a partial result. Binary
search recurses into whichever half still shows the effect; when both halves come
back negative it has nowhere to go. That is the signature of an *interaction*.

`scripts/ipp_bisect.py` therefore drives the search with **delta debugging**
(`ddmin`), which tests the complements when no subset passes and refines the
partition when those fail too. It returns a **1-minimal** set. This part is
already written and unit-tested — you are supplying the missing half, the oracle.

Expected cost, measured: **~38 physical print jobs** for a two-attribute cause
(p90 44), ~70 for three. Budget an afternoon.

## The rig

- A **Windows 11** machine (or Server 2022/2025 — `Add-Printer -IppURL` is absent
  on 2019). It must be able to reach the replay server over TCP.
- A second host to run `scripts/ipp_replay.py` (Linux or macOS; it is pure-stdlib
  Python 3). The known-working topology is replay-on-host, Windows-in-a-VM.
  Running the replay on the Windows box itself against `127.0.0.1` is untested —
  if you try it, prove the client really queried it before trusting any verdict.
- The **Brother MFC-L8900CDW** (the failing device), reachable over IPP.
- A **working** IPP Everywhere target to donate attributes. `ippeveprinter`
  (ships with CUPS 2.3.4 / macOS) was used before and is known to print from this
  same Windows client.

## Step 1 — enable the print log, before believing anything

It is **off by default**, and its absence is why this defect went unnoticed.

```powershell
$ch = 'Microsoft-Windows-PrintService/Operational'
$c = New-Object System.Diagnostics.Eventing.Reader.EventLogConfiguration $ch
$c.IsEnabled = $true; $c.SaveChanges()
```

`id=307` = "Document printed". Its **absence** is the failure; `id=842` with a
non-zero Win32 error names it.

## Step 2 — capture both devices

```bash
python3 scripts/ipp_replay.py capture <brother-ip> brother.ipp
# start ippeveprinter, then:
python3 scripts/ipp_replay.py capture 127.0.0.1 good.ipp 8632 /ipp/print
```

Sanity-check that `good.ipp` came from a device this Windows client genuinely
prints to. If the donor cannot print, the whole search is measuring nothing.

## Step 3 — write the oracle

This is the only piece not in the repo, deliberately: it spans two machines and
the Windows access path is site-specific, so any committed version would be a
claim that had never run.

Print the contract:

```bash
python3 scripts/ipp_bisect.py --contract
```

It must, **per trial**:

1. Read attribute names from **stdin**, one per line (possibly empty).
2. Start the replay server with those imported from the donor, using the fresh
   UUID and identity the driver hands you:
   ```bash
   python3 scripts/ipp_replay.py serve brother.ipp <bind-ip> 8631 airprint \
       --from good.ipp "<comma,separated,names>" \
       printer-uuid="$PN_TRIAL_UUID" \
       printer-make-and-model="$PN_TRIAL_IDENTITY"
   ```
   Note: `--from` must be the **first** argument after the mode, and an empty
   subset is passed as an empty string (`--from good.ipp ""`). Keep the mode as
   `airprint` — that is the Brother's real value; when `ipp-features-supported`
   is in the subset the donor's value wins over the mode, so the search covers
   that withdrawn question rather than needing a separate experiment for it.
   (What it can conclude is bounded — see Step 5.)
3. On Windows: remove any previous queue, then
   ```powershell
   Get-Printer -Name 'PN-BISECT' -ErrorAction SilentlyContinue | Remove-Printer
   Add-Printer -Name 'PN-BISECT' -IppURL "ipp://<replay-host>:8631/ipp/print"
   ```
4. Print something real, and **poll** `PrintService/Operational` for `id=307`
   (PASS) or `id=842` with a non-zero Win32 error (FAIL), filtered to events
   after the job started.
5. Emit on stdout:
   ```
   VERDICT: PASS | FAIL | INDETERMINATE
   QUERIES: <Get-Printer-Attributes the replay server saw this trial>
   IDENTITY: <printer-make-and-model read back from the server over IPP>
   ```

### The three traps — each has already produced a confident wrong answer

1. **Never sleep a fixed interval.** A 30s wait recorded `DID NOT PRINT` whenever
   a job merely took longer, and that single coercion invalidated an entire
   earlier search. Poll for the outcome; if the deadline passes with neither
   event, report `INDETERMINATE` — never FAIL. **This one is yours to get right**;
   it is the only trap the driver cannot see.
2. **`pkill` + `sleep 1` does not free the port.** The replacement server hits
   `Address already in use`, dies in the background, and the **previous
   configuration keeps serving** — so verdicts get attributed to a config that
   was never running. Wait for the port to actually close, check the log for a
   traceback, then read the identity back over IPP. The driver mints a fresh
   `$PN_TRIAL_IDENTITY` per trial and forces `INDETERMINATE` when the `IDENTITY:`
   you report is not the one it asked for — so **query the server for it**;
   echoing the variable back satisfies the check and defeats the point of it.
3. **Confirm Windows actually queried you.** Windows keys IPP devices on
   `printer-uuid`; reuse one and it answers from cache without re-reading your
   attributes. Mint a fresh UUID per trial (`$PN_TRIAL_UUID` is provided) and
   report the query count. `QUERIES: 0` is forced to INDETERMINATE by the driver
   however you voted.

## Step 4 — run the search

```bash
python3 scripts/ipp_bisect.py brother.ipp good.ipp \
    --oracle ./run-trial.sh --journal bisect.json
```

It checks the premise first — importing everything must PASS, importing nothing
must FAIL. If that check fails, **stop and investigate**: it means the donor, the
device or the oracle is not what the record describes, or some donor attribute
*breaks* the effect rather than fixing it. One trial telling you that is worth
three hours of not knowing.

Results are journalled, so an interrupted run resumes without reprinting. Exit
codes: `0` found it, `2` premise violated, `3` a trial was inconclusive and the
run refused to guess.

## Step 5 — what to do with the answer

Report the 1-minimal set and the trial log.

**Read it for what it is.** `ddmin` returns *a* 1-minimal set — one where removing
any single member loses the effect — not *the* unique cause, and not every
minimal cause. A different partition order can land on a different set. So an
attribute's **absence** from the result means it was not necessary alongside the
ones that survived; it is not proof that no minimal cause involves it. That
applies squarely to `ipp-features-supported`: if it does not appear, the honest
statement is "not required in the cause we found", which is weaker than settling
the `ipp-everywhere` question outright. Say the weaker thing.

Two further bounds on what the search can return, both from how `ipp_replay.py`
substitutes attributes: it rewrites the **subject's** attribute list, so a donor
attribute the Brother does not advertise at all is never added and can never
appear in the result; and importing a name the *donor* lacks **drops** it from
the served response, so such a member of the set means "the Brother advertising
this at all is part of the cause".

Then, and only then, the real question opens up: whether the `driverless`
criterion in the probe can be tightened to predict this failure **without**
downgrading working AirPrint-only printers to `driver_required`. That trade is
what has kept the criterion unchanged so far, and it needs a decision, not a
hunch.

Do **not** change the `driverless` criterion as part of this task.

## What not to do

- Do not report a verdict from a trial where the replay server saw zero queries.
- Do not treat "no `id=307` yet" as evidence of failure.
- Do not conclude anything from `Get-Printer`, the port name, `PrinterStatus` or
  `DetectedErrorState`. Every one of those has returned a clean answer for a
  queue that could not print. **The only sufficient check is a printed page.**
- Do not re-litigate the routed hop, `application/pdf`, or a bare
  `ipp-features-supported` gate. All three are already dead.

## House rules for this repo

`CLAUDE.md` governs. In particular: verify rather than assume (`ruff check central
agent tests scripts migrations`, plus `pytest`), report failures honestly with the
output, and bump the agent version only if you change `agent/`.
