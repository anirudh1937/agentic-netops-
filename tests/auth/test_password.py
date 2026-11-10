"""Tests for password hashing and verification using Argon2id.

Milestone 8.2.4.2 — Password hashing and verification only.
Authentication endpoints, sessions, and JWT are implemented in later milestones.

These tests run without PostgreSQL, Docker, network access, or external services.
"""

from __future__ import annotations

import pytest

from app.auth.password import hash_password, verify_password


def test_hash_returns_non_empty_string() -> None:
    """Hashing a password returns a non-empty string."""
    h = hash_password("secret-password")
    assert isinstance(h, str)
    assert len(h) > 0


def test_hash_identifies_argon2id() -> None:
    """The encoded hash identifies the Argon2id algorithm."""
    h = hash_password("secret-password")
    assert h.startswith("$argon2id$")


def test_correct_password_verifies() -> None:
    """A correct password verifies successfully."""
    h = hash_password("my-secret-password")
    assert verify_password("my-secret-password", h) is True


def test_incorrect_password_returns_false() -> None:
    """An incorrect password returns False."""
    h = hash_password("my-secret-password")
    assert verify_password("wrong-password", h) is False


def test_two_hashes_of_same_password_are_different() -> None:
    """Two hashes of the same password differ due to unique salts."""
    h1 = hash_password("same-password")
    h2 = hash_password("same-password")
    assert h1 != h2


def test_both_hashes_verify() -> None:
    """Both independently generated hashes verify against the original password."""
    h1 = hash_password("another-password")
    h2 = hash_password("another-password")
    assert verify_password("another-password", h1) is True
    assert verify_password("another-password", h2) is True


def test_empty_password_hashes_and_verifies() -> None:
    """An empty password can be hashed and verified (edge case)."""
    h = hash_password("")
    assert isinstance(h, str)
    assert len(h) > 0
    assert h.startswith("$argon2id$")
    assert verify_password("", h) is True


def test_malformed_hash_returns_false() -> None:
    """A malformed hash returns False instead of raising."""
    assert verify_password("any-password", "not-a-valid-hash") is False


def test_unsupported_hash_format_returns_false() -> None:
    """An unsupported hash format returns False instead of raising."""
    assert verify_password("any-password", "$bcrypt$2a$12$somehash") is False


def test_empty_hash_returns_false() -> None:
    """An empty hash string returns False."""
    assert verify_password("any-password", "") is False


def test_none_hash_returns_false() -> None:
    """A None hash returns False instead of raising."""
    assert verify_password("any-password", None) is False


def test_password_must_be_string() -> None:
    """Passing a non-string password raises TypeError."""
    with pytest.raises(TypeError):
        hash_password(12345)  # type: ignore[arg-type]


def test_hash_password_must_be_string() -> None:
    """Passing a non-string hash raises TypeError."""
    with pytest.raises(TypeError):
        hash_password(12345)  # type: ignore[arg-type]
