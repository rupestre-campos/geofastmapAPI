"""Password hashing and verification.

Passwords are stored as bcrypt(salt, secret) where secret is either:
- The client-side SHA-256 hash (64 hex chars) when sent from the web UI, or
- SHA-256(plaintext) when the backend receives plaintext (e.g. HTTP Basic Auth).
So we always bcrypt a 64-char hex secret; verification compares the same.
"""

import hashlib
import re
from passlib.hash import bcrypt

# Client sends SHA-256 hash as 64 lowercase hex chars; backend may receive that or plaintext
HEX_64 = re.compile(r"^[a-f0-9]{64}$")


def _secret(value: str) -> str:
    """Normalize to the value we bcrypt: 64-char hex (SHA-256 of password or client hash)."""
    if not value:
        return value
    if HEX_64.match(value.strip().lower()):
        return value.strip().lower()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    """Hash for storage: bcrypt(secret). Secret is SHA-256 hash or derived from plaintext."""
    return bcrypt.hash(_secret(password))


def verify_password(password: str, password_hash: str) -> bool:
    """Verify: compare bcrypt(secret) with stored hash."""
    return bcrypt.verify(_secret(password), password_hash)
