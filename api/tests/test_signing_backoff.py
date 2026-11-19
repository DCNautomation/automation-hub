"""Pure functions: no database needed."""
import pytest

from app import jobs, signing

SECRET = "test-secret-0123456789"
BODY = b'{"order": 42}'


STAMP = "1800000000"
KEY = "evt-1"


def test_sign_then_verify_roundtrip():
    sig = signing.sign(SECRET, BODY, STAMP, KEY)
    assert sig.startswith("sha256=")
    assert signing.verify(SECRET, BODY, sig, STAMP, KEY)


def test_verify_rejects_wrong_secret_and_tampered_body():
    sig = signing.sign(SECRET, BODY, STAMP, KEY)
    assert not signing.verify("another-secret-0123456", BODY, sig, STAMP, KEY)
    assert not signing.verify(SECRET, b'{"order": 43}', sig, STAMP, KEY)


def test_signature_covers_the_timestamp_and_the_key():
    sig = signing.sign(SECRET, BODY, STAMP, KEY)
    assert not signing.verify(SECRET, BODY, sig, "1800000001", KEY)
    assert not signing.verify(SECRET, BODY, sig, STAMP, "evt-2")


def test_fields_cannot_be_shifted_into_each_other():
    # Newline is the separator and cannot appear in a header value, so these must differ.
    assert signing.sign(SECRET, b"2", STAMP, "1") != signing.sign(SECRET, b"12", STAMP, "")


@pytest.mark.parametrize("header", [None, "", "sha256=", "sha256=zzzz", "md5=abc", "garbage"])
def test_verify_rejects_missing_or_malformed_header(header):
    assert not signing.verify(SECRET, BODY, header, STAMP, KEY)


@pytest.mark.parametrize(
    "value, expected",
    [("1800000000", 1800000000), ("0", 0), (None, None), ("", None), ("-1", None), ("1.5", None),
     (" 17", None), ("١", None), ("9" * 40, None)],
)
def test_parse_timestamp_accepts_only_plain_ascii_digits(value, expected):
    assert signing.parse_timestamp(value) == expected


def test_backoff_ceiling_doubles_each_attempt_then_caps():
    ceiling = lambda attempt: jobs.backoff_seconds(attempt, base=10, cap=100, rand=lambda: 1.0)
    assert [ceiling(n) for n in (1, 2, 3, 4)] == [10, 20, 40, 80]
    assert ceiling(5) == 100
    assert ceiling(30) == 100


def test_backoff_jitter_stays_between_half_and_full_ceiling():
    low = jobs.backoff_seconds(3, base=10, cap=3600, rand=lambda: 0.0)
    high = jobs.backoff_seconds(3, base=10, cap=3600, rand=lambda: 1.0)
    assert (low, high) == (20, 40)
    for _ in range(200):
        assert 20 <= jobs.backoff_seconds(3, base=10, cap=3600) <= 40
