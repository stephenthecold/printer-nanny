"""The pure helpers in scripts/windows_probe_report.py.

That script is hand-run on an operator's own machine against their own network,
so the parts that decide *what to touch* deserve tests even though the script as
a whole can only be exercised on Windows with printers present.

The property that actually matters here is the host cap: a machine sitting on a
/16 must not have 65k SNMP probes fired across it because someone ran the
discovery helper. That looks like a port scan to whatever is watching the
network, and it is exactly the kind of thing a colleague would not forgive.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "windows_probe_report.py"


def _load():
    spec = importlib.util.spec_from_file_location("windows_probe_report", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wpr = _load()


# --- the host cap -----------------------------------------------------------


def test_a_subnet_larger_than_the_cap_is_refused():
    """A /16 is 65k hosts. Sweeping it unasked is not acceptable behaviour."""
    assert wpr.parse_cidrs(["10.0.0.0/16"], max_hosts=1024) == []


def test_a_subnet_within_the_cap_is_accepted():
    assert wpr.parse_cidrs(["192.168.1.0/24"], max_hosts=1024) == ["192.168.1.0/24"]


def test_the_cap_is_a_parameter_not_a_constant():
    """An operator who knows their network can raise it deliberately."""
    assert wpr.parse_cidrs(["10.0.0.0/16"], max_hosts=100000) == ["10.0.0.0/16"]


def test_a_host_address_is_normalised_to_its_network():
    """Get-NetIPAddress hands back 192.168.1.42/24, not the network address."""
    assert wpr.parse_cidrs(["192.168.1.42/24"], max_hosts=1024) == ["192.168.1.0/24"]


@pytest.mark.parametrize(
    "value", ["not-a-cidr", "", "   ", "999.1.1.1/24", "192.168.1.0/33"]
)
def test_malformed_input_is_skipped_not_raised(value):
    """A bad value should be a clear line of output, not a traceback."""
    assert wpr.parse_cidrs([value], max_hosts=1024) == []


@pytest.mark.parametrize("value", ["127.0.0.0/8", "169.254.0.0/16", "::1/128"])
def test_loopback_link_local_and_ipv6_are_skipped(value):
    assert wpr.parse_cidrs([value], max_hosts=1024) == []


# --- merging two discovery sources -----------------------------------------


def test_one_device_found_twice_is_reported_once():
    """SNMP and mDNS both see the same printer; the report must not double it."""
    merged = wpr.merge_devices(
        [{"ip": "10.0.0.5", "brand": "Brother", "model": "HL-L2350DW"}],
        [{"ip": "10.0.0.5", "hostname": "Front-Desk.local"}],
    )
    assert list(merged) == ["10.0.0.5"]


def test_the_two_sources_fill_in_each_others_gaps():
    """SNMP usually knows the model; mDNS usually knows the friendly name.

    Replacing the record with whichever arrived last would throw away half of
    what was learned, so the merge fills empty fields instead.
    """
    merged = wpr.merge_devices(
        [{"ip": "10.0.0.5", "brand": "Brother", "model": "HL-L2350DW", "hostname": None}],
        [{"ip": "10.0.0.5", "hostname": "Front-Desk.local", "model": None}],
    )
    dev = merged["10.0.0.5"]
    assert dev["model"] == "HL-L2350DW"
    assert dev["hostname"] == "Front-Desk.local"


def test_an_earlier_source_is_not_overwritten_by_a_later_one():
    merged = wpr.merge_devices(
        [{"ip": "10.0.0.5", "model": "real model"}],
        [{"ip": "10.0.0.5", "model": "worse guess"}],
    )
    assert merged["10.0.0.5"]["model"] == "real model"


def test_a_device_with_no_address_is_dropped():
    """Nothing can be probed without an address, and a blank key would collide."""
    assert wpr.merge_devices([{"hostname": "ghost.local"}]) == {}


# --- presentation -----------------------------------------------------------


def test_describe_prefers_the_most_identifying_field():
    assert wpr.describe({"model": "HL-L2350DW", "hostname": "x", "brand": "Brother"}) == "HL-L2350DW"
    assert wpr.describe({"hostname": "Front-Desk.local", "brand": "Brother"}) == "Front-Desk.local"
    assert wpr.describe({"brand": "Brother"}) == "Brother"
    assert wpr.describe({"ip": "10.0.0.5"}) == "(unidentified)"


def test_addresses_sort_numerically():
    """Lexical sort puts .10 before .9, which is painful to read against DHCP."""
    ips = ["192.168.1.10", "192.168.1.9", "192.168.1.100", "192.168.1.2"]
    assert sorted(ips, key=wpr.sort_key) == [
        "192.168.1.2",
        "192.168.1.9",
        "192.168.1.10",
        "192.168.1.100",
    ]


def test_non_addresses_sort_after_addresses_without_raising():
    ips = ["printer.local", "192.168.1.9"]
    assert sorted(ips, key=wpr.sort_key) == ["192.168.1.9", "printer.local"]


# --- a subnet passed where an address was expected --------------------------


def test_a_subnet_passed_to_ip_is_rerouted_to_the_sweep():
    """`-PrinterIp 10.0.3.0/24` is an easy slip with a misleading failure.

    Without this the literal string is probed as a hostname, cannot resolve,
    burns the full timeout, and comes back `unreachable` -- which asserts that a
    device is present and silent. That is a different diagnosis from "you gave
    me a subnet", and it sends the reader off checking firewalls and cabling.
    Happened on the first real attempt.
    """
    direct, misrouted = wpr.split_addresses(["10.0.3.0/24"])
    assert direct == []
    assert misrouted == ["10.0.3.0/24"]
    # ...and it survives the cap check, so the sweep actually happens.
    assert wpr.parse_cidrs(misrouted, max_hosts=1024) == ["10.0.3.0/24"]


def test_addresses_and_hostnames_pass_through_untouched():
    """The probe accepts a hostname, so only a prefix marks a subnet."""
    direct, misrouted = wpr.split_addresses(["192.168.1.50", "printer.local"])
    assert direct == ["192.168.1.50", "printer.local"]
    assert misrouted == []


def test_a_mixed_list_is_split_rather_than_rejected():
    direct, misrouted = wpr.split_addresses(["192.168.1.50", "10.0.3.0/24"])
    assert direct == ["192.168.1.50"]
    assert misrouted == ["10.0.3.0/24"]


def test_blank_entries_are_dropped():
    """An empty -PrinterIp element must not become a probe of the empty string."""
    assert wpr.split_addresses(["", "   ", None]) == ([], [])


# --- hypervisor NAT detection -----------------------------------------------


@pytest.mark.parametrize(
    "cidr", ["10.0.2.0/24", "10.0.2.15/24", "192.168.56.0/24", "192.168.56.101/24"]
)
def test_hypervisor_nat_ranges_are_recognised(cidr):
    """A NAT'd VM cannot see the LAN, so an empty sweep needs explaining.

    This came from a real run: the sweep found nothing on 10.0.2.0/24 and the
    output invited the reader to suspect a segmented VLAN or a wrong SNMP
    community. Both were wrong -- that range is VirtualBox/QEMU NAT, where no
    tool can reach a physical printer. Without naming it, the next person spends
    an hour debugging the probe instead of the network.
    """
    assert wpr.is_virtual_nat(cidr)


@pytest.mark.parametrize("cidr", ["192.168.1.0/24", "10.0.0.0/24", "172.16.4.0/24"])
def test_ordinary_lans_are_not_flagged_as_virtual(cidr):
    """Crying "this is a VM" at a real office LAN would be worse than silence."""
    assert not wpr.is_virtual_nat(cidr)


@pytest.mark.parametrize("cidr", ["::1/128", "garbage", ""])
def test_virtual_nat_check_never_raises(cidr):
    """It runs on the no-devices-found path, where an exception would replace
    a useful message with a traceback."""
    assert wpr.is_virtual_nat(cidr) is False


# --- the probe never raises -------------------------------------------------


def test_probe_one_turns_a_raising_probe_into_a_result(monkeypatch):
    """A sweep must survive one hostile device; an exception would end the run."""
    from printer_nanny_agent import ipp

    def boom(*a, **kw):
        raise RuntimeError("device did something rude")

    monkeypatch.setattr(ipp, "probe", boom)
    result = wpr.probe_one("10.0.0.5", timeout=0.1)
    assert result["status"] == "error"
    assert "rude" in result["reason"]
