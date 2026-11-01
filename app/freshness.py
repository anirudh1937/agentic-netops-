"""Read-time freshness assessment for stored telemetry readings.

Freshness is intentionally **not persisted**. It is derived from the last
successful collection timestamp whenever a consumer reads a stored reading.
This keeps the store unchanged, makes recovery automatic after a successful
poll, and ensures an old measurement can never be presented as live.

The authoritative clock is ``last_successful_fetched_at`` -- the server-side
time of the last successful poll. ``timestamp`` and ``fetched_at`` are updated
by failed attempts too, so they must not be used to decide whether the stored
measurement itself is still fresh.

No network access, no background work, and no automatic remediation happen
here. The module is deliberately small so callers can share one classification
rule (the API) and tests can inject a fixed clock.
"""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Mapping, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

#: Default age after which a successfully collected reading is stale.
DEFAULT_STALE_THRESHOLD_SECONDS = 300.0

#: Environment variable used to override the stale threshold.
STALE_THRESHOLD_ENV_VAR = "NETOPS_STALE_THRESHOLD_SECONDS"

#: Small allowance for clock skew between the collector and the API host.
#: A success timestamp further in the future than this is treated as invalid
#: rather than as a fresh reading.
FUTURE_TIMESTAMP_TOLERANCE_SECONDS = 60.0


class FreshnessState(str, Enum):
    """Collection freshness states for stored readings and vendors.

    ``HEALTHY``, ``STALE``, ``POLLING_FAILED`` and ``UNKNOWN`` describe a
    stored reading. ``UNCONFIGURED`` is vendor-level and comes from the
    poller's existing ``unconfigured_vendors`` configuration information; it is
    never attached to a fabricated device reading.
    """

    HEALTHY = "HEALTHY"
    STALE = "STALE"
    POLLING_FAILED = "POLLING_FAILED"
    UNCONFIGURED = "UNCONFIGURED"
    UNKNOWN = "UNKNOWN"


class FreshnessAssessment(BaseModel):
    """Result of assessing one reading at a single point in time."""

    state: FreshnessState = Field(
        description="Collection freshness state for the reading."
    )
    age_seconds: Optional[float] = Field(
        default=None,
        description="Age of the last successful collection in seconds. None "
        "when the success timestamp is missing or malformed. A future "
        "timestamp outside the documented tolerance yields a negative age and "
        "an UNKNOWN state.",
    )
    threshold_seconds: float = Field(
        description="Stale threshold applied to this assessment, in seconds."
    )


def parse_iso_timestamp(value: object) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp into an aware UTC datetime.

    Returns ``None`` for missing, non-string, empty, or malformed values.
    Naive timestamps are treated as UTC because every timestamp produced by
    this project is UTC; callers should not assume local time. The trailing
    ``Z`` form is accepted for interoperability.
    """

    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def _normalise_now(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def compute_age_seconds(
    last_successful_fetched_at: object,
    *,
    now: Optional[datetime] = None,
) -> Optional[float]:
    """Return the age, in seconds, of a successful collection timestamp.

    ``now`` is injectable so tests can use a fixed clock without sleeping.
    Missing or malformed timestamps return ``None``; they are never replaced
    with the current time.
    """

    success_at = parse_iso_timestamp(last_successful_fetched_at)
    if success_at is None:
        return None

    current = _normalise_now(now)
    return round((current - success_at).total_seconds(), 3)


def read_stale_threshold_seconds(
    environ: Optional[Mapping[str, str]] = None,
) -> float:
    """Read the stale threshold from the environment.

    ``NETOPS_STALE_THRESHOLD_SECONDS`` must be a positive, finite number of
    seconds. Missing, blank, zero, negative, non-finite, or malformed values
    fall back to :data:`DEFAULT_STALE_THRESHOLD_SECONDS` so a bad setting can
    never mark every reading healthy or stale.
    """

    source = os.environ if environ is None else environ
    raw = source.get(STALE_THRESHOLD_ENV_VAR)

    if raw is None:
        return DEFAULT_STALE_THRESHOLD_SECONDS

    text = str(raw).strip()
    if not text:
        return DEFAULT_STALE_THRESHOLD_SECONDS

    try:
        value = float(text)
    except (TypeError, ValueError):
        logger.warning(
            "%s must be a positive number; using the default of %.0f seconds.",
            STALE_THRESHOLD_ENV_VAR,
            DEFAULT_STALE_THRESHOLD_SECONDS,
        )
        return DEFAULT_STALE_THRESHOLD_SECONDS

    if not math.isfinite(value) or value <= 0:
        logger.warning(
            "%s must be a positive, finite number; using the default of %.0f "
            "seconds.",
            STALE_THRESHOLD_ENV_VAR,
            DEFAULT_STALE_THRESHOLD_SECONDS,
        )
        return DEFAULT_STALE_THRESHOLD_SECONDS

    return value


def assess_freshness(
    reading: object,
    *,
    now: Optional[datetime] = None,
    threshold_seconds: Optional[float] = None,
    future_tolerance_seconds: float = FUTURE_TIMESTAMP_TOLERANCE_SECONDS,
) -> FreshnessAssessment:
    """Assess a stored reading's collection freshness.

    Classification precedence:

    1. No valid ``last_successful_fetched_at`` -> ``UNKNOWN``.
    2. A timestamp further in the future than ``future_tolerance_seconds`` ->
       ``UNKNOWN`` (clock skew or bad data; never ``HEALTHY``).
    3. Age greater than the threshold -> ``STALE``, even if the latest attempt
       failed. Old data is the more important operational signal.
    4. Latest poll failed but the last success is within the threshold ->
       ``POLLING_FAILED``.
    5. Latest poll succeeded and the last success is within the threshold ->
       ``HEALTHY``.
    6. Any other/missing outcome -> ``UNKNOWN``.

    The threshold defaults to the environment setting and ``now`` defaults to
    the current UTC time. Both are injectable for deterministic tests.
    """

    if threshold_seconds is None:
        threshold_seconds = read_stale_threshold_seconds()

    if not math.isfinite(threshold_seconds) or threshold_seconds <= 0:
        threshold_seconds = DEFAULT_STALE_THRESHOLD_SECONDS

    current = _normalise_now(now)
    age = compute_age_seconds(
        getattr(reading, "last_successful_fetched_at", None),
        now=current,
    )

    if age is None:
        return FreshnessAssessment(
            state=FreshnessState.UNKNOWN,
            age_seconds=None,
            threshold_seconds=threshold_seconds,
        )

    if age < -future_tolerance_seconds:
        return FreshnessAssessment(
            state=FreshnessState.UNKNOWN,
            age_seconds=age,
            threshold_seconds=threshold_seconds,
        )

    # Within the clock-skew tolerance, treat a slightly future timestamp as
    # just collected rather than reporting a negative age.
    effective_age = max(age, 0.0)

    if effective_age > threshold_seconds:
        state = FreshnessState.STALE
    else:
        outcome = getattr(reading, "last_poll_outcome", None)
        if outcome == "failure":
            state = FreshnessState.POLLING_FAILED
        elif outcome == "success":
            state = FreshnessState.HEALTHY
        else:
            state = FreshnessState.UNKNOWN

    return FreshnessAssessment(
        state=state,
        age_seconds=effective_age,
        threshold_seconds=threshold_seconds,
    )
