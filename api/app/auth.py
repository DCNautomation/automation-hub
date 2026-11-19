"""Login, lockout and bearer-token sessions.

Lockout and throttling live in Postgres (login_attempts), so they survive restarts and are shared
by every API process. Two limits apply:
  * per (username, ip): LOGIN_MAX_FAILURES failures inside the window locks that pair only, so one
    address can never lock someone else out of their own account;
  * per ip: LOGIN_IP_MAX_FAILURES failures across any usernames, which stops username spraying.
A success clears the (username, ip) count. Unknown usernames are counted exactly like real ones, and
take the same time to answer, so neither the response nor the timing says which accounts exist.
While locked, attempts are refused without checking the password and are not recorded, so a lockout
cannot be extended or bypassed by guessing during it.

Logins from one ip are serialised with a Postgres advisory lock. Without it, a burst of parallel
guesses would all read "0 failures" before any of them was recorded and sail past the limit.
"""
import hashlib
import secrets
from typing import NamedTuple

from app import audit, config, db, passwords, users


class InvalidCredentials(Exception):
    pass


class LoginLocked(Exception):
    def __init__(self, retry_after):
        super().__init__(f"locked for {retry_after}s")
        self.retry_after = retry_after


class Principal(NamedTuple):
    user_id: int
    username: str
    role: str


class Session(NamedTuple):
    token: str
    expires_at: object
    principal: Principal


def _hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# Seconds until each of the newest `limit` failures leaves the window, newest first. The lock lifts
# when the oldest of them does, so retry-after is the last value once `limit` failures exist.
_AGE_OUT = (
    "SELECT GREATEST(1, CEIL(EXTRACT(EPOCH FROM (attempted_at + make_interval(secs => %s) - now())))) AS secs "
    "FROM login_attempts WHERE NOT succeeded AND attempted_at > now() - make_interval(secs => %s) "
)


def _locked_seconds(conn, name, ip):
    """Seconds left on a lockout for this attempt, or 0 when it may proceed."""
    window = config.LOGIN_WINDOW_SECONDS
    pair = conn.execute(
        _AGE_OUT + "AND username = %s AND ip = %s "
        "AND attempted_at > COALESCE((SELECT max(attempted_at) FROM login_attempts "
        "                              WHERE username = %s AND ip = %s AND succeeded), '-infinity') "
        "ORDER BY attempted_at DESC LIMIT %s",
        (window, window, name, ip, name, ip, config.LOGIN_MAX_FAILURES),
    ).fetchall()
    if len(pair) >= config.LOGIN_MAX_FAILURES:
        return int(pair[-1]["secs"])
    by_ip = conn.execute(
        _AGE_OUT + "AND ip = %s ORDER BY attempted_at DESC LIMIT %s",
        (window, window, ip, config.LOGIN_IP_MAX_FAILURES),
    ).fetchall()
    if len(by_ip) >= config.LOGIN_IP_MAX_FAILURES:
        return int(by_ip[-1]["secs"])
    return 0


def login(username, password, ip):
    name = users.normalize_username(username)
    with db.connection() as conn:
        got_lock = conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS ok", (f"login:{ip}",)
        ).fetchone()["ok"]
        if not got_lock:
            # Another login from this address is mid-check. Refusing beats queueing: it keeps one
            # address from tying up the connection pool, and it is what makes the limit exact.
            raise LoginLocked(1)
        remaining = _locked_seconds(conn, name, ip)
        if remaining:
            raise LoginLocked(remaining)

        user = conn.execute(
            "SELECT id, username, role, password_hash FROM users WHERE username = %s", (name,)
        ).fetchone()
        matches = passwords.verify_password(password, user["password_hash"] if user else passwords.dummy_hash())
        ok = bool(user) and matches
        conn.execute(
            "INSERT INTO login_attempts (username, ip, succeeded) VALUES (%s, %s, %s)", (name, ip, ok)
        )
        if not ok:
            # Keep the failure record even though the caller sees an exception.
            conn.commit()
            raise InvalidCredentials()

        token = secrets.token_urlsafe(32)
        expires_at = conn.execute(
            "INSERT INTO sessions (user_id, token_hash, expires_at) "
            "VALUES (%s, %s, now() + make_interval(secs => %s)) RETURNING expires_at",
            (user["id"], _hash_token(token), config.SESSION_TTL_SECONDS),
        ).fetchone()["expires_at"]
        conn.execute("DELETE FROM sessions WHERE user_id = %s AND expires_at < now()", (user["id"],))
        audit.record(conn, f"user:{name}", "auth.login", "user", user["id"], after={"ip": ip})
    return Session(token, expires_at, Principal(user["id"], user["username"], user["role"]))


def authenticate(token):
    """The Principal behind a bearer token, or None when it is unknown, expired or logged out."""
    if not token:
        return None
    with db.connection() as conn:
        row = conn.execute(
            "SELECT u.id, u.username, u.role FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = %s AND s.expires_at > now()",
            (_hash_token(token),),
        ).fetchone()
    return Principal(row["id"], row["username"], row["role"]) if row else None


def logout(token):
    with db.connection() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = %s", (_hash_token(token),))
