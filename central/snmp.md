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

## Vendor-specific provider notes (post-Milestone-2)

Brand-agnostic Printer-MIB doesn't capture everything. Per the project design
(see `CLAUDE.md` upload), a `PrinterProvider` plugin layer at
`agent/printer_nanny_agent/providers/` runs after the standard reading is
built and can enrich it from vendor-private OIDs.

### Brother

Brother laser MFCs (MFC-/HL-/DCP- series) do not have continuous toner fill
sensors. The cartridge firmware only tracks OK / Low / Empty buckets, so
`prtMarkerSuppliesLevel` returns `-3` for every toner. Brother's private MIB
exposes the bucket-state as a plain-text active-alert scalar:

| OID                                              | Example                  |
|--------------------------------------------------|--------------------------|
| `1.3.6.1.4.1.2435.2.3.9.4.2.1.5.4.5.2.0`         | `Toner Low (BK)`         |
| `1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.51.2.1.{1,2,3}`| Recent-alerts table (index, description, page count when raised) |

The Brother provider parses the active alert into a (severity, color) pair
and upgrades the matching toner supply: `level_pct` becomes a UI hint
(`15.0` for low, `0.0` for empty) and `status_note` becomes the bucket name.
Page-count consumables (drum, belt, fuser) keep the real numbers Brother
already reports via the standard Printer-MIB.

For real continuous percentages on Brother lasers (vendor-estimated, not
sensor data) the only source is HTML scraping of the printer's EWS at
`http://<ip>/general/status.html`. The gauge HTML format varies by firmware
and is left as a future EWS provider.
