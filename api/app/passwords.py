"""Password hashing with scrypt from the standard library (no extra dependency).

Stored form: `scrypt$<N>$<r>$<p>$<salt b64>$<hash b64>`. The cost parameters travel with the hash,
so they can be raised later without breaking existing logins.
"""
import base64
import hashlib
import hmac
import secrets
from functools import cache

SCRYPT_N = 2**15  # 32 MB and roughly 100 ms per hash; tests lower this to stay fast
SCRYPT_R = 8
SCRYPT_P = 1
DKLEN = 32


def _scrypt(password, salt, n, r, p):
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=DKLEN, maxmem=256 * n * r * p + (1 << 20)
    )


def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password, stored):
    """True only for a well-formed hash that matches. Anything malformed is simply False."""
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = _scrypt(password, base64.b64decode(salt), int(n), int(r), int(p))
        return hmac.compare_digest(digest, base64.b64decode(expected))
    except (ValueError, TypeError, AttributeError):
        return False


@cache
def dummy_hash():
    """A real hash nobody knows the password to, so an unknown username costs the same time as a wrong password."""
    return hash_password(secrets.token_urlsafe(16))
