"""HMAC-SHA256 webhook signatures.

The header value is `sha256=<hex digest>` over `<timestamp>\n<idempotency key>\n<raw body>`.
Signing the timestamp and key (not just the body) is what stops a captured request from being
replayed later or resent under a new key. A newline can't appear in a header value, so the
fields can't be shifted into each other.
"""
import hashlib
import hmac
import re

PREFIX = "sha256="
_TIMESTAMP = re.compile(r"[0-9]{1,12}")  # ASCII digits only; 12 digits is far past year 5000


def sign(secret, body, timestamp, key):
    message = f"{timestamp}\n{key}\n".encode("utf-8") + body
    return PREFIX + hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify(secret, body, header, timestamp, key):
    if not header or not header.startswith(PREFIX):
        return False
    # Compare as bytes: compare_digest raises on non-ASCII str, and a bad header must just fail.
    expected = sign(secret, body, timestamp or "", key or "")
    return hmac.compare_digest(expected.encode("utf-8"), header.encode("utf-8"))


def parse_timestamp(value):
    """Unix seconds as an int, or None when the header is missing or not plain ASCII digits."""
    if value is None or not _TIMESTAMP.fullmatch(value):
        return None
    return int(value)
