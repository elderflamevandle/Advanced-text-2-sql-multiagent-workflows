"""Encryption of stored database credentials (Fernet / MultiFernet).

The key comes from ``T2S_SECRET_KEY``. For rotation, set a comma-separated
list: the first key encrypts, every key is tried for decryption. Generate a key
with ``python -c "from database.credentials import generate_key; print(generate_key())"``.

Nothing in this module logs or raises key material, plaintext or ciphertext.
"""
from __future__ import annotations

import json
import logging
import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

logger = logging.getLogger(__name__)

SECRET_KEY_ENV = "T2S_SECRET_KEY"


class SecretKeyError(RuntimeError):
    """T2S_SECRET_KEY is missing or malformed (a configuration problem)."""


class CredentialDecryptError(RuntimeError):
    """Stored credentials could not be decrypted with the configured key(s)."""


def generate_key() -> str:
    """Return a new url-safe base64 Fernet key."""
    return Fernet.generate_key().decode("ascii")


def _parse_keys(raw: str | list[str] | tuple[str, ...]) -> list[str]:
    parts = raw.split(",") if isinstance(raw, str) else list(raw)
    return [p.strip() for p in parts if p and p.strip()]


class CredentialCipher:
    """Encrypts/decrypts small JSON documents holding connection secrets."""

    def __init__(self, keys: str | list[str] | tuple[str, ...], *, ephemeral: bool = False):
        parsed = _parse_keys(keys)
        if not parsed:
            raise SecretKeyError(f"{SECRET_KEY_ENV} is empty.")
        fernets = []
        for position, key in enumerate(parsed, start=1):
            try:
                fernets.append(Fernet(key.encode("ascii")))
            except Exception:  # noqa: BLE001 - never echo the key itself
                raise SecretKeyError(
                    f"{SECRET_KEY_ENV}: key #{position} is not a valid Fernet key "
                    "(expected 32 url-safe base64-encoded bytes)."
                ) from None
        self._fernet = MultiFernet(fernets)
        self.key_count = len(fernets)
        self.ephemeral = ephemeral

    def __repr__(self) -> str:
        return f"CredentialCipher(keys={self.key_count}, ephemeral={self.ephemeral})"

    def encrypt(self, secrets: dict) -> str:
        payload = json.dumps(secrets, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return self._fernet.encrypt(payload).decode("ascii")

    def decrypt(self, token: str) -> dict:
        try:
            payload = self._fernet.decrypt(token.encode("ascii"))
            data = json.loads(payload.decode("utf-8"))
        except (InvalidToken, ValueError, AttributeError, UnicodeError):
            raise CredentialDecryptError(
                "Stored credentials could not be decrypted. "
                f"Check that {SECRET_KEY_ENV} contains the key they were encrypted with."
            ) from None
        if not isinstance(data, dict):
            raise CredentialDecryptError("Stored credentials are malformed.")
        return data

    def rotate(self, token: str) -> str:
        """Re-encrypt ``token`` with the primary (first) key."""
        try:
            return self._fernet.rotate(token.encode("ascii")).decode("ascii")
        except (InvalidToken, ValueError, AttributeError, UnicodeError):
            raise CredentialDecryptError(
                "Stored credentials could not be re-encrypted: no configured key decrypts them."
            ) from None


def cipher_from_env(*, persistent: bool) -> CredentialCipher:
    """Build the cipher from ``T2S_SECRET_KEY``.

    Without a key: a throwaway key is generated for in-memory metadata stores
    (a warning is logged), and ``SecretKeyError`` is raised for persistent ones,
    because the stored credentials would be unreadable after a restart.
    """
    raw = os.environ.get(SECRET_KEY_ENV, "").strip()
    if raw:
        return CredentialCipher(raw)
    if persistent:
        raise SecretKeyError(
            f"{SECRET_KEY_ENV} is not set, but the metadata store is persistent. "
            "Generate a key with `python -c \"from database.credentials import generate_key; "
            f"print(generate_key())\"` and export it as {SECRET_KEY_ENV}."
        )
    logger.warning(
        "%s is not set: using an ephemeral key for the in-memory metadata store. "
        "Registered connections are lost when the process exits.", SECRET_KEY_ENV,
    )
    return CredentialCipher(generate_key(), ephemeral=True)
