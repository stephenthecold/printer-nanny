"""printer-nanny-agent — site collector for Printer Nanny.

Discovers and polls printers over SNMP (RFC 3805 Printer MIB), then pushes
readings to the central server and pulls queued commands. See the package README
and ``central/snmp.md`` for the OID reference.
"""

__version__ = "0.1.0"
