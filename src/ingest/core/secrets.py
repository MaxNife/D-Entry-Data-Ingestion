"""Encryption at rest for connection passwords (Fernet, AES-128-CBC + HMAC).

The key comes from ``INGEST_SECRET_KEY`` (a urlsafe-base64 Fernet key). Without it, a
key is generated once into ``data/.secret_key`` next to the uploads, readable only by
the current user where the OS allows. Losing the key means re-entering passwords;
the data itself is unaffected.
"""

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ingest.config import settings

_cached: Fernet | None = None


def _key_file() -> Path:
    return Path(settings.upload_dir).parent / ".secret_key"


def _fernet() -> Fernet:
    global _cached
    if _cached is not None:
        return _cached
    key = os.environ.get("INGEST_SECRET_KEY", "").strip().encode()
    if not key:
        path = _key_file()
        if path.exists():
            key = path.read_bytes().strip()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fp:
                fp.write(key)
    _cached = Fernet(key)
    return _cached


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError(
            "stored password cannot be decrypted (the secret key changed); re-enter it"
        ) from e
