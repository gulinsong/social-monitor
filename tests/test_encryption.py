"""Tests for cookie encryption in core.base_monitor"""
from core.base_monitor import encrypt_cookie, decrypt_cookie


def test_encrypt_decrypt_roundtrip():
    original = "session=abc123; token=xyz789"
    encrypted = encrypt_cookie(original)
    assert encrypted != original
    decrypted = decrypt_cookie(encrypted)
    assert decrypted == original


def test_encrypt_produces_base64():
    original = "test_cookie_value"
    encrypted = encrypt_cookie(original)
    # base64 strings only contain these characters
    assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=" for c in encrypted)


def test_encrypt_empty_string():
    encrypted = encrypt_cookie("")
    decrypted = decrypt_cookie(encrypted)
    assert decrypted == ""


def test_encrypt_unicode():
    original = "用户=test; 会话=abc"
    encrypted = encrypt_cookie(original)
    decrypted = decrypt_cookie(encrypted)
    assert decrypted == original


def test_different_inputs_different_outputs():
    e1 = encrypt_cookie("cookie1")
    e2 = encrypt_cookie("cookie2")
    assert e1 != e2
