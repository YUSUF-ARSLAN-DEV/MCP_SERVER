"""Encrypts the API keys people save, so a copied database file does not hand them over.

Fernet (AES-128-CBC with an HMAC) from the `cryptography` package. The master key comes from SECRETS_KEY when the
operator sets one (generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`),
and otherwise from a file next to the database that is created on first use (owner-only permissions).

Be clear about what this does and does not protect: a stolen database backup is useless without the key, but anyone who
can read the server's data folder can read the key file too. Set SECRETS_KEY from the environment to keep them apart.
"""
from __future__ import annotations
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

KEY_FILE = "secrets.key"


class SecretBoxError(RuntimeError):
    """A stored secret cannot be read (wrong or lost master key, or the value was altered)."""


class SecretBox:
    def __init__(self, key: bytes):
        self._fernet = Fernet(key)

    def encrypt(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise SecretBoxError("A saved key could not be decrypted: the server's secrets key changed or the data was altered. "
                                 "Enter the key again.") from exc


def load_secretbox(data_dir: Path, configured_key: str = "") -> SecretBox:
    if configured_key.strip():
        try:
            return SecretBox(configured_key.strip().encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise SecretBoxError("SECRETS_KEY is not a valid key (it must be 32 url-safe base64 bytes).") from exc
    path = Path(data_dir) / KEY_FILE
    if path.is_file():
        return SecretBox(path.read_bytes().strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:                       # another worker created it a moment ago
        return SecretBox(path.read_bytes().strip())
    with os.fdopen(fd, "wb") as handle:
        handle.write(key)
    return SecretBox(key)
