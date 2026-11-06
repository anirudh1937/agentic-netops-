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
* Observable status: :meth:`status` returns an immutable snapshot of the
  poller configuration and the last cycle, including a sanitized error.
* Loop resilience: unexpected exceptions from a poll cycle are recorded and
  contained so later cycles still run; ``asyncio.CancelledError`` is not
  suppressed.
* Credential safety: connector error messages are logged only through the
  credential-safe :func:`_safe_error_message` helper; arbitrary exception
  text is never logged verbatim.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import math
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal, Mapping, Optional

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

#: Default deadline for a single vendor's device telemetry fetch, in seconds.
#: Generous enough for the RESTCONF connectors, which issue up to four
#: sequential requests with a 10 second per-operation HTTP timeout.
DEFAULT_VENDOR_TIMEOUT_SECONDS = 45.0

#: Default wall-clock budget for one complete poll cycle, in seconds.
DEFAULT_CYCLE_TIMEOUT_SECONDS = 150.0

#: Default budget for cancelling the poll loop and draining in-flight
#: workers during shutdown, in seconds.
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 5.0

#: Sentinel returned by ``_fetch_tracked`` when the worker thread never
#: started because the reservation was cleared before it could run.  This
#: is NOT an exception: it means the fetch was safely skipped.
_WORKER_NOT_STARTED = object()

#: Environment variables that override the timeout defaults. Only these three
#: settings are added; ``NETOPS_POLL_ENABLED`` and
#: ``NETOPS_STALE_THRESHOLD_SECONDS`` keep their existing behavior.
VENDOR_TIMEOUT_ENV_VAR = "NETOPS_POLL_VENDOR_TIMEOUT_SECONDS"
CYCLE_TIMEOUT_ENV_VAR = "NETOPS_POLL_CYCLE_TIMEOUT_SECONDS"
SHUTDOWN_TIMEOUT_ENV_VAR = "NETOPS_POLL_SHUTDOWN_TIMEOUT_SECONDS"


@dataclass(frozen=True)
class PollerStatus:
    """Immutable snapshot of the poller's configuration and last cycle."""

    enabled: bool
    running: bool
    interval_seconds: float
    configured_vendors: tuple[str, ...]
    unconfigured_vendors: tuple[str, ...]
    last_cycle_started_at: Optional[str]
    last_cycle_finished_at: Optional[str]
    last_cycle_outcome: Optional[Literal["success", "degraded", "error"]]
    last_error: Optional[str]


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


def _now_iso() -> str:
    """Return the current UTC time in the project's wire format."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def _positive_timeout(value: object, *, name: str) -> float:
    """Coerce ``value`` to a positive, finite timeout in seconds.

    Raises :class:`ValueError` naming ``name`` for non-numeric values, NaN,
    positive or negative infinity, zero, and negative numbers. The parameter
    name is included so a misconfiguration identifies the setting that must be
    fixed rather than producing a bare conversion error.
    """

    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name} must be a positive finite number of seconds"
        ) from exc

    if not math.isfinite(seconds):
        raise ValueError(f"{name} must be a finite number of seconds")
    if seconds <= 0:
        raise ValueError(f"{name} must be greater than zero seconds")

    return seconds


def _timeout_from_env(
    environ: Mapping[str, str],
    key: str,
    default: float,
) -> float:
    """Read one timeout override from ``environ``.

    Missing, empty, and whitespace-only values silently return ``default``.
    Invalid values also return ``default`` but log a warning naming ``key``.
    The supplied value is never echoed into the log, matching the project's
    credential-safe logging rule.
    """

    raw = environ.get(key)
    if raw is None:
        return default

    text = str(raw).strip()
    if not text:
        return default

    try:
        return _positive_timeout(text, name=key)
    except ValueError:
        logger.warning(
            "Ignoring invalid %s value; using the default of %.1f seconds.",
            key,
            default,
        )
        return default


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
    vendor_timeout_seconds:
        Deadline for a single vendor's telemetry fetch. Must be a positive,
        finite number of seconds. Defaults to
        :data:`DEFAULT_VENDOR_TIMEOUT_SECONDS`.
    cycle_timeout_seconds:
        Wall-clock budget for one complete poll cycle. Must be a positive,
        finite number of seconds. Defaults to
        :data:`DEFAULT_CYCLE_TIMEOUT_SECONDS`. A value shorter than
        ``vendor_timeout_seconds`` is accepted but logged as a warning,
        because the per-vendor deadline can then never fire first.
    shutdown_timeout_seconds:
        Budget for cancelling the poll loop and draining in-flight workers.
        Must be a positive, finite number of seconds. Defaults to
        :data:`DEFAULT_SHUTDOWN_TIMEOUT_SECONDS`.
    """

    def __init__(
        self,
        *,
        store: TelemetryStore,
        interval_seconds: float = 60.0,
        enabled: bool = False,
        connectors: Optional[dict[str, SandboxConnector]] = None,
        vendor_timeout_seconds: float = DEFAULT_VENDOR_TIMEOUT_SECONDS,
        cycle_timeout_seconds: float = DEFAULT_CYCLE_TIMEOUT_SECONDS,
        shutdown_timeout_seconds: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")

        # Validate before storing anything so a misconfigured poller never
        # exists in a half-initialized state.
        vendor_timeout = _positive_timeout(
            vendor_timeout_seconds, name="vendor_timeout_seconds"
        )
        cycle_timeout = _positive_timeout(
            cycle_timeout_seconds, name="cycle_timeout_seconds"
        )
        shutdown_timeout = _positive_timeout(
            shutdown_timeout_seconds, name="shutdown_timeout_seconds"
        )

        self._store = store
        self._interval_seconds = float(interval_seconds)
        self._enabled = bool(enabled)
        self._connectors: dict[str, SandboxConnector] = dict(connectors or {})
        # Connectors supplied directly (connectors= or add_connector()) are
        # caller-owned; this poller never closes them. Connectors built by
        # from_config() are marked owned below.
        self._owns_connectors: dict[str, bool] = {
            vendor: False for vendor in self._connectors
        }
        # Connector ids that are reserved by a poller coroutine before its
        # worker thread has started or finished.  remove_connector() and
        # stop() must not close a reserved connector; the deferred-close
        # mechanism handles it when the worker finishes.
        self._reserved_connectors: set[int] = set()
        # Connector ids that have already been closed by the poller.  Used to
        # ensure exactly-once close semantics across stop(), _drain_close(),
        # and remove_connector().
        self._closed_connectors: set[int] = set()
        self._vendor_timeout_seconds = vendor_timeout
        self._cycle_timeout_seconds = cycle_timeout
        self._shutdown_timeout_seconds = shutdown_timeout

        # Both values are preserved exactly as configured; the warning only
        # tells the operator that the per-vendor deadline cannot fire before
        # the cycle is truncated.
        if self._cycle_timeout_seconds < self._vendor_timeout_seconds:
            logger.warning(
                "Cycle timeout (%.1fs) is shorter than vendor timeout (%.1fs); "
                "vendor deadlines may never fire before the cycle is truncated.",
                self._cycle_timeout_seconds,
                self._vendor_timeout_seconds,
            )

        self._task: Optional[asyncio.Task[None]] = None
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        # Last-known device identity per vendor, observed from a successful
        # poll. Used on failure so the error reading is stored under the same
        # composite identity as the last success rather than fabricating a
        # device_id.
        self._last_identity: dict[str, tuple[str, str]] = {}

        # Vendors that are currently unavailable: skipped because their
        # required environment configuration was missing, or because their
        # connector could not be recreated after a restart. This is
        # instance-scoped so one poller's configuration failures never leak
        # into another poller's status.
        self._unconfigured_vendors: set[str] = set()

        # Last poll-cycle status. Timestamps and outcomes are only updated
        # after a cycle starts or finishes; the status endpoint reads them
        # without triggering polling.
        self._last_cycle_started_at: Optional[str] = None
        self._last_cycle_finished_at: Optional[str] = None
        self._last_cycle_outcome: Optional[
            Literal["success", "degraded", "error"]
        ] = None
        self._last_error: Optional[str] = None

        # Shutdown tracking. ``_stop_event`` stops the loop from scheduling
        # new cycles; ``_task`` is the loop coroutine itself. Shutdown is
        # bounded by ``_shutdown_timeout_seconds``.
        self._shutdown_in_progress: bool = False
        self._shutdown_completed: bool = False
        # Set when a stop() has finished (completed or abandoned), so
        # concurrent stop() calls can wait on it with a bounded timeout.
        self._shutdown_done: asyncio.Event = asyncio.Event()
        # True when the shutdown deadline elapsed before the poll loop task
        # finished; in-flight vendor workers may still be active.
        self._shutdown_abandoned_workers: bool = False
        # Vendors whose synchronous fetch_telemetry() worker threads are
        # currently active. A per-vendor counter (vendor -> active worker
        # count) is used instead of a plain set so that overlapping workers
        # for the same vendor (possible when a timed-out coroutine abandons
        # its thread while a replacement cycle starts a new one) are tracked
        # correctly: the vendor stays active until the last worker finishes.
        self._active_vendors: dict[str, int] = {}
        # Worker counts keyed by connector instance (``id(connector)``). This
        # lets deferred cleanup wait for the exact connector a worker is
        # using, even after a restart installs a fresh connector for the same
        # vendor.
        self._active_connectors: dict[int, int] = {}
        self._active_vendors_lock = threading.Lock()
        #: Connector ownership. True when this poller created (and therefore
        #: owns) the connector for a vendor and may close it on shutdown.
        #: False for connectors injected via the constructor or add_connector(),
        #: which remain caller-owned and are never closed by this poller.
        #: Owned connectors whose worker threads were still active when
        #: stop() ran. They are closed once the last worker for the connector
        #: finishes; if the drain deadline expires first they are left open
        #: and closed later when the worker completes. Keyed by connector
        #: instance so a restart can install a new connector for the same
        #: vendor without losing track of the old one.
        self._pending_close: dict[int, tuple[str, SandboxConnector]] = {}
        # Factories for connectors this poller built. Used to recreate owned
        # connectors on restart: a stopped poller closes its owned connectors,
        # so restarting must not hand a closed HTTP client back to the loop.
        self._connector_factories: dict[str, Callable[[], SandboxConnector]] = {}
        # Reference to the single deferred-cleanup task (if any). Retaining it
        # prevents duplicate drain tasks and lets the task be cancelled and its
        # outcome observed deterministically.
        self._drain_task: Optional[asyncio.Task[None]] = None
        # Event loop captured at start(), used to wake the loop from a worker
        # thread when a deferred connector's last worker finishes.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

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
            It also supplies the optional timeout overrides
            (``NETOPS_POLL_VENDOR_TIMEOUT_SECONDS``,
            ``NETOPS_POLL_CYCLE_TIMEOUT_SECONDS``,
            ``NETOPS_POLL_SHUTDOWN_TIMEOUT_SECONDS``). Missing or invalid
            values fall back to the documented defaults.
        vendors:
            Optional explicit list of vendor identifiers to configure. When
            omitted, :func:`available_vendors` is used.
        """

        import os

        if environ is None:
            environ = dict(os.environ)

        vendor_timeout = _timeout_from_env(
            environ, VENDOR_TIMEOUT_ENV_VAR, DEFAULT_VENDOR_TIMEOUT_SECONDS
        )
        cycle_timeout = _timeout_from_env(
            environ, CYCLE_TIMEOUT_ENV_VAR, DEFAULT_CYCLE_TIMEOUT_SECONDS
        )
        shutdown_timeout = _timeout_from_env(
            environ, SHUTDOWN_TIMEOUT_ENV_VAR, DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
        )

        poller = cls(
            store=store,
            interval_seconds=interval_seconds,
            enabled=enabled,
            vendor_timeout_seconds=vendor_timeout,
            cycle_timeout_seconds=cycle_timeout,
            shutdown_timeout_seconds=shutdown_timeout,
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
                poller._unconfigured_vendors.add(vendor_id)
                logger.warning(
                    "Skipping vendor '%s': %s",
                    vendor_id,
                    _safe_error_message(exc),
                )
                continue
            poller._connectors[vendor_id] = connector
            # Connectors built by from_config() are owned by this poller and
            # may be closed on shutdown. Connectors injected elsewhere remain
            # caller-owned.
            poller._owns_connectors[vendor_id] = True
            # Remember how to build a fresh connector so a later start() after
            # a stop() does not reuse this one's closed HTTP client.
            poller._connector_factories[vendor_id] = (
                lambda vendor=vendor: make_connector(vendor, environ=environ)
            )

        return poller

    @property
    def unconfigured_vendors(self) -> set[str]:
        """Vendor identifiers that are currently unavailable.

        A vendor appears here when its required configuration was missing at
        construction time, or when its owned connector could not be recreated
        after a restart. The status endpoint exposes the same set.
        """

        return set(self._unconfigured_vendors)

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

    def status(self) -> PollerStatus:
        """Return an immutable snapshot of the poller state.

        Reading the snapshot never triggers a poll and never contacts a
        device. Collection fields are tuples so callers cannot mutate the
        poller's internal state through the returned value.
        """

        return PollerStatus(
            enabled=self._enabled,
            running=self.is_running,
            interval_seconds=self._interval_seconds,
            configured_vendors=tuple(sorted(self._connectors)),
            unconfigured_vendors=tuple(sorted(self._unconfigured_vendors)),
            last_cycle_started_at=self._last_cycle_started_at,
            last_cycle_finished_at=self._last_cycle_finished_at,
            last_cycle_outcome=self._last_cycle_outcome,
            last_error=self._last_error,
        )

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

        # Capture the loop before creating the task so worker threads can
        # schedule deferred cleanup thread-safely. If the previous run ended
        # in stop(), its owned connectors were closed; recreate them so the
        # restarted loop never polls through a closed HTTP client.
        was_shutdown_completed = self._shutdown_completed
        self._loop = asyncio.get_running_loop()
        if was_shutdown_completed:
            # Factory-level failures are contained inside the helper and
            # reported through unconfigured_vendors; a missing factory raises
            # RuntimeError because the closed connector cannot be reopened.
            self._recreate_owned_connectors()

        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        # A new start clears prior shutdown state so the poller can be
        # restarted after a previous stop(). Pending deferred closes are NOT
        # cleared: a connector left open by an abandoned worker is still owned
        # and must be closed when that worker finally finishes.
        self._shutdown_in_progress = False
        self._shutdown_completed = False
        self._shutdown_abandoned_workers = False
        logger.info(
            "Telemetry poller started (interval=%.fs, enabled=%s, targets=%d)",
            self._interval_seconds,
            self._enabled,
            len(self._connectors),
        )

    async def stop(self) -> None:
        """Stop the poller and wait for clean shutdown within the deadline.

        Stops scheduling new polling cycles, cancels the poll loop task, and
        waits for it to finish within ``_shutdown_timeout_seconds``. Safe to
        call when not running, after a previous stop, or multiple times
        (idempotent).

        Python cannot terminate a blocked worker thread, so a timed-out
        synchronous fetch may keep its worker alive past the shutdown
        deadline. In that case the connector is NOT closed (the worker may
        still be using it) and incomplete shutdown is recorded rather than
        claimed successful. The shutdown operation itself is always bounded:
        after the deadline elapses, stop() returns without waiting further,
        and the abandoned poll loop task is left to finish in the background.
        """
        try:
            if self._shutdown_completed:
                return

            if self._shutdown_in_progress:
                # A concurrent stop() is already in progress; wait for it to
                # finish, bounded by the shutdown deadline.
                try:
                    await asyncio.wait_for(
                        self._shutdown_done.wait(),
                        timeout=self._shutdown_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
                return

            self._shutdown_in_progress = True

            # Stop scheduling new poll cycles, cancel the poll loop task, and
            # wait for it to finish, bounded by the shutdown deadline. A loop
            # that suppresses cancellation (or a blocked vendor worker) keeps
            # running after wait_for() abandons it, so we poll the task's
            # done-state against a wall-clock deadline instead of relying on
            # asyncio.wait_for() on the task itself.
            self._stop_event.set()
            if self._task is not None:
                task = self._task
                task.cancel()
                deadline = asyncio.get_event_loop().time() + self._shutdown_timeout_seconds
                while not task.done() and asyncio.get_event_loop().time() < deadline:
                    await asyncio.sleep(0.01)
                if not task.done():
                    # Still running after the deadline: abandon it (left
                    # running in the background) and record the incomplete
                    # shutdown.
                    self._shutdown_abandoned_workers = True
                    logger.warning(
                        "Poller shutdown did not wait for the loop task to "
                        "finish; in-flight vendor workers may still be active.",
                    )
                else:
                    # Finished cleanly (or was cancelled); drop the reference
                    # so is_running() reports False.
                    self._task = None

            # Blocked vendor worker threads cannot be killed, so any vendor
            # still active at shutdown time is an abandoned worker whose
            # owned connector is deferred to _drain_close.
            if self._active_vendors:
                self._shutdown_abandoned_workers = True

            # Close owned connectors whose workers are not active. Owned
            # connectors with active workers are deferred to _drain_close,
            # which closes them once that connector's last worker finishes.
            for vendor, connector in list(self._connectors.items()):
                if not self._owns_connectors.get(vendor):
                    continue  # caller-owned; never close
                key = id(connector)
                if key in self._pending_close:
                    # This connector was already deferred by an earlier
                    # shutdown. The drain task owns the close; closing it here
                    # as well would attempt a second close.
                    continue
                if key in self._reserved_connectors:
                    # A worker thread is starting or running.  Do not close
                    # inline; defer to the drain task so the worker can finish.
                    if key not in self._pending_close:
                        self._pending_close[key] = (vendor, connector)
                    continue
                with self._active_vendors_lock:
                    active = self._active_connectors.get(key, 0)
                if active > 0:
                    self._pending_close[key] = (vendor, connector)
                else:
                    # Only close if not already closed.
                    if key not in self._closed_connectors:
                        self._closed_connectors.add(key)
                        try:
                            connector.close()
                        except Exception as exc:  # pragma: no cover - best effort
                            safe_error = _safe_error_message(exc)
                            logger.warning(
                                "Close failed for connector vendor=%s: %s",
                                vendor,
                                safe_error,
                            )
            # Schedule at most one drain task, after the scan, so multiple
            # pending vendors do not create duplicate cleanup tasks.
            self._schedule_drain()
        finally:
            self._shutdown_in_progress = False
            self._shutdown_completed = True
            self._shutdown_done.set()

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
            self._last_cycle_started_at = _now_iso()
            self._last_cycle_finished_at = None
            self._last_cycle_outcome = None
            self._last_error = None

            try:
                errors = await asyncio.wait_for(
                    self._poll_all(), timeout=self._cycle_timeout_seconds
                )
            except asyncio.TimeoutError:
                # Cycle deadline exceeded before every vendor was polled.
                # Timed-out worker threads are not cancelled (Python cannot
                # kill threads), but their results are never written to the
                # store, so a late completion cannot corrupt cycle state.
                self._last_cycle_finished_at = _now_iso()
                self._last_cycle_outcome = "error"
                self._last_error = "Poll cycle timed out."
                logger.warning(
                    "Poll cycle timed out after %.1fs.",
                    self._cycle_timeout_seconds,
                )
            except Exception as exc:  # noqa: BLE001 - keep the poller alive
                safe_error = _safe_error_message(exc)
                self._last_cycle_finished_at = _now_iso()
                self._last_cycle_outcome = "error"
                self._last_error = safe_error
                logger.warning(
                    "Unexpected telemetry poll cycle failure: %s",
                    safe_error,
                )
            else:
                if errors:
                    self._last_cycle_finished_at = _now_iso()
                    self._last_cycle_outcome = "degraded"
                    # Keep the poller-level error free of hostnames and raw
                    # connector text. Detailed sanitized vendor errors remain
                    # in the stored readings exposed by GET /telemetry/latest.
                    self._last_error = (
                        "1 vendor poll failed."
                        if len(errors) == 1
                        else f"{len(errors)} vendor polls failed."
                    )
                else:
                    self._last_cycle_finished_at = _now_iso()
                    self._last_cycle_outcome = "success"

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
                # Interval elapsed: attempt a poll cycle. Unexpected cycle
                # failures are recorded and contained so later cycles still run.
                try:
                    await self.poll_once()
                except Exception as exc:  # noqa: BLE001 - keep the poller alive
                    safe_error = _safe_error_message(exc)
                    self._last_cycle_finished_at = _now_iso()
                    self._last_cycle_outcome = "error"
                    self._last_error = safe_error
                    logger.warning(
                        "Unexpected telemetry poll cycle failure: %s",
                        safe_error,
                    )

    async def _poll_all(self) -> list[str]:
        errors: list[str] = []
        # Snapshot the mapping: remove_connector()/restart may replace entries
        # while a cycle is awaiting a vendor worker. _poll_vendor() re-checks
        # identity before fetching so a removed connector is never polled.
        for vendor, connector in list(self._connectors.items()):
            error = await self._poll_vendor(vendor, connector)
            if error is not None:
                errors.append(error)
        return errors

    def _fetch_tracked(
        self, vendor: str, connector: SandboxConnector,
        check_reservation: bool = False,
        worker_started: "threading.Event | None" = None,
    ) -> "object":
        """Run ``connector.fetch_telemetry()`` in a worker thread.

        Tracks the vendor in ``_active_vendors`` for the lifetime of the
        worker *thread*: the vendor is added when the thread begins executing
        and removed in a ``finally`` that runs when the thread completes
        (whether successfully, with an exception, or after abandonment).
        This differs from marking the coroutine as active: when
        ``asyncio.wait_for`` abandons a timed-out fetch, the coroutine
        yields but the thread keeps running, and the vendor must remain
        marked active until the thread actually finishes.

        When ``check_reservation`` is True the worker thread verifies that its
        reservation is still present before running.  If the reservation was
        cleared (e.g. the coroutine was cancelled before the worker started)
        the fetch is skipped and ``_WORKER_NOT_STARTED`` is returned.
        """

        key = id(connector)
        with self._active_vendors_lock:
            if check_reservation:
                if key not in self._reserved_connectors:
                    # Reservation was cleared before the worker started;
                    # do not run the fetch.
                    return _WORKER_NOT_STARTED
                self._reserved_connectors.discard(key)
            self._active_vendors[vendor] = self._active_vendors.get(vendor, 0) + 1
            self._active_connectors[key] = self._active_connectors.get(key, 0) + 1
            if worker_started is not None:
                worker_started.set()
        try:
            return connector.fetch_telemetry()
        finally:
            schedule_cleanup = False
            with self._active_vendors_lock:
                remaining = self._active_vendors.get(vendor, 1) - 1
                if remaining <= 0:
                    self._active_vendors.pop(vendor, None)
                else:
                    self._active_vendors[vendor] = remaining

                connector_remaining = self._active_connectors.get(key, 1) - 1
                if connector_remaining <= 0:
                    self._active_connectors.pop(key, None)
                    # The last worker for this connector just finished. If it
                    # was deferred at shutdown, wake the loop so it can close
                    # the connector now -- even if the drain deadline already
                    # expired while this worker was still running.
                    schedule_cleanup = key in self._pending_close
                else:
                    self._active_connectors[key] = connector_remaining
            if schedule_cleanup:
                self._schedule_drain_from_worker()

    def _schedule_drain_from_worker(self) -> None:
        """Wake the event loop from a worker thread to run deferred cleanup."""

        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._schedule_drain)
        except RuntimeError:
            # The loop began shutting down between the check and the call.
            pass

    def _schedule_drain(self) -> None:
        """Create at most one deferred-cleanup task while work is pending."""

        if not self._pending_close:
            return
        current = self._drain_task
        if current is not None and not current.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._loop = loop
        task = loop.create_task(self._drain_close())
        self._drain_task = task
        task.add_done_callback(self._on_drain_done)

    def _on_drain_done(self, task: "asyncio.Task[None]") -> None:
        """Observe a finished drain task and schedule follow-up if needed."""

        if self._drain_task is task:
            self._drain_task = None
        if task.cancelled():
            # Cancellation means the loop is going away; leave the remaining
            # connectors pending rather than closing them from a dying loop.
            return
        exc = task.exception()
        if exc is not None:  # pragma: no cover - defensive
            logger.warning(
                "Deferred connector cleanup failed: %s", _safe_error_message(exc)
            )
        # A worker may have finished while the drain was running, leaving a
        # connector that is now idle but was not in the drain's snapshot.
        # Re-run once every remaining pending connector is idle; otherwise the
        # worker completion hook schedules the follow-up.
        if self._pending_close:
            with self._active_vendors_lock:
                any_active = any(
                    self._active_connectors.get(key, 0) > 0
                    for key in self._pending_close
                )
            if not any_active:
                self._schedule_drain()

    async def _drain_close(self) -> None:
        """Close owned connectors whose workers have finished.

        stop() closes owned, idle connectors immediately but defers owned
        connectors with active workers. This task waits (bounded by the
        shutdown deadline measured from when it runs) for each pending
        connector's worker count to reach zero, then closes that connector
        exactly once. If the deadline expires while a worker is still active,
        the connector is left open and remains pending: closing it mid-fetch
        would be unsafe. The worker's completion hook schedules another drain,
        so cleanup still happens after the worker eventually finishes.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._shutdown_timeout_seconds
        for key, (vendor, connector) in list(self._pending_close.items()):
            if key in self._reserved_connectors:
                # Connector is reserved; a worker is starting or running.
                # Skip for now; the worker completion hook will schedule
                # another drain when it finishes.
                continue
            should_close = False
            while True:
                with self._active_vendors_lock:
                    active = self._active_connectors.get(key, 0)
                if active <= 0:
                    should_close = True
                    break
                if loop.time() >= deadline:
                    logger.warning(
                        "Leaving connector for vendor=%s open: worker still "
                        "active after shutdown deadline; it will be closed "
                        "when the worker finishes.",
                        vendor,
                    )
                    break
                await asyncio.sleep(0.1)
            if not should_close:
                # Deadline expired with a live worker: keep the entry pending
                # so the worker completion hook can close it later.
                continue
            if key not in self._closed_connectors:
                self._closed_connectors.add(key)
                try:
                    connector.close()
                except Exception as exc:  # pragma: no cover - shutdown best effort
                    safe_error = _safe_error_message(exc)
                    logger.warning(
                        "Close failed for connector vendor=%s: %s",
                        vendor,
                        safe_error,
                    )
            self._pending_close.pop(key, None)

    async def _poll_vendor(
        self, vendor: str, connector: SandboxConnector
    ) -> Optional[str]:
        if self._connectors.get(vendor) is not connector:
            # The connector was removed or replaced after this cycle took its
            # snapshot. Do not fetch through a detached/closed connector.
            logger.debug(
                "Skipping vendor=%s: connector was removed or replaced.", vendor
            )
            return None

        # Reserve the connector synchronously before any synchronous work
        # (including host access) so that remove_connector() and stop() see
        # the reservation and do not close it before the worker starts.
        key = id(connector)
        with self._active_vendors_lock:
            self._reserved_connectors.add(key)

        # Track whether the worker thread actually started.  Used in the
        # finally block to decide whether to release the reservation.
        import threading
        worker_started = threading.Event()

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
        # still returned (sanitized) and logged so operators can see the
        # attempt failed.
        #
        # The connector's fetch_telemetry() is synchronous (blocking I/O),
        # so it runs off the event loop via asyncio.to_thread(). Because
        # Python cannot terminate a blocked thread, asyncio.wait_for() only
        # abandons the waiting coroutine after vendor_timeout_seconds: the
        # worker thread may continue until httpx's own timeout fires. The
        # result is never written to the store after a timeout, so a late
        # completion cannot resurrect a vendor the cycle already reported as
        # failed.

        host = getattr(connector, "host", None) or "unknown"
        known = self._last_identity.get(vendor)
        if known is not None:
            device_id, interface = known
        else:
            device_id = None
            interface = None

        attempt_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        cancelled = False

        try:
            telemetry = await asyncio.wait_for(
                asyncio.to_thread(self._fetch_tracked, vendor, connector, True, worker_started),
                timeout=self._vendor_timeout_seconds,
            )
        except asyncio.CancelledError:
            # Coroutine cancelled (e.g. by stop()).  Do not discard the
            # reservation here; the worker thread may still be running.
            cancelled = True
            raise
        except asyncio.TimeoutError:
            # ... (existing code unchanged)
            safe_error = "Vendor poll timed out."
            logger.warning(
                "Poll timed out for vendor=%s host=%s after %.1fs.",
                vendor,
                host,
                self._vendor_timeout_seconds,
            )
            if device_id is not None:
                self._record_failure(
                    vendor=vendor,
                    device_id=device_id,
                    interface=interface,
                    attempt_at=attempt_at,
                    error=safe_error,
                )
            return safe_error
        except SandboxConnectorError as exc:
            safe_error = _safe_error_message(exc)
            logger.warning(
                "Poll failed for vendor=%s host=%s: %s",
                vendor,
                host,
                safe_error,
            )
            if device_id is not None:
                self._record_failure(
                    vendor=vendor,
                    device_id=device_id,
                    interface=interface,
                    attempt_at=attempt_at,
                    error=safe_error,
                )
            return safe_error
        except Exception as exc:
            safe_error = _safe_error_message(exc)
            logger.warning(
                "Poll failed unexpectedly for vendor=%s host=%s: %s",
                vendor,
                host,
                safe_error,
            )
            if device_id is not None:
                self._record_failure(
                    vendor=vendor,
                    device_id=device_id,
                    interface=interface,
                    attempt_at=attempt_at,
                    error=safe_error,
                )
            return safe_error
        finally:
            # If the worker thread never started (submission failed or the
            # coroutine was cancelled before the worker ran), release the
            # reservation.  If the coroutine was cancelled, keep it so that
            # stop() and remove_connector() defer the close.  If the worker
            # did start, _fetch_tracked already released it.
            if not cancelled and not worker_started.is_set():
                with self._active_vendors_lock:
                    self._reserved_connectors.discard(key)

        # If the worker was never started (reservation cleared before the
        # worker ran), skip processing.
        if telemetry is _WORKER_NOT_STARTED:
            return None

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
        return None

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
    def _recreate_owned_connectors(self) -> None:
        """Install fresh owned connectors after a completed shutdown.

        ``stop()`` closes owned connectors, whose HTTP clients cannot be
        reopened. A restart therefore builds a new connector from the factory
        recorded by :meth:`from_config` (or registered by a test seam) before
        the new poll loop starts. Caller-owned connectors have no factory and
        are never replaced or closed.

        A vendor whose factory raises is detached from the active connector
        set and reported as unavailable, so the restarted loop can never poll
        through the closed connector. If an owned connector has no factory at
        all, restart is impossible: every owned connector is detached first so
        a direct ``poll_once()`` cannot touch a closed client, and
        :class:`RuntimeError` is raised.
        """

        owned = [
            vendor for vendor, is_owned in self._owns_connectors.items() if is_owned
        ]
        missing = [
            vendor for vendor in owned if vendor not in self._connector_factories
        ]
        if missing:
            for vendor in owned:
                self._connectors.pop(vendor, None)
            self._unconfigured_vendors.update(owned)
            raise RuntimeError(
                "Cannot restart telemetry poller: owned connector(s) for "
                "vendor(s) "
                f"{', '.join(sorted(missing))} have no recreation factory."
            )

        unavailable: list[str] = []
        for vendor in owned:
            factory = self._connector_factories[vendor]
            try:
                connector = factory()
            except Exception as exc:  # noqa: BLE001 - isolate per-vendor restart
                # Never keep the closed connector installed: the poll loop and
                # poll_once() both consult _connectors.
                self._connectors.pop(vendor, None)
                self._unconfigured_vendors.add(vendor)
                unavailable.append(vendor)
                logger.error(
                    "Could not recreate owned connector for vendor=%s; "
                    "vendor is unavailable: %s",
                    vendor,
                    _safe_error_message(exc),
                )
                continue
            self._connectors[vendor] = connector
            self._unconfigured_vendors.discard(vendor)

        if unavailable:
            logger.error(
                "Telemetry poller restart is degraded; unavailable vendor(s): %s",
                ", ".join(sorted(unavailable)),
            )

    def add_connector(self, vendor: str, connector: SandboxConnector) -> None:
        """Register a connector for ``vendor``. Used mainly in tests.

        Connectors added here are caller-owned: this poller does not close
        them on shutdown.
        """

        self._connectors[vendor] = connector
        self._owns_connectors[vendor] = False
        self._connector_factories.pop(vendor, None)
        self._unconfigured_vendors.discard(vendor)

    def remove_connector(self, vendor: str) -> None:
        """Remove the connector for ``vendor``.

        Caller-owned connectors are detached without being closed. Owned
        connectors are closed here when idle. If a worker is still using an
        owned connector, it remains in ``_pending_close`` so the existing
        worker-completion hook schedules the deferred close after the worker
        finishes. Pending cleanup entries are never discarded for an active
        connector.

        Reservation and pending-close checks ensure that a connector is never
        closed twice, even if it was already closed or deferred by a prior
        ``stop()`` or ``_drain_close()``.
        """

        connector = self._connectors.pop(vendor, None)
        is_owned = self._owns_connectors.pop(vendor, False)
        self._connector_factories.pop(vendor, None)
        if connector is None or not is_owned:
            return

        key = id(connector)

        # If the connector is reserved (a worker thread is starting or
        # running), do not close it here.  Defer to the drain task.
        if key in self._reserved_connectors:
            if key not in self._pending_close:
                self._pending_close[key] = (vendor, connector)
            self._schedule_drain()
            return

        # If the connector is already pending close (e.g. from a prior
        # stop()), the drain task owns the close.  Do not close again.
        if key in self._pending_close:
            return

        # If the connector was already closed by the poller, do not close again.
        if key in self._closed_connectors:
            self._pending_close.pop(key, None)
            return

        with self._active_vendors_lock:
            active = self._active_connectors.get(key, 0)
        if active > 0:
            # Keep the pending entry so the worker completion hook schedules
            # the deferred close.
            self._pending_close[key] = (vendor, connector)
            self._schedule_drain()
            return

        self._pending_close.pop(key, None)
        if key not in self._closed_connectors:
            self._closed_connectors.add(key)
            try:
                connector.close()
            except Exception as exc:  # pragma: no cover - best effort
                logger.warning(
                    "Close failed for removed connector vendor=%s: %s",
                    vendor,
                    _safe_error_message(exc),
                )

    @property
    def connectors(self) -> dict[str, SandboxConnector]:
        return dict(self._connectors)