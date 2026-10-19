"""Cisco connector, re-exported from the dedicated Cisco adapter package.

The Cisco adapter lives in :mod:`app.cisco` because it predates the vendor
registry and has its own device/client classes. Exposing it here lets
:mod:`app.connectors` treat every vendor uniformly.
"""

from __future__ import annotations

from app.cisco.adapter import CiscoConnector

__all__ = ["CiscoConnector"]