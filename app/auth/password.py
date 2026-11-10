"""Password hashing and verification using Argon2id.

Milestone 8.2.4.2 — Password hashing and verification only.

This module provides secure password hashing using the Argon2id algorithm
through the argon2-cffi library.

Authentication endpoints, sessions, JWT, and organization membership
authorization are implemented in later milestones.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHash

# A single PasswordHasher instance with default parameters.
# Argon2id is the recommended winner of the Password Hashing Competition and
# provides resistance to both GPU and side-channel attacks.
# The default parameters (time_cost=3, memory_cost=65536, parallelism=4)
# are appropriate for general-purpose password hashing.
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash a plaintext password using Argon2id.

    Args:
        password: The plaintext password to hash.

    Returns:
        A salted, encoded Argon2id hash string suitable for storage.

    Raises:
        TypeError: If *password* is not a string.
    """
    if not isinstance(password, str):
        raise TypeError("password must be a string")
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against an Argon2id hash.

    Args:
        password: The plaintext password to verify.
        password_hash: The encoded hash produced by ``hash_password``.

    Returns:
        True if the password matches the hash; False otherwise.

    Raises:
        TypeError: If *password* is not a string.
    """
    if not isinstance(password, str):
        raise TypeError("password must be a string")
    if not isinstance(password_hash, str):
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHash):
        return False
