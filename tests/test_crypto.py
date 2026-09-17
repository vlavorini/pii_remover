"""Encryption at rest and pseudonymisation."""
import base64

import pytest

from app.core.crypto import ArtifactCipher, CryptoError, generate_key, key_from_passphrase
from app.core.hashing import pseudonym


def test_generate_key_is_valid_fernet_key():
    key = generate_key()
    assert len(base64.urlsafe_b64decode(key)) == 32
    ArtifactCipher(key)


def test_passphrase_derivation_is_stable():
    assert key_from_passphrase("hunter2") == key_from_passphrase("hunter2")
    assert key_from_passphrase("hunter2") != key_from_passphrase("hunter3")


def test_cipher_roundtrip_bytes():
    cipher = ArtifactCipher(generate_key())
    blob = cipher.encrypt_bytes(b"personal data")
    assert blob != b"personal data"
    assert cipher.decrypt_bytes(blob) == b"personal data"


def test_write_file_is_encrypted_and_0600(tmp_path):
    import os
    import stat

    cipher = ArtifactCipher(generate_key())
    target = tmp_path / "artifact.txt"
    written = cipher.write_file(target, b"secret")
    assert written.suffix == ".enc"
    on_disk = written.read_bytes()
    assert b"secret" not in on_disk
    assert stat.S_IMODE(os.stat(written).st_mode) == 0o600
    assert cipher.read_file(target) == b"secret"


def test_json_roundtrip(tmp_path):
    cipher = ArtifactCipher(generate_key())
    payload = {"job_id": "job_1", "masked": 3}
    cipher.dump_json(tmp_path / "r.json", payload)
    assert cipher.load_json(tmp_path / "r.json") == payload


def test_missing_key_is_refused():
    with pytest.raises(CryptoError):
        ArtifactCipher("", enabled=True)


def test_wrong_key_cannot_decrypt():
    a = ArtifactCipher(generate_key())
    b = ArtifactCipher(generate_key())
    with pytest.raises(CryptoError):
        b.decrypt_bytes(a.encrypt_bytes(b"x"))


def test_pseudonym_is_deterministic_and_not_reversible():
    salt = b"unit-test-salt"
    first = pseudonym("Mario Rossi", "PERSON", salt=salt)
    second = pseudonym("Mario Rossi", "PERSON", salt=salt)
    other = pseudonym("Giulia Bianchi", "PERSON", salt=salt)
    assert first == second
    assert first != other
    assert "Mario" not in first and "Rossi" not in first
    assert first.startswith("[PERSON_")


def test_pseudonym_styles():
    salt = b"s"
    assert pseudonym("x", "EMAIL", salt=salt, label_style="redact") == "[REDACTED]"
    assert pseudonym("x", "EMAIL", salt=salt, label_style="hash").startswith("EMAIL_")
