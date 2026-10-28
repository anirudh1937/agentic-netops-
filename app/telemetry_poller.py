"""Controlled periodic telemetry poller for Agentic NetOps (Milestone 2).

This module provides a small, dependency-free async poller that drives the
existing vendor connectors through :func:`app.telemetry_store.TelemetryStore`
without duplicating device-access logic.

Polling is **disabled by default**: constructing a :class:`TelemetryPoller`
with ``enabled=False`` (the default) never starts a background loop, and
importing this module has no side effects. The poller must be explicitly
started (and configured with ``enabled=True``) before any polling occurs.

Design notes
------------
* Identity: each reading is keyed by ``(vendor, device_id, interface)`` so
  telemetry from different devices never overwrites one another. The current
  configuration supports one connector per vendor; multiple devices for the
  same vendor are not supported in this milestone.
* Failure isolation: a single connector failure is recorded as an error
  reading for that identity and does not prevent other targets from being
  polled.
* No overlapping polls: an ``asyncio.Lock`` serializes poll cycles; a cycle
  that is still running when the next interval elapses simply skips that
  cycle.
* Clean shutdown: :meth:`stop` cancels the background task and waits for it
  to finish. The lock is not replaced, so no poll is left mid-flight.
* Credential safety: connector error messages are logged only through the
  credential-safe :func:`_safe_error_message` helper; arbitrary exception
  text is never logged verbatim.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from app.cisco import CiscoTelemetryError
from app.connectors import (
    SandboxConnectorError,
    UnsupportedVendorError,
    available_vendors,
    make_connector,
)
from app.connectors.base import SandboxConnector
from app.inventory import Vendor
from app.telemetry import NetworkTelemetry
from app.telemetry_store import TelemetryReading, TelemetryStore
from app.diagnosis import diagnose_network_issue

logger = logging.getLogger(__name__)

#: Vendors whose connectors are known to be unconfigured (missing required
#: environment variables). Tracked so configuration problems are reported
#: once rather than on every poll cycle.
_UNCONFIGURED_VENDORS: set[str] = set()


def _safe_error_message(exc: BaseException) -> str:
    """Return a credential-safe summary of an exception for logging.

    Connector errors (``SandboxConnectorError`` and subclasses) are already
    documented to omit credentials and host secrets, so their message is used
    directly. For any other exception type, only the exception class name is
    logged -- arbitrary exception text may contain secrets or connection
    details and must never be written to logs.
    """

    if isinstance(exc, SandboxConnectorError):
        return str(exc)
    return f"{type(exc).__name__}"


def _is_missing_configuration(exc: BaseException) -> bool:
    """Return True when the exception indicates missing device configuration."""

    message = str(exc).lower()
    return any(
        token in message
        for token in (
            "not configured",
            "is not set",
            "host is not configured",
            "credentials are not configured",
        )
    )


class TelemetryPoller:
    """Controlled periodic telemetry poller.

    Parameters
    ----------
    store:
        The :class:`TelemetryStore` to write readings into.
    interval_seconds:
        Polling interval in seconds. Must be positive. Only honored when
        ``enabled`` is ``True``.
    enabled:
        Whether polling is active. Defaults to ``False`` so importing or
        constructing the poller never starts a background loop.
    connectors:
        Optional mapping of vendor string to an already-built
        :class:`SandboxConnector`. When provided, these are used directly
        instead of building connectors via :func:`make_connector`. This is
        primarily a seam for testing.
    """

    def __init__(
        self,
        *,
        store: TelemetryStore,
        interval_seconds: float = 60.0,
        enabled: bool = False,
        connectors: Optional[dict[str, SandboxConnector]] = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._store = store
        self._interval_seconds = float(interval_seconds)
        self._enabled = bool(enabled)
        self._connectors: dict[str, SandboxConnector] = dict(connectors or {})

        self._task: Optional[asyncio.Task[None]] = None
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        # Last-known device identity per vendor, observed from a successful
        # poll. Used on failure so the error reading is stored under the same
        # composite identity as the last success rather than fabricating a
        # device_id.
        self._last_identity: dict[str, tuple[str, str]] = {}

    # ------------------------------------------------------------------ #
    # Factory: build from environment configuration
    # ------------------------------------------------------------------ #
    @classmethod
    def from_config(
        cls,
        store: TelemetryStore,
        *,
        interval_seconds: float = 60.0,
        enabled: bool = False,
        environ: Optional[dict[str, str]] = None,
        vendors: Optional[list[str]] = None,
    ) -> "TelemetryPoller":
        """Build a poller with connectors constructed from environment config.

        Uses :func:`app.connectors.make_connector` and
        :func:`app.connectors.available_vendors` so the poller is driven by
        the same environment variables (``CISCO_SANDBOX_HOST`` etc.) as the
        rest of the application.

        Configuration behavior:
        * Vendor *discovery* is registry-based: :func:`available_vendors`
          returns the vendors registered in the connector registry (cisco,
          juniper, arista). It does not inspect ``environ``.
        * Connector *construction* is environment-based: each discovered
          vendor's connector is built via :func:`make_connector`, which reads
          that vendor's credentials from the supplied ``environ`` mapping
          (defaulting to ``os.environ``).
        * Vendors whose required configuration is missing are skipped and
          reported rather than silently pretended available. No network call
          is made during construction.

        Parameters
        ----------
        store:
            The :class:`TelemetryStore` to write readings into.
        interval_seconds:
            Polling interval in seconds.
        enabled:
            Whether polling starts enabled.
        environ:
            Environment mapping used to construct connectors. Defaults to
            ``os.environ``. It is passed to each connector's ``from_env``;
          it does not affect which vendors are discovered.
        vendors:
            Optional explicit list of vendor identifiers to configure. When
            omitted, :func:`available_vendors` is used.
        """

        import os

        if environ is None:
            environ = dict(os.environ)

        poller = cls(
            store=store,
            interval_seconds=interval_seconds,
            enabled=enabled,
        )

        target_vendors = vendors if vendors is not None else available_vendors()
        for vendor_id in target_vendors:
            try:
                vendor = Vendor(vendor_id)
            except ValueError:
                logger.warning(
                    "Skipping unsupported vendor '%s': not a registered vendor.",
                    vendor_id,
                )
                continue
            try:
                connector = make_connector(vendor, environ=environ)
            except (
                SandboxConnectorError,
                UnsupportedVendorError,
                CiscoTelemetryError,
            ) as exc:
                # Missing configuration or unsupported vendor: report and skip
                # rather than pretending the target is available. The Cisco
                # adapter raises CiscoTelemetryError (not SandboxConnectorError)
                # when its host is unconfigured, so it must be caught explicitly
                # here without hiding unrelated programming errors.
                _UNCONFIGURED_VENDORS.add(vendor_id)
                logger.warning(
                    "Skipping vendor '%s': %s",
                    vendor_id,
                    _safe_error_message(exc),
                )
                continue
            poller._connectors[vendor_id] = connector

        return poller

    @property
    def unconfigured_vendors(self) -> set[str]:
        """Vendor identifiers that were skipped due to missing configuration."""

        return set(_UNCONFIGURED_VENDORS)

    # ------------------------------------------------------------------ #
    # Configuration introspection
    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Start the background polling loop.

        No-op when polling is disabled or already running. Safe to call
        multiple times.
        """

        if not self._enabled:
            logger.debug("Telemetry poller is disabled; not starting.")
            return
        if self.is_running:
            logger.debug("Telemetry poller is already running.")
            return

        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "Telemetry poller started (interval=%.fs, enabled=%s, targets=%d)",
            self._interval_seconds,
            self._enabled,
            len(self._connectors),
        )

    async def stop(self) -> None:
        """Stop the background polling loop and wait for clean shutdown.

        Cancels the running task, awaits its cancellation. Safe to call when
        not running or after a previous stop.
        """

        if not self.is_running:
            return

        assert self._task is not None
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("Telemetry poller stopped.")

    # ------------------------------------------------------------------ #
    # Single poll operation (testable independently)
    # ------------------------------------------------------------------ #
    async def poll_once(self) -> None:
        """Poll every configured target exactly once.

        Iterates over the configured connectors, fetches telemetry, runs the
        deterministic diagnosis engine, and stores the result. Each target is
        isolated: a failure for one target is recorded as an error reading
        and does not prevent subsequent targets from being polled.

        Requires at least one configured connector; otherwise returns
        immediately without writing anything.
        """

        if not self._connectors:
            logger.debug("No connectors configured; poll_once is a no-op.")
            return

        async with self._lock:
            await self._poll_all()

    # ------------------------------------------------------------------ #
    # Internal loop
    # ------------------------------------------------------------------ #
    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._interval_seconds
                )
            except asyncio.TimeoutError:
                # Interval elapsed: attempt a poll cycle.
                await self.poll_once()

    async def _poll_all(self) -> None:
        for vendor, connector in self._connectors.items():
            await self._poll_vendor(vendor, connector)

    async def _poll_vendor(
        self, vendor: str, connector: SandboxConnector
    ) -> None:
        # Resolve a stable identity for this target. The connector exposes
        # ``host`` (public management address only, never a credential) and
        # ``vendor``. The actual device_id/interface are only known after a
        # successful fetch_telemetry(), so on failure we cannot invent them.
        #
        # Requirement 9: do not fabricate device identifiers. If we have
        # observed a previous successful poll for this vendor, reuse its
        # device_id/interface so the error reading shares the same composite
        # identity as the last success. If we have never succeeded for this
        # vendor, there is no verified identity to record against -- do not
        # create a store record with a fabricated device ID. The failure is
        # still logged (sanitized) so operators can see the attempt failed.
        host = getattr(connector, "host", None) or "unknown"
        known = self._last_identity.get(vendor)
        if known is not None:
            device_id, interface = known
        else:
            device_id = None
            interface = None

        attempt_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        try:
            telemetry = connector.fetch_telemetry()
        except SandboxConnectorError as exc:
            # Credential-safe error already; log only the safe message.
            logger.warning(
                "Poll failed for vendor=%s host=%s: %s",
                vendor,
                host,
                _safe_error_message(exc),
            )
            # Only record a failure reading when we have a verified identity
            # to attach it to. A first-poll failure has no real device_id/
            # interface, so we intentionally do not fabricate a store record.
            if device_id is not None:
                self._record_failure(
                    vendor=vendor,
                    device_id=device_id,
                    interface=interface,
                    attempt_at=attempt_at,
                    error=_safe_error_message(exc),
                )
            return
        except Exception as exc:  # noqa: BLE001 - isolate per-vendor failures
            # Never log arbitrary exception text (may contain secrets).
            logger.warning(
                "Poll failed unexpectedly for vendor=%s host=%s: %s",
                vendor,
                host,
                _safe_error_message(exc),
            )
            if device_id is not None:
                self._record_failure(
                    vendor=vendor,
                    device_id=device_id,
                    interface=interface,
                    attempt_at=attempt_at,
                    error=f"{type(exc).__name__}",
                )
            return

        # Successful fetch: compute server-side collection time and diagnosis.
        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        diagnosis = diagnose_network_issue(telemetry)

        self._store.put(
            vendor=vendor,
            device_id=telemetry.device_id,
            interface=telemetry.interface,
            timestamp=telemetry.timestamp,
            fetched_at=fetched_at,
            source=vendor,
            telemetry=telemetry.model_dump(),
            diagnosis=diagnosis,
            error=None,
        )
        # Remember the identity so a later failure for this vendor can reuse it.
        self._last_identity[vendor] = (telemetry.device_id, telemetry.interface)
        logger.info(
            "Polled vendor=%s device=%s interface=%s severity=%s",
            vendor,
            telemetry.device_id,
            telemetry.interface,
            diagnosis.get("severity"),
        )

    def _record_failure(
        self,
        *,
        vendor: str,
        device_id: str,
        interface: str,
        attempt_at: str,
        error: str,
    ) -> None:
        """Record a failed poll attempt without clobbering last-success data."""

        self._store.put(
            vendor=vendor,
            device_id=device_id,
            interface=interface,
            timestamp=attempt_at,
            fetched_at=attempt_at,
            source=vendor,
            telemetry={},
            diagnosis={},
            error=error,
        )

    # ------------------------------------------------------------------ #
    # Connector registration (primarily a testing seam)
    # ------------------------------------------------------------------ #
    def add_connector(self, vendor: str, connector: SandboxConnector) -> None:
        """Register a connector for ``vendor``. Used mainly in tests."""

        self._connectors[vendor] = connector

    def remove_connector(self, vendor: str) -> None:
        """Remove the connector for ``vendor``."""

        self._connectors.pop(vendor, None)

    @property
    def connectors(self) -> dict[str, SandboxConnector]:
        return dict(self._connectors)
