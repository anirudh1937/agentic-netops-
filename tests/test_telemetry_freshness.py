"""Deterministic unit tests for read-time telemetry freshness.

All tests use a fixed clock and in-memory objects only. No test sleeps, reads
the wall clock for classification, contacts a device, or reaches an external
service.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.freshness import (
    DEFAULT_STALE_THRESHOLD_SECONDS,
    FUTURE_TIMESTAMP_TOLERANCE_SECONDS,
    STALE_THRESHOLD_ENV_VAR,
    FreshnessState,
    assess_freshness,
    compute_age_seconds,
    parse_iso_timestamp,
    read_stale_threshold_seconds,
)
from app.telemetry_store import TelemetryStore

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _iso(seconds_before_now: float = 0, *, at: datetime | None = None) -> str:
    moment = at if at is not None else NOW - timedelta(seconds=seconds_before_now)
    return moment.isoformat(timespec="seconds")


def _reading(
    *,
    success_seconds_ago: float = 0,
    outcome: str = "success",
    success_at: object = ...,
):
    """Minimal reading-like object exposing only the freshness inputs."""

    from types import SimpleNamespace

    return SimpleNamespace(
        last_successful_fetched_at=(
            _iso(success_seconds_ago) if success_at is ... else success_at
        ),
        last_poll_outcome=outcome,
    )


# -------------------------------------------------------------------------- #
# Classification
# -------------------------------------------------------------------------- #


def test_recent_successful_telemetry_is_healthy():
    assessment = assess_freshness(
        _reading(success_seconds_ago=30),
        now=NOW,
        threshold_seconds=300,
    )

    assert assessment.state is FreshnessState.HEALTHY
    assert assessment.age_seconds == 30.0
    assert assessment.threshold_seconds == 300.0


def test_exactly_at_threshold_is_not_stale():
    assessment = assess_freshness(
        _reading(success_seconds_ago=300),
        now=NOW,
        threshold_seconds=300,
    )

    assert assessment.age_seconds == 300.0
    assert assessment.state is FreshnessState.HEALTHY


def test_older_than_threshold_is_stale():
    assessment = assess_freshness(
        _reading(success_seconds_ago=301),
        now=NOW,
        threshold_seconds=300,
    )

    assert assessment.state is FreshnessState.STALE
    assert assessment.age_seconds == 301.0


def test_recent_polling_failure_is_polling_failed():
    assessment = assess_freshness(
        _reading(success_seconds_ago=90, outcome="failure"),
        now=NOW,
        threshold_seconds=300,
    )

    assert assessment.state is FreshnessState.POLLING_FAILED
    assert assessment.age_seconds == 90.0


def test_failure_beyond_threshold_is_stale_not_polling_failed():
    """Stale takes precedence over the latest poll failure."""

    assessment = assess_freshness(
        _reading(success_seconds_ago=900, outcome="failure"),
        now=NOW,
        threshold_seconds=300,
    )

    assert assessment.state is FreshnessState.STALE
    assert assessment.age_seconds == 900.0


def test_failure_exactly_at_threshold_is_polling_failed():
    assessment = assess_freshness(
        _reading(success_seconds_ago=300, outcome="failure"),
        now=NOW,
        threshold_seconds=300,
    )

    assert assessment.state is FreshnessState.POLLING_FAILED
    assert assessment.age_seconds == 300.0


def test_state_enum_exposes_all_collection_and_vendor_states():
    assert {state.value for state in FreshnessState} == {
        "HEALTHY",
        "STALE",
        "POLLING_FAILED",
        "UNCONFIGURED",
        "UNKNOWN",
    }


def test_missing_success_timestamp_is_unknown():
    for value in ("", None, "   "):
        assessment = assess_freshness(
            _reading(success_at=value),
            now=NOW,
            threshold_seconds=300,
        )

        assert assessment.state is FreshnessState.UNKNOWN
        assert assessment.age_seconds is None
        assert assessment.threshold_seconds == 300.0


def test_malformed_success_timestamp_is_unknown():
    for value in ("not-a-date", "2026-13-45T99:99:99+00:00"):
        assessment = assess_freshness(
            _reading(success_at=value),
            now=NOW,
            threshold_seconds=300,
        )

        assert assessment.state is FreshnessState.UNKNOWN
        assert assessment.age_seconds is None


def test_future_timestamp_outside_tolerance_is_unknown():
    future = NOW + timedelta(
        seconds=FUTURE_TIMESTAMP_TOLERANCE_SECONDS + 1
    )

    assessment = assess_freshness(
        _reading(success_at=_iso(at=future)),
        now=NOW,
        threshold_seconds=300,
    )

    assert assessment.state is FreshnessState.UNKNOWN
    assert assessment.age_seconds is not None
    assert assessment.age_seconds < 0


def test_future_timestamp_within_tolerance_is_clamped_to_zero():
    future = NOW + timedelta(seconds=FUTURE_TIMESTAMP_TOLERANCE_SECONDS - 1)

    assessment = assess_freshness(
        _reading(success_at=_iso(at=future)),
        now=NOW,
        threshold_seconds=300,
    )

    assert assessment.state is FreshnessState.HEALTHY
    assert assessment.age_seconds == 0.0


def test_unknown_poll_outcome_is_unknown():
    assessment = assess_freshness(
        _reading(success_seconds_ago=10, outcome="mystery"),
        now=NOW,
        threshold_seconds=300,
    )

    assert assessment.state is FreshnessState.UNKNOWN


# -------------------------------------------------------------------------- #
# Recovery and multiple identities
# -------------------------------------------------------------------------- #


def test_recovery_after_failure_is_automatic():
    store = TelemetryStore()
    key = dict(
        vendor="cisco",
        device_id="router-01",
        interface="GigabitEthernet0/1",
        source="cisco",
    )

    store.put(
        **key,
        timestamp=_iso(120),
        fetched_at=_iso(120),
        telemetry={"cpu_percent": 50.0},
        diagnosis={"severity": "normal"},
    )
    assert assess_freshness(store.get_all()[0], now=NOW).state is (
        FreshnessState.HEALTHY
    )

    store.put(
        **key,
        timestamp=_iso(30),
        fetched_at=_iso(30),
        error="Device rejected authentication.",
    )
    failed = assess_freshness(store.get_all()[0], now=NOW)
    assert failed.state is FreshnessState.POLLING_FAILED
    # Last-success clock is preserved through the failure.
    assert failed.age_seconds == 120.0

    store.put(
        **key,
        timestamp=_iso(0),
        fetched_at=_iso(0),
        telemetry={"cpu_percent": 55.0},
        diagnosis={"severity": "normal"},
    )
    recovered = assess_freshness(store.get_all()[0], now=NOW)
    assert recovered.state is FreshnessState.HEALTHY
    assert recovered.age_seconds == 0.0


def test_first_failure_without_success_is_unknown():
    store = TelemetryStore()
    store.put(
        vendor="cisco",
        device_id="router-01",
        interface="GigabitEthernet0/1",
        timestamp=_iso(10),
        fetched_at=_iso(10),
        source="cisco",
        error="Device rejected authentication.",
    )

    assessment = assess_freshness(store.get_all()[0], now=NOW)

    assert assessment.state is FreshnessState.UNKNOWN
    assert assessment.age_seconds is None


def test_multiple_devices_and_interfaces_are_assessed_independently():
    store = TelemetryStore()

    identities = [
        ("cisco", "router-01", "GigabitEthernet0/1", 10, "success"),
        ("cisco", "router-02", "GigabitEthernet0/1", 600, "success"),
        ("juniper", "router-01", "ge-0/0/1", 60, "failure"),
        ("arista", "switch-01", "Ethernet1", 20, "failure"),
    ]
    expected = [
        FreshnessState.HEALTHY,
        FreshnessState.STALE,
        FreshnessState.POLLING_FAILED,
        FreshnessState.POLLING_FAILED,
    ]

    for vendor, device_id, interface, age, outcome in identities:
        if outcome == "success":
            store.put(
                vendor=vendor,
                device_id=device_id,
                interface=interface,
                timestamp=_iso(age),
                fetched_at=_iso(age),
                source=vendor,
                telemetry={"cpu_percent": 10.0},
                diagnosis={"severity": "normal"},
            )
        else:
            store.put(
                vendor=vendor,
                device_id=device_id,
                interface=interface,
                timestamp=_iso(age),
                fetched_at=_iso(age),
                source=vendor,
                telemetry={"cpu_percent": 10.0},
                diagnosis={"severity": "normal"},
            )
            store.put(
                vendor=vendor,
                device_id=device_id,
                interface=interface,
                timestamp=_iso(1),
                fetched_at=_iso(1),
                source=vendor,
                error="Poll failed.",
            )

    assessments = [
        assess_freshness(reading, now=NOW) for reading in store.get_all()
    ]

    assert [assessment.state for assessment in assessments] == expected
    assert [assessment.age_seconds for assessment in assessments] == [
        10.0,
        600.0,
        60.0,
        20.0,
    ]


# -------------------------------------------------------------------------- #
# Timestamp parsing and age
# -------------------------------------------------------------------------- #


def test_timestamp_parsing_accepts_iso8601_forms():
    assert parse_iso_timestamp("2026-01-01T12:00:00Z") == NOW
    assert parse_iso_timestamp("2026-01-01T13:00:00+01:00") == NOW
    assert parse_iso_timestamp("2026-01-01T12:00:00") == NOW
    assert parse_iso_timestamp(" 2026-01-01T12:00:00+00:00 ") == NOW


def test_timestamp_parsing_rejects_invalid_values():
    assert parse_iso_timestamp(None) is None
    assert parse_iso_timestamp("") is None
    assert parse_iso_timestamp("   ") is None
    assert parse_iso_timestamp(12345) is None
    assert parse_iso_timestamp("not-a-timestamp") is None


def test_compute_age_seconds_uses_injected_clock_and_never_substitutes_now():
    assert compute_age_seconds(_iso(45), now=NOW) == 45.0
    assert compute_age_seconds("", now=NOW) is None
    assert compute_age_seconds("garbage", now=NOW) is None


# -------------------------------------------------------------------------- #
# Threshold configuration
# -------------------------------------------------------------------------- #


def test_default_threshold_is_300_seconds(monkeypatch):
    monkeypatch.delenv(STALE_THRESHOLD_ENV_VAR, raising=False)

    assert read_stale_threshold_seconds() == 300.0
    assert DEFAULT_STALE_THRESHOLD_SECONDS == 300.0


def test_assessment_uses_default_threshold_when_not_supplied(monkeypatch):
    monkeypatch.delenv(STALE_THRESHOLD_ENV_VAR, raising=False)

    assessment = assess_freshness(
        _reading(success_seconds_ago=299),
        now=NOW,
    )

    assert assessment.state is FreshnessState.HEALTHY
    assert assessment.threshold_seconds == DEFAULT_STALE_THRESHOLD_SECONDS


def test_valid_threshold_override(monkeypatch):
    monkeypatch.setenv(STALE_THRESHOLD_ENV_VAR, "600")

    assert read_stale_threshold_seconds() == 600.0
    assert read_stale_threshold_seconds({"NETOPS_STALE_THRESHOLD_SECONDS": "12.5"}) == 12.5


@pytest.mark.parametrize(
    "raw",
    ["0", "-1", "not-a-number", "nan", "inf", "-inf", ""],
)
def test_invalid_threshold_values_fall_back_to_default(raw):
    assert read_stale_threshold_seconds({STALE_THRESHOLD_ENV_VAR: raw}) == (
        DEFAULT_STALE_THRESHOLD_SECONDS
    )


def test_assess_freshness_rejects_invalid_caller_threshold():
    assessment = assess_freshness(
        _reading(success_seconds_ago=10),
        now=NOW,
        threshold_seconds=0,
    )

    assert assessment.threshold_seconds == DEFAULT_STALE_THRESHOLD_SECONDS
    assert assessment.state is FreshnessState.HEALTHY
