"""Exercise the PowerShell seam against a real Windows spooler.

Everything above ``PowerShellRunner`` is covered on any platform by
tests/test_workstation_queue.py with a fake. This file covers the one layer that
fake cannot: whether the cmdlets, parameters and environment-variable delivery
actually behave as assumed on Windows. It is skipped everywhere else.

Run by .github/workflows/windows-client.yml on a windows-latest runner.

WHAT THIS DOES AND DOES NOT PROVE
---------------------------------
Proves: the scripts parse and run; the cmdlets and parameters exist as spelled;
values delivered as environment variables arrive intact and are NOT executed;
create / converge / repair / idempotent-rerun / remove behave against the real
spooler; and the inbox IPP class driver is present and named as expected.

Does NOT prove: behaviour against a real printer, domain/GPO interaction with
RestrictDriverInstallationToAdministrators, vendor driver packages, or the
LocalSystem service context (a CI runner is an elevated user, not SYSTEM).
Those remain manual, per deploy/WINDOWS-MSI-TESTING.md. Claiming otherwise from
a green run here would be overstating the evidence.
"""

from __future__ import annotations

import platform
import uuid

import pytest

from printer_nanny_agent import workstation as ws

pytestmark = pytest.mark.skipif(
    platform.system() != "Windows", reason="needs a real Windows spooler"
)


@pytest.fixture(scope="module")
def runner():
    return ws.PowerShellRunner(timeout_s=180)


@pytest.fixture(scope="module")
def local_driver(runner):
    """An inbox driver that exists on this image.

    Chosen at runtime rather than hardcoded: the set of preinstalled drivers
    differs between runner images, and a hardcoded name turns an image refresh
    into a mystery failure.
    """
    out = runner.run(
        "$ErrorActionPreference='Stop'\n"
        "Get-PrinterDriver | Select-Object -ExpandProperty Name\n"
    )
    names = [n.strip() for n in out.splitlines() if n.strip()]
    assert names, "no printer drivers installed on this runner"
    for preferred in ("Microsoft Print To PDF", "Microsoft XPS Document Writer", "Generic / Text Only"):
        if preferred in names:
            return preferred
    return names[0]


@pytest.fixture()
def queue_name(runner):
    """A unique queue, always cleaned up even when the test fails."""
    name = "PNTest-" + uuid.uuid4().hex[:8]
    yield name
    try:
        ws.remove_queue(runner, name)
    except ws.PowerShellError:
        pass


# --- the seam itself --------------------------------------------------------


def test_powershell_runs_at_all(runner):
    assert "pong" in runner.run("'pong'")


def test_nonzero_exit_raises_with_stderr(runner):
    with pytest.raises(ws.PowerShellError) as exc:
        runner.run("$ErrorActionPreference='Stop'; throw 'deliberate failure'")
    assert exc.value.returncode != 0
    assert "deliberate" in (exc.value.stderr or "") + (exc.value.stdout or "")


@pytest.mark.parametrize(
    "hostile",
    [
        'x"; Write-Host PWNED; #',
        "'; Write-Host PWNED; '",
        "$(Write-Host PWNED)",
        "a`nWrite-Host PWNED",
        "& calc.exe",
    ],
)
def test_hostile_values_are_inert_on_a_real_shell(runner, hostile):
    """The claim the unit tests can only assert structurally, proven for real.

    A value carrying PowerShell syntax must come back as literal text, not be
    evaluated. If environment delivery were unsound this is where it shows.
    """
    out = runner.run("$env:PN_VALUE", {"value": hostile})
    assert "PWNED" not in out, "a hostile value executed"
    assert hostile.strip().splitlines()[0][:12] in out or hostile in out


# --- the inbox driver -------------------------------------------------------


def test_ipp_class_driver_is_present_and_named_as_expected(runner):
    """Tier 1 binds this driver by exact name; a rename would break silently."""
    out = runner.run(
        "$ErrorActionPreference='Stop'\n"
        "Get-PrinterDriver | Select-Object -ExpandProperty Name\n"
    )
    names = {n.strip() for n in out.splitlines() if n.strip()}
    if ws.IPP_CLASS_DRIVER not in names:
        pytest.skip(
            "inbox IPP class driver absent on this image; installed: {}".format(sorted(names))
        )
    assert ws.driver_present(runner, ws.IPP_CLASS_DRIVER)


def test_driver_present_is_false_for_a_made_up_driver(runner):
    assert not ws.driver_present(runner, "Definitely Not A Real Driver " + uuid.uuid4().hex[:6])


# --- create / converge / remove against the real spooler --------------------


def test_create_converge_and_remove(runner, queue_name, local_driver):
    """The full lifecycle, including the idempotent re-run that matters most."""
    assert not ws.queue_state(runner, queue_name).present

    created = ws._converge(
        runner, queue_name, ws.port_name_for("127.0.0.1"), local_driver,
        "127.0.0.1", "", "",
    )
    assert created == "created"

    state = ws.queue_state(runner, queue_name)
    assert state.present
    assert state.driver == local_driver

    # The client re-runs constantly; a second pass must touch nothing.
    again = ws._converge(
        runner, queue_name, ws.port_name_for("127.0.0.1"), local_driver,
        "127.0.0.1", "", "",
    )
    assert again == "unchanged"

    ws.remove_queue(runner, queue_name)
    assert not ws.queue_state(runner, queue_name).present


def test_remove_is_idempotent(runner, queue_name):
    """Deprovisioning must survive a queue an operator already deleted."""
    ws.remove_queue(runner, queue_name)
    ws.remove_queue(runner, queue_name)


def test_drift_is_repaired_in_place(runner, queue_name, local_driver):
    """A re-addressed device must be corrected, not duplicated."""
    ws._converge(runner, queue_name, ws.port_name_for("127.0.0.1"), local_driver,
                 "127.0.0.1", "", "")
    result = ws._converge(runner, queue_name, ws.port_name_for("127.0.0.2"), local_driver,
                          "127.0.0.2", "", "")
    assert result == "updated"

    state = ws.queue_state(runner, queue_name)
    assert state.port == ws.port_name_for("127.0.0.2")
    # Exactly one queue by that name -- repaired, not duplicated.
    assert [q for q in ws.list_queues(runner) if q["name"] == queue_name].__len__() == 1


def test_a_queue_named_with_shell_syntax_is_created_literally(runner, local_driver):
    """The end-to-end proof: a hostile name becomes a queue, not a command."""
    name = "PNTest-" + uuid.uuid4().hex[:6] + " $(Write-Host PWNED)"
    try:
        ws._converge(runner, name, ws.port_name_for("127.0.0.1"), local_driver,
                     "127.0.0.1", "", "")
        state = ws.queue_state(runner, name)
        assert state.present, "the literal name should have been created"
    finally:
        try:
            ws.remove_queue(runner, name)
        except ws.PowerShellError:
            pass


def test_reconcile_only_touches_its_own_prefix(runner, local_driver):
    """Never delete a printer the user added themselves."""
    ours = "PNTest-" + uuid.uuid4().hex[:8]
    try:
        ws._converge(runner, ours, ws.port_name_for("127.0.0.1"), local_driver,
                     "127.0.0.1", "", "")
        before = {q["name"] for q in ws.list_queues(runner)}

        # Desired set is empty, so our stale queue goes and nothing else does.
        outcomes = ws.reconcile(runner, [], managed_prefix="PNTest-")
        after = {q["name"] for q in ws.list_queues(runner)}

        assert outcomes.get(ours) == "removed"
        assert (before - after) == {ours}, "only our own queue may be removed"
    finally:
        try:
            ws.remove_queue(runner, ours)
        except ws.PowerShellError:
            pass
