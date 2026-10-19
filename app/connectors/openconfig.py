"""Parsing helpers for OpenConfig-style RESTCONF payloads.

Juniper, Arista, and other vendors expose telemetry through the OpenConfig
YANG models. These helpers normalize the nested ``config``/``state`` shapes
into the flat values the rest of the project works with, so per-vendor
connectors stay small and consistent.
"""

from __future__ import annotations

from typing import Optional


def map_status(status_raw) -> Optional[str]:
    """Translate an OpenConfig ``oper-status`` leaf into ``up``/``down``/None.

    An absent or unrecognized status maps to ``None`` (unknown) so callers
    never fabricate a link the device did not report.
    """

    if status_raw is None:
        return None
    upper = str(status_raw).upper()
    if upper in {"UP", "TRUE", "1", "ADMIN_UP", "LOWER_LINK_UP"}:
        return "up"
    if upper in {"DOWN", "ADMIN_DOWN", "NOT PRESENT", "LOWER_LINK_DOWN"}:
        return "down"
    return None


def openconfig_leaf(container, key):
    """Read a leaf from an OpenConfig ``config``/``state`` container."""

    if not isinstance(container, dict):
        return None
    for source in (container.get("config"), container.get("state")):
        if isinstance(source, dict) and key in source:
            return source[key]
    return None


def openconfig_block(data, namespace):
    """Return the payload nested under ``namespace`` or ``None``.

    RESTCONF JSON wraps every resource in its YANG namespace, e.g.
    ``{"openconfig-system:system": {"state": {...}}}``. Callers that address a
    single container must unwrap that namespace before reading leaves.
    """

    if isinstance(data, dict):
        return data.get(namespace)
    return None


def interfaces_from(data, namespace: str) -> list[tuple[str, str]]:
    """Return ``(name, status)`` pairs for interfaces under ``namespace``.

    Entries without a name are skipped so inventory never reports phantom
    links.
    """

    if not isinstance(data, dict):
        return []

    block = data.get(namespace)
    # RESTCONF JSON payloads expose the list directly
    # ({"interface": [...]}); tolerate a wrapped container too.
    if isinstance(block, list):
        interfaces = block
    elif isinstance(block, dict):
        interfaces = block.get("interface")
    else:
        interfaces = None
    if interfaces is None:
        return []
    if not isinstance(interfaces, list):
        interfaces = [interfaces]
    result: list[tuple[str, str]] = []
    for intf in interfaces:
        if not isinstance(intf, dict):
            continue
        name = openconfig_leaf(intf, "name") or intf.get("name")
        if not name:
            continue
        result.append((name, map_status(openconfig_leaf(intf, "oper-status"))))
    return result


def platform_from(data, namespace: str) -> tuple[Optional[str], Optional[str]]:
    """Return ``(vendor, model)`` reported under the platform ``namespace``."""

    block = data.get(namespace, {}) if isinstance(data, dict) else {}
    config = block.get("config") if isinstance(block, dict) else {}
    if not isinstance(config, dict):
        return None, None
    return config.get("vendor"), config.get("model")


def uptime_from(data, namespace: str) -> Optional[int]:
    block = data.get(namespace) if isinstance(data, dict) else None
    value = openconfig_leaf(block, "uptime-seconds")
    try:
        return int(value)
    except (KeyError, TypeError, ValueError):
        return None