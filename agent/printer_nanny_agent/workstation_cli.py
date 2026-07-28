"""Entry point for the workstation service.

Separate from ``cli.py`` because these are two different programs that happen to
ship in one wheel: the site agent polls printers over SNMP and never touches a
spooler, and the workstation client provisions queues and never speaks SNMP. A
single CLI would make each one's flags look like the other's options.

Configuration is by environment variable, not a config file, because the MSI
sets these at install time and a service has no interactive place to read one
from. ``PN_ENROLL_KEY`` is the client-scoped enrollment key baked into the
per-client installer; everything after enrollment authenticates with the
machine's own key from ProgramData.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Optional

log = logging.getLogger(__name__)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="printer-nanny-workstation",
        description="Provision printer queues assigned to this machine and its user.",
    )
    parser.add_argument(
        "--server", default=_env("PN_SERVER"), help="central base URL"
    )
    parser.add_argument(
        "--enroll-key",
        default=_env("PN_ENROLL_KEY"),
        help="client enrollment key (from the Machines page)",
    )
    parser.add_argument(
        "--interval", type=float, default=float(_env("PN_INTERVAL", "300")),
        help="seconds between polls",
    )
    parser.add_argument(
        "--state-dir", default=_env("PN_STATE_DIR") or None,
        help="where machine identity and credentials live",
    )
    parser.add_argument(
        "--prefix", default=_env("PN_QUEUE_PREFIX") or None,
        help="managed queue name prefix; only queues carrying it are ever removed",
    )
    parser.add_argument(
        "--insecure", action="store_true",
        help="skip TLS verification (testing against a self-signed cert)",
    )
    parser.add_argument(
        "--once", action="store_true", help="one cycle then exit; useful in CI"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.server:
        parser.error("no central URL: pass --server or set PN_SERVER")
    if not args.enroll_key:
        parser.error("no enrollment key: pass --enroll-key or set PN_ENROLL_KEY")

    from printer_nanny_agent import workstation_service as svc

    kwargs = dict(
        interval=args.interval,
        state_dir=args.state_dir,
        verify_tls=not args.insecure,
        once=args.once,
    )
    if args.prefix is not None:
        kwargs["prefix"] = args.prefix

    try:
        report = svc.run(args.server, args.enroll_key, **kwargs)
    except svc.ServiceError as exc:
        # Enrollment refused is terminal and worth a distinct exit code: under a
        # service manager, restarting on a bad key just retries forever and
        # buries the reason in a restart loop.
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 0

    if args.once:
        for queue, outcome in sorted(report.outcomes.items()):
            print(f"{queue}: {outcome}")
        for queue, why in sorted(report.skipped.items()):
            print(f"{queue}: skipped ({why})")
        if report.desired_default:
            # Stated rather than implied: the queue exists, but making it the
            # user's default is per-user state this service does not write.
            print(f"default requested: {report.desired_default} (not applied)")
        return 0 if report.ok else 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
