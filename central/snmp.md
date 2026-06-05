# SNMP OID reference (Printer MIB / Host Resources MIB)

The agent (Milestone 2) collects brand-agnostically using the standard **Printer
MIB (RFC 3805)** and **Host Resources MIB (RFC 2790)**. Nearly all networked
printers (HP, Brother, Canon, Xerox, Lexmark, Konica, Ricoh, …) implement these.

## Identity
| Field          | OID                          | Name                        |
|----------------|------------------------------|-----------------------------|
| System name    | `1.3.6.1.2.1.1.5.0`          | `sysName`                   |
| System descr   | `1.3.6.1.2.1.1.1.0`          | `sysDescr`                  |
| Printer name   | `1.3.6.1.2.1.43.5.1.1.16.1`  | `prtGeneralPrinterName`     |
| Serial number  | `1.3.6.1.2.1.43.5.1.1.17.1`  | `prtGeneralSerialNumber`    |
| Model / device | `1.3.6.1.2.1.25.3.2.1.3`     | `hrDeviceDescr`             |

## Supplies (toner / ink / drum / fuser / waste)  — walk the `prtMarkerSupplies` table
| Field            | OID base                     | Name                            |
|------------------|------------------------------|---------------------------------|
| Description      | `1.3.6.1.2.1.43.11.1.1.6`    | `prtMarkerSuppliesDescription`  |
| Type code        | `1.3.6.1.2.1.43.11.1.1.5`    | `prtMarkerSuppliesType`         |
| Max capacity     | `1.3.6.1.2.1.43.11.1.1.8`    | `prtMarkerSuppliesMaxCapacity`  |
| Current level    | `1.3.6.1.2.1.43.11.1.1.9`    | `prtMarkerSuppliesLevel`        |
| Colorant value   | `1.3.6.1.2.1.43.12.1.1.4`    | `prtMarkerColorantValue`        |

**Sentinel handling** (see `central/snmp_parse.py`):
- Level `-1` other · `-2` unknown · `-3` some remaining (don't store as a number).
- Max capacity `-1` unlimited · `-2` unknown → treat reported level as a percent.

## Page count
| Field            | OID                          | Name                  |
|------------------|------------------------------|-----------------------|
| Lifetime pages   | `1.3.6.1.2.1.43.10.2.1.4.1.1`| `prtMarkerLifeCount`  |

## Status & errors
| Field                  | OID                            | Name                          |
|------------------------|--------------------------------|-------------------------------|
| Printer status         | `1.3.6.1.2.1.25.3.5.1.1`       | `hrPrinterStatus`             |
| Detected error state   | `1.3.6.1.2.1.25.3.5.1.2`       | `hrPrinterDetectedErrorState` |
| Alert table            | `1.3.6.1.2.1.43.18.1.1`        | `prtAlertTable` (code/sev/desc)|

`hrPrinterStatus`: 1 other · 2 unknown · 3 idle · 4 printing · 5 warmup.
`hrPrinterDetectedErrorState` is a bit field (low paper, no paper, low toner,
no toner, door open, jammed, offline, service requested, …).

## Discovery
SNMP GET `sysDescr` (or `hrDeviceDescr`) across the subnet's host range. Anything
that answers and exposes `prtGeneralPrinterName` / the marker-supplies table is a
printer → recorded as `discovery_state = pending` for a tech to approve.
