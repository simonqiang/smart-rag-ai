"""Task 24a: backup envelope parsing and serialization edge cases."""

import hashlib
import json
import os
import struct

import pytest

from operations import backup


def test_round_trip_decrypts_verified_bundle(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(backup.KEY_ENV, "round trip key")
    header = {
        "format": backup.BUNDLE_FORMAT,
        "format_version": backup.FORMAT_VERSION,
        "created_at": "2026-10-21T00:00:00+00:00",
        "schema_revision": "0014",
        "kdf_salt": os.urandom(16).hex(),
        "nonce": os.urandom(12).hex(),
        "bundle_sha256": hashlib.sha256(b"payload").hexdigest(),
        "counts": {},
        "source_ids": [],
    }
    path = tmp_path / "round.srb"
    path.write_bytes(backup._encrypt(b"payload", header))

    parsed, bundle = backup.load_bundle(path, "round trip key")
    assert bundle == b"payload"
    assert parsed["schema_revision"] == "0014"


def _envelope(header: dict, ciphertext: bytes = b"x") -> bytes:
    header_bytes = json.dumps(header, ensure_ascii=False, sort_keys=True).encode()
    return backup.MAGIC + struct.pack(">I", len(header_bytes)) + header_bytes + ciphertext


def _header() -> dict:
    return {
        "format": backup.BUNDLE_FORMAT,
        "format_version": backup.FORMAT_VERSION,
        "created_at": "2026-10-21T00:00:00+00:00",
        "schema_revision": "0014",
        "kdf_salt": "ab" * 16,
        "nonce": "cd" * 12,
        "bundle_sha256": "0" * 64,
        "counts": {"users": 1},
        "source_ids": [],
    }


def test_rejects_bad_magic(tmp_path) -> None:
    path = tmp_path / "nope.srb"
    path.write_bytes(b"NOPE" + b"\x00" * 32)
    with pytest.raises(backup.BackupCorrupt, match="not a smart-rag backup"):
        backup.inspect_backup(path)


def test_rejects_truncated_header(tmp_path) -> None:
    path = tmp_path / "short.srb"
    path.write_bytes(backup.MAGIC + struct.pack(">I", 400) + b'{"a": 1}')
    with pytest.raises(backup.BackupCorrupt, match="truncated"):
        backup.inspect_backup(path)


def test_rejects_unreadable_header(tmp_path) -> None:
    path = tmp_path / "junk.srb"
    path.write_bytes(backup.MAGIC + struct.pack(">I", 4) + b"!!!!")
    with pytest.raises(backup.BackupCorrupt, match="header"):
        backup.inspect_backup(path)


def test_rejects_unknown_format_version(tmp_path) -> None:
    header = _header() | {"format_version": 99}
    path = tmp_path / "future.srb"
    path.write_bytes(_envelope(header))
    with pytest.raises(backup.BackupCorrupt, match="format version"):
        backup.inspect_backup(path)


def test_decrypt_rejects_checksum_mismatch(tmp_path) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    header = _header() | {"bundle_sha256": "1" * 64}  # baked in before encrypting
    header_bytes = json.dumps(header, ensure_ascii=False, sort_keys=True).encode()
    key = backup._derive_key("any key", bytes.fromhex(header["kdf_salt"]))
    ciphertext = AESGCM(key).encrypt(
        bytes.fromhex(header["nonce"]), b"the real bundle", associated_data=header_bytes
    )
    path = tmp_path / "swapped.srb"
    path.write_bytes(
        backup.MAGIC + struct.pack(">I", len(header_bytes)) + header_bytes + ciphertext
    )
    with pytest.raises(backup.BackupCorrupt, match="checksum mismatch"):
        backup.load_bundle(path, "any key")


def test_serialize_refuses_unsupported_types() -> None:
    with pytest.raises(ValueError, match="unsupported column type"):
        backup._serialize(object())


def _zip_payload(manifest: dict, members: dict[str, bytes] | None = None) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, data in (members or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_verify_bundle_refuses_foreign_payload() -> None:
    bundle = _zip_payload({"format": "something-else", "format_version": 1, "members": {}})
    with pytest.raises(backup.BackupCorrupt, match="not a smart-rag bundle"):
        backup._verify_bundle(bundle)


def test_verify_bundle_refuses_checksum_mismatch() -> None:
    manifest = {
        "format": backup.BUNDLE_FORMAT,
        "format_version": backup.FORMAT_VERSION,
        "members": {"tables/users.jsonl": "0" * 64},
    }
    bundle = _zip_payload(manifest, {"tables/users.jsonl": b"rows"})
    with pytest.raises(backup.BackupCorrupt, match="failed its checksum"):
        backup._verify_bundle(bundle)
