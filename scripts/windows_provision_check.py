"""Provision a real print queue against a real printer, then take it away again.

This is the last unproven link in the driverless path. The probe now tells us a
device is ``driverless`` (verified against real hardware), and the Windows CI job
proves queues can be created and removed (verified against a real spooler) -- but
CI has no printer, so nothing has ever taken an actual IPP URL from an actual
device and turned it into a working queue.

That gap hid a serious defect. Until agent 0.11.0 tier 1 built the queue with::

    Add-PrinterPort -PrinterHostAddress ipp://host:631/ipp/print
    Add-Printer -DriverName "Microsoft IPP Class Driver" -PortName <derived>

``Add-PrinterPort`` cannot create an IPP port at all -- its only port type is the
Standard TCP/IP monitor, which speaks RAW or LPR on port 9100. The queue was
created, appeared in ``Get-Printer``, passed every convergence test, and could
not print. Every check we had was structurally incapable of noticing.

WHAT THIS ASSERTS, AND WHY EACH ONE
-----------------------------------
``Get-Printer`` returning the queue proves nothing -- it was true in the broken
case too. So:

* **The port name is not ours.** A ``PN_``-prefixed name means the derived-name
  code path ran, which is the bug by definition.
* **The port is not on the Standard TCP/IP monitor.** Given RAW and LPR are that
  monitor's only protocols, its presence *is* the disproof, whatever else looks
  right.
* **The driver is the inbox IPP class driver**, chosen by Windows rather than
  named by us.
* **A second pass is ``unchanged``** and does not re-run discovery, because this
  code runs on every poll.

It uses the production functions directly. A reimplementation here would certify
something nobody ships.

REMOVABILITY
------------
Creates exactly one queue named ``PNTest-*`` and removes it in a ``finally``.
Stages no driver. The caller's inventory diff is what actually proves the
machine came back clean; this only promises not to make that hard.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ip", required=True, help="address of a real printer to provision against")
    ap.add_argument("--timeout", type=float, default=8.0)
    args = ap.parse_args(argv)

    from printer_nanny_agent import ipp, workstation as ws

    print("=" * 72)
    print("Real-printer provisioning check  (creates one queue, then removes it)")
    print("=" * 72)

    # --- 1. the device must actually be driverless ---------------------------
    print("\n[1/5] probing {} ...".format(args.ip))
    result = ipp.probe(args.ip, timeout=args.timeout)
    print("      status:   {}".format(result.status))
    print("      reason:   {}".format(result.reason))
    print("      endpoint: {}".format(result.endpoint))

    if result.status != ipp.STATUS_DRIVERLESS:
        print("\n      Not driverless, so tier 1 does not apply. Nothing to provision.")
        print("      This is a valid outcome -- the point of the tier is to say so.")
        return 0
    if not result.endpoint:
        print("\n      driverless but no endpoint was recorded; cannot provision")
        return 1

    runner = ws.PowerShellRunner(timeout_s=180)
    name = "PNTest-" + uuid.uuid4().hex[:8]
    failures: List[str] = []

    try:
        # --- 2. capability gates --------------------------------------------
        print("\n[2/5] checking this Windows build")
        supported = ws.ippurl_supported(runner)
        print("      Add-Printer -IppURL available: {}".format(supported))
        if not supported:
            print("      Documented for Server 2022/2025, absent on 2019.")
            print("      Tier 1 correctly refuses here rather than building a")
            print("      RAW port that cannot print. Nothing further to test.")
            return 0
        print("      inbox driver present: {}".format(
            ws.driver_present(runner, ws.IPP_CLASS_DRIVER)))

        # --- 3. provision ----------------------------------------------------
        print("\n[3/5] provisioning {!r} -> {}".format(name, result.endpoint))
        outcome = ws.ensure_driverless_queue(runner, name, result.endpoint)
        print("      outcome: {}".format(outcome))
        if outcome != "created":
            failures.append("expected 'created', got {!r}".format(outcome))

        # --- 4. the assertions that actually distinguish working from broken --
        print("\n[4/5] inspecting what Windows built")
        state = ws.queue_state(runner, name)
        print("      present: {}".format(state.present))
        print("      port:    {}".format(state.port))
        print("      driver:  {}".format(state.driver))

        if not state.present:
            failures.append("queue reported created but Get-Printer cannot see it")
        if state.port.startswith("PN_"):
            failures.append(
                "port {!r} is a name WE derived -- the pre-0.11.0 bug is back"
                .format(state.port)
            )
        if state.driver.strip() != ws.IPP_CLASS_DRIVER:
            failures.append(
                "driver is {!r}, expected {!r}".format(state.driver, ws.IPP_CLASS_DRIVER)
            )

        detail = ws.port_detail(runner, state.port) if state.port else {}
        print("      monitor: {}".format(detail.get("description") or "(none)"))
        if ws.STANDARD_TCP_MONITOR in (detail.get("description") or "").lower():
            failures.append(
                "port is on the Standard TCP/IP monitor, which speaks only "
                "RAW/LPR -- this queue cannot carry IPP"
            )

        # --- 5. idempotence, without a second network round trip -------------
        print("\n[5/5] re-running (must be a no-op)")
        again = ws.ensure_driverless_queue(runner, name, result.endpoint)
        print("      outcome: {}".format(again))
        if again != "unchanged":
            failures.append("second pass returned {!r}, expected 'unchanged'".format(again))

    except ws.IppProvisionError as exc:
        print("\n      REFUSED: {}".format(exc))
        print("      retryable: {}".format(exc.retryable))
        failures.append("provisioning refused: {}".format(exc))
    except Exception as exc:  # noqa: BLE001 - report, then always clean up
        failures.append("unexpected error: {!r}".format(exc))
    finally:
        print("\n[cleanup] removing {!r}".format(name))
        try:
            ws.remove_queue(runner, name)
            still = ws.queue_state(runner, name).present
            print("          removed: {}".format(not still))
            if still:
                failures.append("queue survived removal -- remove it by hand")
        except Exception as exc:  # noqa: BLE001
            failures.append("cleanup failed: {!r}".format(exc))

    print("\n" + "-" * 72)
    if failures:
        print("FAILED:")
        for f in failures:
            print("  - {}".format(f))
        return 1
    print("PASSED -- a real driverless printer was provisioned on a port Windows")
    print("chose, bound to the inbox IPP class driver, and removed cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
