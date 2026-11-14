"""CredentialCipher: round trip, wrong key, rotation, env handling."""
import logging

import pytest

from database.credentials import (
    CredentialCipher,
    CredentialDecryptError,
    SecretKeyError,
    cipher_from_env,
    generate_key,
)

SECRET = {"password": "p@ss-W0rd-ZZ-719"}


def test_round_trip_and_ciphertext_hides_plaintext():
    cipher = CredentialCipher(generate_key())
    token = cipher.encrypt(SECRET)
    assert "p@ss-W0rd-ZZ-719" not in token
    assert cipher.decrypt(token) == SECRET


def test_wrong_key_fails_cleanly():
    token = CredentialCipher(generate_key()).encrypt(SECRET)
    other_key = generate_key()
    with pytest.raises(CredentialDecryptError) as err:
        CredentialCipher(other_key).decrypt(token)
    text = str(err.value)
    assert other_key not in text and token not in text and "p@ss" not in text


def test_garbage_token_fails_cleanly():
    with pytest.raises(CredentialDecryptError):
        CredentialCipher(generate_key()).decrypt("not-a-token")


def test_rotation_with_key_list():
    old, new = generate_key(), generate_key()
    token = CredentialCipher(old).encrypt(SECRET)
    rotating = CredentialCipher(f"{new}, {old}")
    assert rotating.key_count == 2
    assert rotating.decrypt(token) == SECRET          # old data still readable
    rotated = rotating.rotate(token)
    assert CredentialCipher(new).decrypt(rotated) == SECRET   # now under the new key
    with pytest.raises(CredentialDecryptError):
        CredentialCipher(old).decrypt(rotated)
    # new writes use the primary (first) key
    assert CredentialCipher(new).decrypt(rotating.encrypt(SECRET)) == SECRET


def test_invalid_key_error_does_not_echo_key():
    with pytest.raises(SecretKeyError) as err:
        CredentialCipher("definitely-not-a-fernet-key")
    assert "definitely-not-a-fernet-key" not in str(err.value)
    with pytest.raises(SecretKeyError):
        CredentialCipher(" , ")


def test_repr_has_no_key_material():
    key = generate_key()
    assert key not in repr(CredentialCipher(key))


def test_env_key_is_used(monkeypatch):
    key = generate_key()
    monkeypatch.setenv("T2S_SECRET_KEY", key)
    cipher = cipher_from_env(persistent=True)
    assert not cipher.ephemeral
    assert CredentialCipher(key).decrypt(cipher.encrypt(SECRET)) == SECRET


def test_missing_key_persistent_store_is_a_configuration_error(monkeypatch):
    monkeypatch.delenv("T2S_SECRET_KEY", raising=False)
    with pytest.raises(SecretKeyError, match="T2S_SECRET_KEY"):
        cipher_from_env(persistent=True)


def test_missing_key_in_memory_store_gets_ephemeral_key_with_warning(monkeypatch, caplog):
    monkeypatch.delenv("T2S_SECRET_KEY", raising=False)
    with caplog.at_level(logging.WARNING, logger="database.credentials"):
        cipher = cipher_from_env(persistent=False)
    assert cipher.ephemeral
    assert any("ephemeral" in r.getMessage() for r in caplog.records)
    assert cipher.decrypt(cipher.encrypt(SECRET)) == SECRET
