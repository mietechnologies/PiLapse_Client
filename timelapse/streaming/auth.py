"""
HTTP Basic Auth check shared by the preview servers.

The password is never kept in memory in plaintext: callers hash it once with
`hash_password()` and pass the hex digest to `check_basic_auth()`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def check_basic_auth(header: str, username: str, password_hash: str) -> bool:
    """Return True if `header` carries valid Basic credentials."""
    if not password_hash or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        return False
    user, sep, password = decoded.partition(":")
    if not sep:
        return False
    # Compare both fields in constant time so neither leaks via timing.
    user_ok = hmac.compare_digest(user.encode(), username.encode())
    pw_ok = hmac.compare_digest(hash_password(password), password_hash)
    return user_ok and pw_ok
