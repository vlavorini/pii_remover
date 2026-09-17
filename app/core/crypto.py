"""Encryption at rest for uploads and result artifacts (Fernet / AES-128-CBC+HMAC).

The key is mandatory whenever ENCRYPT_ARTIFACTS is on: the app refuses to write
plaintext PII to disk. Keys live in .env / docker secrets and are never logged.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

_ENC_SUFFIX = ".enc"


class CryptoError(RuntimeError):
    pass


class ArtifactCipher:
    """Encrypt/decrypt bytes and JSON blobs on disk."""

    def __init__(self, key: str | None, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._fernet: Fernet | None = None
        if not enabled:
            return
        if not key:
            # Fail closed with a loud, actionable error. Silently degrading to
            # plaintext would let PII reach the disk unencrypted, and the
            # resulting files are unreadable anyway (wrong key later).
            raise CryptoError(
                "ENCRYPTION_KEY is not set but ENCRYPT_ARTIFACTS=true. "
                "Generate one with: python -m app.core.crypto"
            )
        try:
            self._fernet = Fernet(key.encode("utf-8") if isinstance(key, str) else key)
        except Exception as exc:  # noqa: BLE001
            raise CryptoError(f"ENCRYPTION_KEY is not a valid Fernet key: {exc}") from exc

    # ------------------------------------------------------------------ bytes
    def encrypt_bytes(self, data: bytes) -> bytes:
        if not self.enabled:
            return data
        assert self._fernet is not None
        return self._fernet.encrypt(data)

    def decrypt_bytes(self, blob: bytes) -> bytes:
        if not self.enabled:
            return blob
        assert self._fernet is not None
        try:
            return self._fernet.decrypt(blob)
        except InvalidToken as exc:
            raise CryptoError("ciphertext does not match ENCRYPTION_KEY") from exc

    # ------------------------------------------------------------------- json
    def dump_json(self, path: Path, payload: dict[str, Any]) -> Path:
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        return self.write_file(path, raw)

    def load_json(self, path: Path) -> dict[str, Any]:
        return json.loads(self.read_file(path).decode("utf-8"))

    # ------------------------------------------------------------------ files
    def write_file(self, path: Path, data: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        target = Path(str(path) + _ENC_SUFFIX) if self.enabled else path
        target.write_bytes(self.encrypt_bytes(data))
        try:
            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:  # pragma: no cover - best effort on odd filesystems
            pass
        return target

    def read_file(self, path: Path) -> bytes:
        candidate = Path(str(path) + _ENC_SUFFIX)
        if candidate.exists():
            return self.decrypt_bytes(candidate.read_bytes())
        if path.exists():
            return self.decrypt_bytes(path.read_bytes())
        raise FileNotFoundError(str(path))


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


def key_from_passphrase(passphrase: str) -> str:
    """Derive a stable Fernet key from a passphrase (PBKDF2-HMAC-SHA256)."""
    import hashlib

    digest = hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), b"pii-remover-artifact-key", 200_000, dklen=32
    )
    return base64.urlsafe_b64encode(digest).decode("ascii")


if __name__ == "__main__":  # pragma: no cover - operator utility
    print(generate_key())
