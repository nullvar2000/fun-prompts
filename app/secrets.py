"""At-rest encryption for provider API keys.

Keys are encrypted with a Fernet key stored outside the database (under the
data dir by default, mode 0600), so a copied ``.db`` file is not a leaked
credential bundle. The admin UI only ever shows a masked form of the value,
and a masked value coming back through a form means "leave it alone".
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

MASK_MARKER = "…"
MASK_PREFIX_CHARS = 3
MASK_SUFFIX_CHARS = 4


class KeyRing:
    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def load_or_create(cls, key_file: Path) -> KeyRing:
        path = Path(key_file)
        if path.is_file():
            return cls(path.read_bytes())
        path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        path.chmod(0o600)
        return cls(key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        if not token:
            return ""
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError(
                "stored API key could not be decrypted — the key file no longer "
                "matches this database"
            ) from exc


def mask(plaintext: str) -> str:
    """A display-only form: enough to recognise a key, not enough to use it."""
    if not plaintext:
        return ""
    if len(plaintext) <= MASK_PREFIX_CHARS + MASK_SUFFIX_CHARS:
        return MASK_MARKER * len(plaintext)
    return f"{plaintext[:MASK_PREFIX_CHARS]}{MASK_MARKER}{plaintext[-MASK_SUFFIX_CHARS:]}"


def looks_masked(value: str) -> bool:
    return MASK_MARKER in value
