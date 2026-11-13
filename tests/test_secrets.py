"""API keys at rest, and the mask the admin UI shows instead of the value."""

from __future__ import annotations

import os
import stat

import pytest

from app.secrets import KeyRing, looks_masked, mask


def test_key_file_is_created_private_and_round_trips(tmp_path) -> None:
    path = tmp_path / "keys" / "fernet.key"
    ring = KeyRing.load_or_create(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    token = ring.encrypt("sk-abcd1234efgh")
    assert token != "sk-abcd1234efgh"
    assert "sk-abcd1234efgh" not in token
    assert ring.decrypt(token) == "sk-abcd1234efgh"


def test_a_reloaded_ring_still_decrypts_older_tokens(tmp_path) -> None:
    path = tmp_path / "fernet.key"
    token = KeyRing.load_or_create(path).encrypt("secret-value")
    reloaded = KeyRing.load_or_create(path)
    assert reloaded.decrypt(token) == "secret-value"


def test_decrypting_with_the_wrong_key_explains_why(tmp_path) -> None:
    first = KeyRing.load_or_create(tmp_path / "a.key")
    second = KeyRing.load_or_create(tmp_path / "b.key")
    token = first.encrypt("secret")
    with pytest.raises(ValueError, match="key file no longer matches"):
        second.decrypt(token)


def test_empty_keys_stay_empty(tmp_path) -> None:
    ring = KeyRing.load_or_create(tmp_path / "fernet.key")
    assert ring.encrypt("") != ""
    assert ring.decrypt("") == ""


def test_mask_is_recognisable_but_unusable() -> None:
    assert mask("sk-proj-abcdefghij9876") == "sk-…9876"
    assert mask("short") == "…" * 5
    assert mask("") == ""
    assert looks_masked(mask("sk-proj-abcdefghij9876")) is True
    assert looks_masked("sk-proj-abcdefghij9876") is False


def test_key_file_content_is_not_world_readable_even_with_a_permissive_umask(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(os, "umask", lambda _: 0)
    path = tmp_path / "fernet.key"
    KeyRing.load_or_create(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
