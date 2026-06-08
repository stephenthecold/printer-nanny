"""SNMP OID constants (RFC 3805 Printer MIB + Host Resources MIB).

Mirrors central/snmp.md. Scalar OIDs include the trailing instance index where
fixed; table base OIDs are walked. Table rows on real printers can be indexed
by anything (often 1, but also 2/5/10/65535 depending on hrDeviceIndex), so
discovery walks the table bases rather than GETting a .1-instance.
"""

from __future__ import annotations

# --- Identity (scalars) ---
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
PRT_GENERAL_PRINTER_NAME = "1.3.6.1.2.1.43.5.1.1.16.1"
PRT_GENERAL_SERIAL_NUMBER = "1.3.6.1.2.1.43.5.1.1.17.1"
HR_DEVICE_DESCR = "1.3.6.1.2.1.25.3.2.1.3.1"

# --- Identity (table bases — walked during discovery) ---
PRT_GENERAL_PRINTER_NAME_BASE = "1.3.6.1.2.1.43.5.1.1.16"
PRT_GENERAL_SERIAL_NUMBER_BASE = "1.3.6.1.2.1.43.5.1.1.17"
HR_DEVICE_DESCR_BASE = "1.3.6.1.2.1.25.3.2.1.3"
HR_DEVICE_TYPE_BASE = "1.3.6.1.2.1.25.3.2.1.2"
# Well-known hrDeviceType object identity that marks a row as "this is a printer".
HR_DEVICE_TYPE_PRINTER = "1.3.6.1.2.1.25.3.1.5"

# --- Page count (scalar) ---
PRT_MARKER_LIFE_COUNT = "1.3.6.1.2.1.43.10.2.1.4.1.1"

# --- Status / errors (scalars) ---
HR_PRINTER_STATUS = "1.3.6.1.2.1.25.3.5.1.1.1"
HR_PRINTER_DETECTED_ERROR_STATE = "1.3.6.1.2.1.25.3.5.1.2.1"

# --- Supplies table (walk these bases) ---
PRT_MARKER_SUPPLIES_DESCRIPTION = "1.3.6.1.2.1.43.11.1.1.6"
PRT_MARKER_SUPPLIES_TYPE = "1.3.6.1.2.1.43.11.1.1.5"
PRT_MARKER_SUPPLIES_MAX_CAPACITY = "1.3.6.1.2.1.43.11.1.1.8"
PRT_MARKER_SUPPLIES_LEVEL = "1.3.6.1.2.1.43.11.1.1.9"
PRT_MARKER_COLORANT_VALUE = "1.3.6.1.2.1.43.12.1.1.4"

# --- Alert table (walk) ---
PRT_ALERT_SEVERITY_LEVEL = "1.3.6.1.2.1.43.18.1.1.2"
PRT_ALERT_DESCRIPTION = "1.3.6.1.2.1.43.18.1.1.8"

# OIDs probed during discovery: a device answering sysDescr AND exposing the
# printer-name / supplies table is treated as a printer.
DISCOVERY_PROBE = SYS_DESCR
PRINTER_FINGERPRINT = PRT_GENERAL_PRINTER_NAME

# hrPrinterDetectedErrorState is a bit string; bit position → meaning (RFC 1759).
ERROR_STATE_BITS = {
    0: "low paper",
    1: "no paper",
    2: "low toner",
    3: "no toner",
    4: "door open",
    5: "jammed",
    6: "offline",
    7: "service requested",
    8: "input tray missing",
    9: "output tray missing",
    10: "marker supply missing",
    11: "output near full",
    12: "output full",
    13: "input tray empty",
    14: "overdue preventive maintenance",
}

# Bits that should escalate the printer to an error (vs. a warning).
CRITICAL_ERROR_BITS = {1, 3, 5, 7}  # no paper, no toner, jammed, service requested

# Informational bits — recorded but NOT alarmed. The "offline" detected-error bit
# is set by many printers when in power-save/sleep; if we successfully polled the
# device it is reachable, so this must not raise a critical offline alert. (Real
# offline detection is handled centrally via missed heartbeats / stale last_seen.)
INFO_ERROR_BITS = {6}  # offline (power-save)
