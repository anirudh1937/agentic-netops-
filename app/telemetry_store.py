"""In-memory telemetry store for the Agentic NetOps API.

This module provides a small, dependency-free telemetry store used by the
controlled periodic polling feature (Milestone 2). Readings are held in a
plain Python dict and are **lost when the process restarts** -- no database or
external dependency is introduced in this milestone.

Each reading is keyed by the composite identity ``(vendor, device_id,
interface)`` so telemetry from different devices never overwrites one another.

Freshness model
---------------
Every poll attempt (success or failure) updates the ``fetched_at`` and
``timestamp`` fields to reflect the latest attempt time, and records the
outcome in ``error`` (set on failure, cleared on success). The last
*successful* measurement is tracked separately in
``last_successful_timestamp`` / ``last_successful_fetched_at`` and is never
clobbered by a failed poll. This lets consumers distinguish "fresh" (recently
and successfully collected) from "stale" (last success is old, latest attempt
failed) without old telemetry ever masquerading as live.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class TelemetryReading(BaseModel):
    """A single stored telemetry reading plus its diagnosis outcome.

    ``vendor`` identifies the connector that produced the reading
    (``cisco``, ``juniper``, ``arista``). ``device_id`` and ``interface``
    identify the specific device/interface the reading came from, so the same
    interface name on two different devices stays separate.

    ``telemetry`` holds the normalized ``NetworkTelemetry`` shape returned by
    the connector. ``diagnosis`` holds the output of ``diagnose_network_issue``
    computed at fetch time. ``error`` is set only when the latest poll attempt
    for this identity failed; on failure the previous successful telemetry and
    diagnosis are preserved rather than overwritten.
    """

    vendor: str
    device_id: str
    interface: str
    #: Device-reported collection timestamp from the latest poll attempt
    #: (ISO 8601). On a failed poll this reflects the attempt time rather than
    #: a device measurement; use ``last_successful_timestamp`` for the last
    #: confirmed device measurement.
    timestamp: str = Field(
        description="Device-reported collection timestamp from the latest poll attempt (ISO 8601)."
    )
    #: Server-side timestamp when the reading was stored (ISO 8601). Updated on
    #: every poll attempt, successful or failed.
    fetched_at: str = Field(
        description="Server-side timestamp of the latest poll attempt (ISO 8601)."
    )
    source: str = Field(
        description="Connector/vendor label that produced the reading."
    )
    telemetry: dict = Field(
        default_factory=dict,
        description="Normalized telemetry payload from the last successful poll "
        "(NetworkTelemetry.model_dump()). Empty on first-poll failure.",
    )
    diagnosis: dict = Field(
        default_factory=dict,
        description="Diagnosis result from the last successful poll "
        "(diagnose_network_issue output). Empty on first-poll failure.",
    )
    error: Optional[str] = Field(
        default=None,
        description="Error message from the latest failed poll attempt, or None "
        "when the latest attempt succeeded.",
    )
    #: Device-reported timestamp from the last *successful* poll (ISO 8601).
    #: Absent (empty string) until the first successful poll for this identity.
    last_successful_timestamp: str = Field(
        default="",
        description="Device-reported timestamp from the last successful poll "
        "(ISO 8601). Empty until the first success.",
    )
    #: Server-side timestamp of the last *successful* poll (ISO 8601).
    last_successful_fetched_at: str = Field(
        default="",
        description="Server-side timestamp of the last successful poll "
        "(ISO 8601). Empty until the first success.",
    )
    #: Server-side timestamp of the latest poll attempt, successful or failed
    #: (ISO 8601). Mirrors ``fetched_at`` but is kept as an explicit attempt
    #: marker so consumers can compare last-attempt vs last-success freshness.
    last_poll_attempt_at: str = Field(
        default="",
        description="Server-side timestamp of the latest poll attempt "
        "(ISO 8601).",
    )
    #: Outcome of the latest poll attempt: ``"success"`` or ``"failure"``.
    last_poll_outcome: Literal["success", "failure"] = Field(
        default="failure",
        description="Outcome of the latest poll attempt ('success' or 'failure').",
    )
    #: Number of consecutive failed poll attempts for this identity.
    consecutive_failures: int = Field(
        default=0,
        description="Number of consecutive failed poll attempts for this identity.",
    )


class TelemetryStore:
    """Thread-unsafe in-memory store of telemetry readings.

    Intended for use from a single FastAPI worker with an async poller that
    serializes access via an ``asyncio.Lock``. Not safe for concurrent use
    across multiple processes or workers.
    """

    def __init__(self) -> None:
        self._readings: dict[tuple[str, str, str], TelemetryReading] = {}

    def _key(self, vendor: str, device_id: str, interface: str) -> tuple[str, str, str]:
        return (vendor, device_id, interface)

    def put(
        self,
        *,
        vendor: str,
        device_id: str,
        interface: str,
        timestamp: str,
        fetched_at: str,
        source: str,
        telemetry: dict | None = None,
        diagnosis: dict | None = None,
        error: str | None = None,
    ) -> TelemetryReading:
        """Insert or replace the reading for ``(vendor, device_id, interface)``.

        The ``timestamp``/``fetched_at`` arguments always reflect the latest
        poll attempt. On a failed poll (``error`` is set) the previous
        successful telemetry, diagnosis, and success timestamps are preserved
        so stale measurements never masquerade as fresh. On a successful poll
        the success timestamps and telemetry/diagnosis are updated and the
        error is cleared.
        """

        key = self._key(vendor, device_id, interface)
        existing = self._readings.get(key)

        telemetry_payload = telemetry if telemetry is not None else {}
        diagnosis_payload = diagnosis if diagnosis is not None else {}

        is_success = error is None

        if existing is not None and not is_success:
            # Failure with an existing reading: preserve last-success payloads
            # and success timestamps, increment failure count.
            telemetry_payload = existing.telemetry
            diagnosis_payload = existing.diagnosis
            last_successful_timestamp = existing.last_successful_timestamp
            last_successful_fetched_at = existing.last_successful_fetched_at
            consecutive_failures = existing.consecutive_failures + 1
        elif not is_success:
            # First-time failure for this identity: no prior success to
            # preserve; success timestamps stay empty.
            last_successful_timestamp = ""
            last_successful_fetched_at = ""
            consecutive_failures = 1
        else:
            # Success: update everything, clear error, reset failure count.
            last_successful_timestamp = timestamp
            last_successful_fetched_at = fetched_at
            consecutive_failures = 0

        reading = TelemetryReading(
            vendor=vendor,
            device_id=device_id,
            interface=interface,
            timestamp=timestamp,
            fetched_at=fetched_at,
            source=source,
            telemetry=telemetry_payload,
            diagnosis=diagnosis_payload,
            error=error,
            last_successful_timestamp=last_successful_timestamp,
            last_successful_fetched_at=last_successful_fetched_at,
            last_poll_attempt_at=fetched_at,
            last_poll_outcome="success" if is_success else "failure",
            consecutive_failures=consecutive_failures,
        )
        self._readings[key] = reading
        return reading

    def get(
        self, vendor: str, device_id: str, interface: str
    ) -> TelemetryReading | None:
        """Return the latest reading for ``(vendor, device_id, interface)``."""

        return self._readings.get(self._key(vendor, device_id, interface))

    def get_all(self) -> list[TelemetryReading]:
        """Return all stored readings."""

        return list(self._readings.values())

    def clear(self) -> None:
        """Remove all readings. Used by tests."""

        self._readings.clear()

    def __len__(self) -> int:
        return len(self._readings)
