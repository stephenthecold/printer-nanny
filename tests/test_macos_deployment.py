"""The macOS deployment contract: the LaunchDaemon plist and its installer.

These assert the two things about this deployment that are invisible until it is
too late. A malformed plist is silently refused by launchd, so the client simply
never runs and there is nothing in any log we wrote. And a credential in
``ProgramArguments`` is readable by every local user via ``launchctl print`` --
the same exposure that keeps the key off the Windows service's command line.

Both plists are checked: the reviewable one in ``deploy/`` and the one the
installer generates, because they are two files and only one of them ships.
"""

from __future__ import annotations

import io
import plistlib
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PLIST = ROOT / "deploy" / "com.printernanny.workstation.plist"
INSTALLER = ROOT / "deploy" / "install-workstation-macos.sh"
TESTBED = ROOT / "scripts" / "macos_cups_testbed.sh"
CHECKER = ROOT / "scripts" / "macos_provision_check.py"

LABEL = "com.printernanny.workstation"


def _installer_plist() -> dict:
    """The plist the installer writes, with its shell variables filled in."""
    body = re.search(
        r'cat > "\$PLIST" <<PLISTEOF\n(.*?)\nPLISTEOF', INSTALLER.read_text(), re.S
    )
    assert body, "the installer no longer writes a plist heredoc"
    text = body.group(1)
    for var, value in {
        "$LABEL": LABEL,
        "$BIN_LINK": "/usr/local/bin/printer-nanny-workstation",
        "$CONFIG": "/Library/Application Support/PrinterNanny/workstation.toml",
        "$LOG_DIR": "/Library/Logs/PrinterNanny",
    }.items():
        text = text.replace(var, value)
    assert "$" not in text, f"unsubstituted variable in the generated plist: {text}"
    return plistlib.load(io.BytesIO(text.encode()))


@pytest.fixture(params=["repo", "installer"])
def plist(request) -> dict:
    if request.param == "repo":
        with PLIST.open("rb") as fp:
            return plistlib.load(fp)
    return _installer_plist()


# --------------------------------------------------------------------------- #
# It has to parse, or launchd refuses it in silence
# --------------------------------------------------------------------------- #


def test_the_plist_is_well_formed(plist):
    assert plist["Label"] == LABEL


def test_it_runs_the_workstation_entry_point(plist):
    assert plist["ProgramArguments"][0].endswith("printer-nanny-workstation")


def test_it_runs_as_root(plist):
    """lpadmin needs it, the state directory needs it, and running lpoptions as
    the console user needs it."""
    assert plist["UserName"] == "root"


def test_it_starts_at_boot_and_restarts_on_failure(plist):
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}


def test_a_deliberate_stop_is_not_fought(plist):
    """KeepAlive true (rather than SuccessfulExit false) means launchctl bootout
    races launchd forever and the client cannot be stopped."""
    assert plist["KeepAlive"] is not True


def test_the_restart_throttle_is_long_enough_to_read_the_log(plist):
    """launchd's 1s default turns a bad enrollment key into a restart loop that
    buries the reason; the CLI exits 2 for exactly that case."""
    assert plist["ThrottleInterval"] >= 30


# --------------------------------------------------------------------------- #
# No credential anywhere a local user can read it
# --------------------------------------------------------------------------- #


def test_the_plist_carries_no_credential(plist):
    """ProgramArguments and EnvironmentVariables are readable by any local user
    (launchctl print), and launchd requires the plist itself to be readable."""
    blob = str(plist).lower()
    for forbidden in ("enroll_key", "enrollkey", "api_key", "apikey", "pnw_", "secret"):
        assert forbidden not in blob, f"{forbidden!r} appears in the plist"


def test_the_key_travels_by_config_path_only(plist):
    args = plist["ProgramArguments"]
    assert "--config" in args
    assert args[args.index("--config") + 1].endswith("workstation.toml")
    assert "--enroll-key" not in args


def test_the_plist_sets_no_environment_variables(plist):
    """Nothing needs one, and EnvironmentVariables is the other half of what
    launchctl print exposes."""
    assert "EnvironmentVariables" not in plist


def test_the_installer_writes_the_config_before_starting_the_daemon():
    """Otherwise there is a window in which the daemon runs without its key, and
    a window in which the key exists at the default umask."""
    text = INSTALLER.read_text()
    assert text.index("chmod 600") < text.index("launchctl bootstrap")


def test_the_installer_locks_down_the_config_and_state_dir():
    text = INSTALLER.read_text()
    assert 'chmod 600 "$CONFIG"' in text
    assert 'chmod 700 "$STATE_DIR"' in text
    # launchd refuses a group- or world-writable plist outright.
    assert 'chmod 644 "$PLIST"' in text


def test_the_installer_does_not_pass_the_key_on_a_command_line():
    """It may accept --enroll-key as its OWN argument; what it must never do is
    put it into the plist it writes."""
    body = re.search(
        r'cat > "\$PLIST" <<PLISTEOF\n(.*?)\nPLISTEOF', INSTALLER.read_text(), re.S
    ).group(1)
    assert "ENROLL_KEY" not in body


# --------------------------------------------------------------------------- #
# Uninstall must not silently orphan a machine record
# --------------------------------------------------------------------------- #


def test_uninstall_keeps_the_machine_identity():
    """Removing the state directory turns a reinstall into a brand-new machine
    and strands this Mac's assignments on a record nobody looks at again."""
    text = INSTALLER.read_text()
    uninstall = text[text.index('if [ "$UNINSTALL" = "true" ]'):text.index("[ -n \"$SERVER\" ]")]
    assert 'rm -rf "$INSTALL_DIR"' in uninstall
    assert 'rm -rf "$STATE_DIR"' not in uninstall
    assert "$STATE_DIR" in uninstall, "the uninstaller should say where identity lives"


# --------------------------------------------------------------------------- #
# The scripts themselves
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("script", [INSTALLER, TESTBED])
def test_shell_scripts_parse(script):
    proc = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("script", [INSTALLER, TESTBED, CHECKER])
def test_scripts_are_executable(script):
    assert script.stat().st_mode & 0o111, f"{script.name} is not executable"


def test_the_checker_only_ever_removes_its_own_queues():
    """It runs against a real scheduler, which on a Mac is somebody's actual
    printers. A sweep that is not prefix-scoped deletes them."""
    text = CHECKER.read_text()
    assert 'PREFIX = "PNCHK_"' in text
    sweep = text[text.index("def sweep("):text.index("def check_enumeration(")]
    assert "startswith(PREFIX)" in sweep


def test_the_checker_is_honest_about_needing_real_hardware():
    """A green run without --printer-uri proves nothing about driverless
    printing, and must not read as though it does."""
    text = CHECKER.read_text()
    assert "--printer-uri" in text
    assert "so nothing here proves a queue " in text
    assert "before claiming driverless" in text
    # And the skip has to be a skip, not a silent pass.
    assert 'c.skip("a real device becomes a working queue"' in text


def test_the_installer_refuses_to_run_on_non_macos():
    assert 'uname -s' in INSTALLER.read_text()


def test_the_installer_still_guards_the_placeholder_pip_source():
    assert "your-org" in INSTALLER.read_text()
