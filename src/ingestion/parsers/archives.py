"""Zip archive safety for OOXML containers (Task 19).

Every OOXML document is a zip; a small malicious file can decompress far past
memory or disk budgets. The owner-confirmed ceiling is 500 MB decompressed per
file (spec §15); a 10,000-entry cap bounds per-entry parsing work on top.
"""

from __future__ import annotations

import zipfile
from io import BytesIO

from ingestion.parsers.basic import ExtractionFailed

MAX_DECOMPRESSED_BYTES = 500 * 1024 * 1024  # owner-confirmed limit (spec §15)
MAX_ENTRIES = 10_000


def validate_zip(data: bytes) -> zipfile.ZipFile:
    """Open ``data`` as a safe zip or raise the matching typed failure."""
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ExtractionFailed("corrupt_archive") from exc
    entries = archive.infolist()
    if len(entries) > MAX_ENTRIES:
        raise ExtractionFailed("archive_bomb")
    total = 0
    for entry in entries:
        if entry.flag_bits & 0x1:
            raise ExtractionFailed("encrypted_archive")
        total += entry.file_size
        if total > MAX_DECOMPRESSED_BYTES:
            raise ExtractionFailed("archive_bomb")
    return archive
