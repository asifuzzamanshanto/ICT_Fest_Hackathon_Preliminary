"""Authentication: password hashing, JWT issue/verify, request dependencies."""
import hashlib
import hmac
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    JWT_ALGORITHM,
    JWT_SECRET,
    REFRESH_TOKEN_EXPIRE_DAYS,
)
from .database import get_db
from .errors import AppError
from .models import TokenState, User

# Access tokens presented to /auth/logout are recorded here so they can no
# longer be used.
_revoked_tokens: set[str] = set()
_used_refresh_tokens: set[str] = set()
_token_lock = threading.Lock()

_PBKDF2_ROUNDS = 100_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"{salt.hex()}:{dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split(":")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), _PBKDF2_ROUNDS)
    return hmac.compare_digest(dk.hex(), dk_hex)


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def create_access_token(user: User) -> str:
    iat = _now_ts()
    lifetime = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user.id),
        "org": user.org_id,
        "role": user.role,
        "jti": uuid.uuid4().hex,
        "iat": iat,
        "exp": iat + int(lifetime.total_seconds()),
        "type": "access",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_refresh_token(user: User) -> str:
    iat = _now_ts()
    lifetime = timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": str(user.id),
        "org": user.org_id,
        "role": user.role,
        "jti": uuid.uuid4().hex,
        "iat": iat,
        "exp": iat + int(lifetime.total_seconds()),
        "type": "refresh",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        raise AppError(401, "UNAUTHORIZED", "Invalid or expired token")


def _claim_jti(payload: dict) -> str:
    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti:
        raise AppError(401, "UNAUTHORIZED", "Invalid token claims")
    return jti


def token_subject_user_id(payload: dict) -> int:
    sub = payload.get("sub")
    if not isinstance(sub, str):
        raise AppError(401, "UNAUTHORIZED", "Invalid token claims")
    try:
        return int(sub)
    except ValueError:
        raise AppError(401, "UNAUTHORIZED", "Invalid token claims")


def _claim_exp_datetime(payload: dict) -> datetime:
    exp = payload.get("exp")
    if not isinstance(exp, int):
        raise AppError(401, "UNAUTHORIZED", "Invalid token claims")
    return datetime.fromtimestamp(exp, tz=timezone.utc).replace(tzinfo=None)


def _token_state_exists(db: Session, jti: str, token_type: str) -> bool:
    return (
        db.query(TokenState)
        .filter(TokenState.jti == jti, TokenState.token_type == token_type)
        .first()
        is not None
    )


def _record_token_state(db: Session, payload: dict, token_type: str) -> None:
    jti = _claim_jti(payload)
    if _token_state_exists(db, jti, token_type):
        return
    db.add(
        TokenState(
            jti=jti,
            token_type=token_type,
            expires_at=_claim_exp_datetime(payload),
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if not _token_state_exists(db, jti, token_type):
            raise


def revoke_access_token(payload: dict, db: Session) -> None:
    jti = _claim_jti(payload)
    with _token_lock:
        _revoked_tokens.add(jti)
    _record_token_state(db, payload, "access_revoked")


def mark_refresh_token_used(payload: dict, db: Session) -> None:
    jti = _claim_jti(payload)
    with _token_lock:
        if jti in _used_refresh_tokens:
            raise AppError(401, "UNAUTHORIZED", "Refresh token has already been used")
        if _token_state_exists(db, jti, "refresh_used"):
            raise AppError(401, "UNAUTHORIZED", "Refresh token has already been used")
        _used_refresh_tokens.add(jti)
    _record_token_state(db, payload, "refresh_used")


def get_token_payload(request: Request, db: Session = Depends(get_db)) -> dict:
    header = request.headers.get("Authorization")
    if not header or not header.startswith("Bearer "):
        raise AppError(401, "UNAUTHORIZED", "Missing bearer token")
    token = header[len("Bearer "):].strip()
    payload = decode_token(token)
    if payload.get("type") != "access":
        raise AppError(401, "UNAUTHORIZED", "Wrong token type")
    jti = _claim_jti(payload)
    token_subject_user_id(payload)
    with _token_lock:
        if jti in _revoked_tokens or _token_state_exists(db, jti, "access_revoked"):
            raise AppError(401, "UNAUTHORIZED", "Token has been revoked")
    return payload


def get_current_user(
    payload: dict = Depends(get_token_payload),
    db: Session = Depends(get_db),
) -> User:
    user = db.query(User).filter(User.id == token_subject_user_id(payload)).first()
    if user is None:
        raise AppError(401, "UNAUTHORIZED", "Unknown user")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise AppError(403, "FORBIDDEN", "Admin privileges required")
    return user
