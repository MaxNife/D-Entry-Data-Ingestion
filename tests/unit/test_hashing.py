"""Unit tests for core.hashing (spec s3: streaming SHA-256)."""

import hashlib
import os
import tempfile

import pytest

pytestmark = pytest.mark.unit


def test_sha256_file_matches_stdlib(tmp_path):
    from ingest.core.hashing import sha256_file

    content = b"hello world\n" * 1000
    f = tmp_path / "test.bin"
    f.write_bytes(content)

    expected = hashlib.sha256(content).hexdigest()
    assert sha256_file(f) == expected


def test_sha256_bytes_matches_stdlib():
    from ingest.core.hashing import sha256_bytes

    data = b"test data 12345"
    expected = hashlib.sha256(data).hexdigest()
    assert sha256_bytes(data) == expected


def test_sha256_file_empty_file(tmp_path):
    from ingest.core.hashing import sha256_file

    f = tmp_path / "empty.bin"
    f.write_bytes(b"")
    expected = hashlib.sha256(b"").hexdigest()
    assert sha256_file(f) == expected


def test_sha256_file_large_file(tmp_path):
    """Ensure streaming works for files larger than the buffer size."""
    from ingest.core.hashing import sha256_file

    # Write 5MB (larger than typical 1MB buffer)
    content = os.urandom(5 * 1024 * 1024)
    f = tmp_path / "large.bin"
    f.write_bytes(content)

    expected = hashlib.sha256(content).hexdigest()
    assert sha256_file(f) == expected


def test_sha256_deterministic(tmp_path):
    from ingest.core.hashing import sha256_file

    content = b"deterministic content"
    f = tmp_path / "det.bin"
    f.write_bytes(content)

    assert sha256_file(f) == sha256_file(f)

