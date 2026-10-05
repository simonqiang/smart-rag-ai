"""Task 6a: Argon2id password hashing primitives."""

from identity_access.passwords import hash_password, verify_password


def test_hash_is_argon2id_and_verifies() -> None:
    digest = hash_password("correct horse battery staple")
    assert digest.startswith("$argon2id$")
    assert verify_password("correct horse battery staple", digest)


def test_wrong_password_fails_to_verify() -> None:
    digest = hash_password("correct horse battery staple")
    assert not verify_password("wrong password", digest)


def test_hashes_are_salted_per_call() -> None:
    assert hash_password("same input") != hash_password("same input")
