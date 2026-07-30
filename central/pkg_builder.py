"""Assemble the macOS workstation installer's payload, per client.

WHY THIS PRODUCES A BUNDLE AND NOT A .pkg
=========================================
``msi_builder`` builds a finished, installable ``.msi`` inside the container. This
cannot do the same, and the reason is a hard constraint rather than a preference:

    pkgbuild / productbuild   macOS-only.
    productsign               needs a "Developer ID Installer" certificate in a
                              keychain; no Linux equivalent exists.
    xcrun notarytool          a closed Apple binary talking to an Apple service.
    xcrun stapler             likewise.

A ``.pkg`` is a XAR archive carrying a BOM, so an *unsigned* one can be
hand-assembled on Linux with ``xar`` + ``bomutils``. That was considered and
rejected: neither is packaged in current Ubuntu (both would have to be compiled
into the runtime image, ``xar`` being unmaintained), and the result would **still**
need a Mac to sign -- so it buys a fragile dependency and an artifact Apple's own
tooling did not produce, to arrive at the same place. Since a Mac is required for
signing regardless, ``pkgbuild`` on that Mac is correct by construction.

So the work is split where the constraint falls. Central does what only central
can: mint this client's enrollment key, render ``workstation.toml``, and assemble
the payload tree. The bundle carries a copy of ``scripts/build-macos-pkg.sh``,
which does everything that needs Apple's tools.

WHAT IS IN THE BUNDLE, AND WHY EACH PIECE
=========================================
    payload/Library/Application Support/PrinterNanny/workstation.toml   0600
    payload/Library/LaunchDaemons/com.printernanny.workstation.plist
    payload/usr/local/printer-nanny-workstation/wheelhouse/*.whl
    scripts/preinstall, scripts/postinstall
    build-macos-pkg.sh
    README.md

The **wheelhouse** rather than a bundled interpreter: every Mac has Python, and
bundling a *relocatable* macOS framework build is a project of its own. Every
wheel the client needs is pure-Python (``py3-none-any``), verified by a test, so a
wheelhouse built here installs on any Mac -- and ``postinstall`` runs pip with
``--no-index``, which makes the install genuinely offline. A future dependency with
a C extension would break that silently on the Mac and loudly in that test.

The **plist** and the **scripts** are copied from ``deploy/``, not generated. They
are the same files a reviewer reads, which is the rule this repo already learned
the hard way about having two copies of a plist and shipping only one.

THE BUNDLE CONTAINS A LIVE CREDENTIAL
=====================================
``workstation.toml`` carries this client's enrollment key, exactly as the MSI does.
The new part is that a ``.tar.gz`` is a thing an operator saves, and a directory
they might commit. So: it is written 0600 inside the archive, the README says what
it holds, and each build mints its **own** key so a leaked bundle is revoked
without touching any other install. A build that fails rolls its key back -- a key
minted for an installer that never existed is a live credential nobody holds and
nobody will think to revoke.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from central.msi_builder import render_workstation_config

#: Distinct from any agent package, for the same reason the MSI carries its own
#: UpgradeCode: macOS treats a shared identifier as the same product, so on a box
#: legitimately running both -- an MSP's own server polls printers and prints from
#: them -- installing one would replace the other.
PKG_IDENTIFIER = "com.printernanny.workstation"

PKG_NAME = "PrinterNannyWorkstation"

#: Where the payload puts things, mirroring install-workstation-macos.sh so the two
#: install paths converge on one layout rather than two an operator has to know.
_INSTALL_DIR = "usr/local/printer-nanny-workstation"
_STATE_DIR = "Library/Application Support/PrinterNanny"
_LAUNCHD_DIR = "Library/LaunchDaemons"

_PLIST_NAME = "com.printernanny.workstation.plist"


@dataclass
class PkgCapability:
    available: bool
    reason: str = ""


@dataclass
class PkgBundleResult:
    path: Path
    size: int
    agent_version: str
    wheel_count: int
    identifier: str = PKG_IDENTIFIER


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _agent_src() -> Path:
    return _repo_root() / "agent"


def pkg_bundle_available() -> PkgCapability:
    """Whether a bundle can be assembled here.

    Deliberately does NOT check for pkgbuild: this never runs it, and reporting
    "unavailable" on a Linux host because a macOS tool is missing would be exactly
    backwards -- assembling the bundle is the part that works here.
    """
    agent = _agent_src()
    if not (agent / "pyproject.toml").exists():
        return PkgCapability(
            False,
            "The agent source is not present in this image, so no wheels can be "
            "built. This build needs the full repository, not just the app.",
        )
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            check=True, capture_output=True, timeout=60,
        )
    except Exception as exc:  # noqa: BLE001
        return PkgCapability(False, f"pip is not usable in this image: {exc}")
    return PkgCapability(True)


def _agent_version() -> str:
    from printer_nanny_agent import __base_version__

    return str(__base_version__)


def build_wheelhouse(dest: Path, *, timeout: int = 900) -> int:
    """Build the agent and its dependencies into ``dest``. Returns the wheel count.

    ``pip wheel`` rather than ``pip download``: the agent itself has to be built
    from source here, and mixing "build this one" with "fetch those" in one command
    is what ``pip wheel`` is for.
    """
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(_agent_src()),
         "--wheel-dir", str(dest), "--disable-pip-version-check", "--quiet"],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(
            "could not build the agent wheelhouse: "
            + (detail[-1] if detail else "no output")
        )
    wheels = sorted(dest.glob("*.whl"))
    if not wheels:
        raise RuntimeError("pip reported success but produced no wheels")

    # Every wheel must be pure-Python, or the offline install fails ON THE MAC
    # while succeeding here -- the worst place for this to surface. A C-extension
    # dependency needs a macOS-built wheel, which this host cannot produce.
    impure = [w.name for w in wheels if not w.name.endswith("-py3-none-any.whl")]
    if impure:
        raise RuntimeError(
            "these wheels are platform-specific and would not install on macOS: "
            + ", ".join(impure)
            + ". Build them on a Mac, or drop the dependency."
        )
    return len(wheels)


def _tar_add(tar: tarfile.TarFile, data: bytes, arcname: str, mode: int) -> None:
    """Add bytes with an explicit mode, uid 0 and no mtime surprises.

    Modes are set here rather than inherited from a staging directory, because the
    whole security argument rests on one file being 0600 and a staging tree picks
    up the container's umask. uid/gid 0 so an operator extracting as root gets
    root-owned files; ``pkgbuild --ownership recommended`` fixes it either way, but
    a payload that is already right needs no fixing.
    """
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mode = mode
    info.uid = info.gid = 0
    info.uname = "root"
    info.gname = "wheel"
    info.mtime = 0
    tar.addfile(info, io.BytesIO(data))


def _tar_add_file(tar: tarfile.TarFile, src: Path, arcname: str, mode: int) -> None:
    _tar_add(tar, src.read_bytes(), arcname, mode)


def render_readme(*, client_name: str, agent_version: str, key_id: int) -> str:
    return f"""# Printer Nanny workstation installer -- {client_name}

This bundle builds the macOS installer for **{client_name}**. Central assembled the
payload; the rest needs Apple's tools, which only exist on macOS.

    ./build-macos-pkg.sh              # unsigned. MDM-installable, NOT double-clickable.
    ./build-macos-pkg.sh --notarize   # signed + notarized + stapled.

Run `./build-macos-pkg.sh --help` for the environment variables signing needs.

## This bundle contains a live credential

`payload/Library/Application Support/PrinterNanny/workstation.toml` holds this
client's **enrollment key**, mode 0600. Treat the bundle as a secret:

* Do not commit it. Do not put it in a shared drive or a ticket attachment.
* It can only *enroll* a machine. It cannot read printers, people or other
  machines -- so the blast radius is "somebody enrolls a machine you did not
  expect", not "somebody reads your fleet".
* This build minted **enrollment key #{key_id}**, its own. Revoke that one under
  Machines -> Enrollment keys and this bundle is dead; every other installer and
  every already-enrolled Mac keeps working, because each enrolled machine
  authenticates with its own credential.
* Delete the bundle once the .pkg is built and distributed. The .pkg carries the
  same key, so it is equally sensitive.

## What gets installed

    /usr/local/printer-nanny-workstation/     venv + the bundled wheelhouse
    /usr/local/bin/printer-nanny-workstation  symlink to the entry point
    /Library/Application Support/PrinterNanny 0700, holds the key and the machine id
    /Library/LaunchDaemons/com.printernanny.workstation.plist
    /Library/Logs/PrinterNanny/workstation.log

The install is **offline**: every wheel is in the payload and `postinstall` runs
pip with `--no-index`. It uses the Mac's own Python (3.9+), preferring a real
interpreter over `/usr/bin/python3`, which is a shim that can trigger the Command
Line Tools prompt -- harmless interactively, fatal in an unattended MDM push.

Agent version: {agent_version}

## Verifying an install

    sudo launchctl print system/com.printernanny.workstation
    tail -f /Library/Logs/PrinterNanny/workstation.log
    lpstat -p

The machine appears on the Machines page within one poll. A `driver_required`
printer with no matching macOS driver package is **skipped with a stated reason**
rather than bound to a wrong driver -- see the Machines page to add one.
"""


def build_workstation_pkg_bundle(
    *,
    client_name: str,
    client_id: int,
    central_url: str,
    enroll_key: str,
    enroll_key_id: int,
    out_dir: Path,
    interval: int = 300,
    queue_prefix: Optional[str] = None,
    verify_tls: bool = True,
    wheelhouse_timeout: int = 900,
) -> PkgBundleResult:
    """Assemble this client's installer bundle. Returns the .tar.gz."""
    if not (enroll_key or "").strip():
        raise ValueError("build_workstation_pkg_bundle needs an enroll_key")

    repo = _repo_root()
    plist_src = repo / "deploy" / _PLIST_NAME
    preinstall_src = repo / "deploy" / "macos-pkg" / "preinstall"
    postinstall_src = repo / "deploy" / "macos-pkg" / "postinstall"
    build_src = repo / "scripts" / "build-macos-pkg.sh"
    for required in (plist_src, preinstall_src, postinstall_src, build_src):
        if not required.exists():
            raise RuntimeError(f"missing from the image: {required.relative_to(repo)}")

    agent_version = _agent_version()
    out_dir.mkdir(parents=True, exist_ok=True)
    bundle = out_dir / f"{PKG_NAME}-{agent_version}-client{client_id}-bundle.tar.gz"

    staging = Path(tempfile.mkdtemp(prefix="pn-pkg-wheels-"))
    try:
        wheel_count = build_wheelhouse(staging, timeout=wheelhouse_timeout)

        config = render_workstation_config(
            central_url=central_url,
            enroll_key=enroll_key,
            interval=interval,
            queue_prefix=queue_prefix,
            verify_tls=verify_tls,
        )

        with tarfile.open(bundle, "w:gz") as tar:
            # 0600: the one mode in here that is load-bearing.
            _tar_add(
                tar, config.encode(),
                f"payload/{_STATE_DIR}/workstation.toml", 0o600,
            )
            # Verbatim from deploy/, so the file a reviewer reads is the file that
            # ships. Two copies is how one of them drifts.
            _tar_add_file(
                tar, plist_src, f"payload/{_LAUNCHD_DIR}/{_PLIST_NAME}", 0o644
            )
            for wheel in sorted(staging.glob("*.whl")):
                _tar_add_file(
                    tar, wheel,
                    f"payload/{_INSTALL_DIR}/wheelhouse/{wheel.name}", 0o644,
                )
            # 0755: macOS Installer will not run a script it cannot execute, and
            # says so only in /var/log/install.log.
            _tar_add_file(tar, preinstall_src, "scripts/preinstall", 0o755)
            _tar_add_file(tar, postinstall_src, "scripts/postinstall", 0o755)
            _tar_add_file(tar, build_src, "build-macos-pkg.sh", 0o755)
            _tar_add(
                tar,
                render_readme(
                    client_name=client_name,
                    agent_version=agent_version,
                    key_id=enroll_key_id,
                ).encode(),
                "README.md", 0o644,
            )
            # Defaults for the build script, so `./build-macos-pkg.sh` with no
            # arguments produces a correctly identified and versioned package.
            env = (
                f'PN_PKG_IDENTIFIER="{PKG_IDENTIFIER}"\n'
                f'PN_PKG_VERSION="{agent_version}"\n'
                f'PN_PKG_NAME="{PKG_NAME}"\n'
            )
            _tar_add(tar, env.encode(), "pkg.env", 0o644)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return PkgBundleResult(
        path=bundle,
        size=bundle.stat().st_size,
        agent_version=agent_version,
        wheel_count=wheel_count,
    )
