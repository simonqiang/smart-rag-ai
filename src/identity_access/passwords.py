"""Argon2id password hashing (Task 6a).

Spec requirement: passwords are stored as Argon2id hashes only. The hasher
generates a fresh salt per call and exposes verification that never raises
on malformed stored hashes.
"""

from __future__ import annotations

from argon2 import PasswordHasher as _Argon2Hasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = _Argon2Hasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, digest: str) -> bool:
    try:
        return _hasher.verify(digest, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
