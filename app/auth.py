"""Authentication — password hashing and JWT issue/verify.

Deliberately small. Authentication answers *who is calling*; it does not
answer *what they may do* (`app/permissions.py`) or *whose data they may
touch* (`app/authorization.py`). Keeping the three separate is what stops a
token becoming a blanket grant.

## Passwords

PBKDF2-HMAC-SHA256, 240,000 iterations, 16-byte random salt per user, stored
as `pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>`. Verification is
constant-time.

PBKDF2 rather than bcrypt/argon2 purely to avoid a native dependency in this
build; the format carries its own iteration count, so raising the cost later
is a re-hash on next login rather than a migration. For a production
deployment argon2id is the better default.

## Tokens

Short-lived HS256 access tokens. The claims carry the role so the UI can
render the right navigation without a round trip, **but the API never trusts
the role from the token** — it re-reads the employee row on every request.
A token issued before someone was demoted must not keep their old powers.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import os
import secrets

import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Employee

__all__ = [
    "hash_password",
    "verify_password",
    "needs_rehash",
    "create_access_token",
    "decode_access_token",
    "authenticate",
    "AuthError",
    "TokenError",
    "ACCESS_TOKEN_MINUTES",
]

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 240_000
_JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_MINUTES = int(os.environ.get("ACCESS_TOKEN_MINUTES", "480"))


class AuthError(Exception):
    """Bad credentials, or an account that cannot log in."""


class TokenError(Exception):
    """Missing, malformed, expired or otherwise unusable token."""


def _secret() -> str:
    """The signing key.

    Read at call time rather than import time so tests and deployments can set
    it without import-order games. In production this MUST be set; the
    development fallback is random per process, which invalidates every token
    on restart — noisy by design, so nobody ships the default by accident.
    """
    secret = os.environ.get("JWT_SECRET")
    if secret:
        return secret
    global _DEV_SECRET
    if _DEV_SECRET is None:
        _DEV_SECRET = secrets.token_urlsafe(48)
    return _DEV_SECRET


_DEV_SECRET: str | None = None


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def hash_password(password: str, *, iterations: int = _ITERATIONS) -> str:
    if not password or len(password) < 8:
        raise AuthError("Password must be at least 8 characters.")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "$".join([
        _ALGORITHM,
        str(iterations),
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    ])


def verify_password(password: str, encoded: str | None) -> bool:
    """Constant-time check. A missing hash is a failure, never a pass."""
    if not encoded or not password:
        return False
    try:
        algorithm, iterations, salt_b64, hash_b64 = encoded.split("$")
        if algorithm != _ALGORITHM:
            return False
        expected = base64.b64decode(hash_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), base64.b64decode(salt_b64), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, actual)


def needs_rehash(encoded: str | None) -> bool:
    """True when a stored hash uses fewer iterations than we now require."""
    if not encoded:
        return False
    try:
        _, iterations, _, _ = encoded.split("$")
        return int(iterations) < _ITERATIONS
    except ValueError:
        return True


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
def create_access_token(employee: Employee, *, minutes: int | None = None) -> tuple[str, int]:
    """Return (token, expires_in_seconds)."""
    minutes = minutes or ACCESS_TOKEN_MINUTES
    now = dt.datetime.now(dt.timezone.utc)
    expires = now + dt.timedelta(minutes=minutes)
    payload = {
        "sub": str(employee.id),
        "email": employee.email,
        "name": employee.name,
        # Convenience for the UI only. The API re-reads the row every request.
        "role": employee.role,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    return jwt.encode(payload, _secret(), algorithm=_JWT_ALGORITHM), minutes * 60


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, _secret(), algorithms=[_JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Session expired. Please sign in again.") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Invalid session token.") from exc


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
def authenticate(session: Session, email: str, password: str) -> Employee:
    """Check credentials and return the employee.

    Every failure path returns the SAME message. Telling an attacker that an
    address exists but the password is wrong is a free account-enumeration
    oracle; a terminated employee's account state is likewise not something an
    unauthenticated caller should be able to probe.
    """
    generic = AuthError("Incorrect email or password.")

    employee = session.scalars(
        select(Employee).where(Employee.email == email.strip().lower())
    ).first()
    if employee is None:
        # Burn roughly the same time as a real verify, so response timing does
        # not reveal whether the address exists.
        verify_password(password, hash_password("timing-equaliser"))
        raise generic

    if not verify_password(password, employee.password_hash):
        raise generic
    if employee.status != "active":
        raise generic

    employee.last_login_at = dt.datetime.now(dt.timezone.utc)
    if needs_rehash(employee.password_hash):
        employee.password_hash = hash_password(password)
    return employee


def set_password(employee: Employee, password: str) -> None:
    employee.password_hash = hash_password(password)
    employee.must_change_password = False
