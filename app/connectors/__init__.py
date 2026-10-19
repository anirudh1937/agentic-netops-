"""Vendor registry for the multi-vendor sandbox connector abstraction.

Each vendor connector (:class:`CiscoConnector`, :class:`JuniperConnector`,
:class:`AristaConnector`) implements the :class:`~app.connectors.base.SandboxConnector`
interface. The registry maps a :class:`~app.inventory.Vendor` to its connector
class so routes can resolve a vendor at request time without hardcoding the
vendor name.

Connector modules are imported lazily (inside :func:`_registry`) so importing
this package does not eagerly load :mod:`app.cisco` while it is still being
defined -- that would deadlock the import machinery.
"""

from __future__ import annotations

from app.connectors.base import (
    SandboxConnector,
    SandboxConnectorError,
    UnsupportedVendorError,
)
from app.inventory import Vendor

__all__ = [
    "SandboxConnector",
    "SandboxConnectorError",
    "UnsupportedVendorError",
    "available_vendors",
    "get_connector_class",
    "make_connector",
]


def _registry() -> dict[Vendor, type[SandboxConnector]]:
    """Build and cache the vendor -> connector class lookup.

    The connector classes are imported lazily to avoid a circular import with
    :mod:`app.cisco`.
    """

    from app.connectors.arista import AristaConnector
    from app.connectors.cisco import CiscoConnector
    from app.connectors.juniper import JuniperConnector

    return {
        Vendor.CISCO: CiscoConnector,
        Vendor.JUNIPER: JuniperConnector,
        Vendor.ARISTA: AristaConnector,
    }


def get_connector_class(vendor: Vendor) -> type[SandboxConnector]:
    """Return the connector class registered for ``vendor``.

    ``vendor`` may be a :class:`~app.inventory.Vendor` value or the raw string,
    both of which normalize to the enum member. Raises
    :class:`UnsupportedVendorError` for a vendor we do not connect to.
    """

    if not isinstance(vendor, Vendor):
        try:
            vendor = Vendor(vendor)
        except ValueError:
            raise UnsupportedVendorError(
                f"Unsupported vendor '{vendor}'."
            ) from None

    connectors = _registry()
    connector = connectors.get(vendor)
    if connector is None:
        raise UnsupportedVendorError(
            f"No connector registered for vendor '{vendor.value}'."
        )
    return connector


def make_connector(vendor: Vendor, environ: dict[str, str] | None = None) -> SandboxConnector:
    """Instantiate the connector for ``vendor`` from environment configuration."""

    return get_connector_class(vendor).from_env(environ)


def available_vendors() -> list[str]:
    """Return the sorted list of registered vendor identifiers."""

    return sorted(vendor.value for vendor in _registry())