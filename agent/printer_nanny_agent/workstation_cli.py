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


def load_config(path: Optional[str]) -> dict:
    """Read the installer-written config, if there is one.

    WHY A FILE AND NOT COMMAND-LINE ARGUMENTS
    -----------------------------------------
    The enrollment key is a credential, and a Windows service's command line is
    readable by **any logged-in user** (Task Manager's Command line column,
    `Get-CimInstance Win32_Process`, `wmic process`). Passing it as an argument
    would publish that client's enrollment key to every person who sits at the
    machine. So the MSI writes it into this file and only the file's *path*
    appears on the command line.

    A missing or unreadable file is not fatal -- the flags and environment still
    work, which is what makes the client runnable by hand for diagnosis.
    """
    if not path or not os.path.exists(path):
        return {}
    try:
        try:
            import tomllib  # Python 3.11+
        except ImportError:  # pragma: no cover - exercised on 3.9/3.10
            import tomli as tomllib
        with open(path, "rb") as fp:
            data = tomllib.load(fp)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("could not read config %s: %s", path, exc)
        return {}


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="printer-nanny-workstation",
        description="Provision printer queues assigned to this machine and its user.",
    )
    parser.add_argument(
        "--config", default=_env("PN_CONFIG") or None,
        help="path to the installer-written config (holds the enrollment key)",
    )
    parser.add_argument("--server", default=None, help="central base URL")
    parser.add_argument(
        "--enroll-key", default=None,
        help="client enrollment key (from the Machines page)",
    )
    parser.add_argument(
        "--interval", type=float, default=None, help="seconds between polls"
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

    # Precedence: an explicit flag beats the environment, which beats the file.
    # A technician debugging on the box must be able to point the client at a
    # staging server without editing (and then forgetting to restore) the file
    # the installer owns.
    cfg = load_config(args.config)
    server = args.server or _env("PN_SERVER") or str(cfg.get("server") or "")
    enroll_key = (
        args.enroll_key or _env("PN_ENROLL_KEY") or str(cfg.get("enroll_key") or "")
    )
    interval = args.interval
    if interval is None:
        interval = float(_env("PN_INTERVAL") or cfg.get("interval") or 300)
    prefix = args.prefix
    if prefix is None and cfg.get("queue_prefix") is not None:
        prefix = str(cfg["queue_prefix"])
    verify_tls = not args.insecure and bool(cfg.get("verify_tls", True))

    if not server:
        parser.error("no central URL: pass --server, set PN_SERVER, or use --config")
    if not enroll_key:
        parser.error(
            "no enrollment key: pass --enroll-key, set PN_ENROLL_KEY, or use --config"
        )

    from printer_nanny_agent import workstation_service as svc

    kwargs = dict(
        interval=interval,
        state_dir=args.state_dir or cfg.get("state_dir") or None,
        verify_tls=verify_tls,
        once=args.once,
    )
    if prefix is not None:
        kwargs["prefix"] = prefix

    try:
        report = svc.run(server, enroll_key, **kwargs)
    except svc.ServiceError as exc:
        # Enrollment refused is terminal and worth a distinct exit code: under a
        # service manager, restarting on a bad key just retries forever and
        # buries the reason in a restart loop.
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 0

    if args.once:
        if report.cycle_error:
            # Stated, and non-zero below. A cycle that never reached central has
            # no outcomes to print, and printing nothing then exiting 0 is how a
            # diagnostic run reports success for work it did not do.
            print(f"cycle did not complete: {report.cycle_error}")
        for queue, outcome in sorted(report.outcomes.items()):
            print(f"{queue}: {outcome}")
        for queue, why in sorted(report.skipped.items()):
            print(f"{queue}: skipped ({why})")
        if report.default_applied:
            print(f"default printer: {report.default_applied}")
        elif report.desired_default:
            # Stated rather than implied, so "central shows a default the user
            # does not have" stays impossible.
            print(
                f"default requested: {report.desired_default} "
                f"(NOT applied: {report.default_reason})"
            )
        return 0 if report.ok else 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
