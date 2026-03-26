from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session


class AuthError(ValueError):
    pass


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def _b64url_decode(raw: str) -> bytes:
    padding = '=' * ((4 - len(raw) % 4) % 4)
    return base64.urlsafe_b64decode(raw + padding)


def create_access_token(*, user_id: str, role: str, secret_key: str, expires_in_seconds: int) -> str:
    now = int(time.time())
    payload = {
        'sub': user_id,
        'role': role,
        'iat': now,
        'exp': now + expires_in_seconds,
    }
    payload_bytes = json.dumps(payload, separators=(',', ':'), ensure_ascii=True).encode('utf-8')
    payload_token = _b64url_encode(payload_bytes)
    signature = hmac.new(secret_key.encode('utf-8'), payload_token.encode('ascii'), hashlib.sha256).digest()
    signature_token = _b64url_encode(signature)
    return f'{payload_token}.{signature_token}'


def decode_access_token(token: str, *, secret_key: str) -> dict:
    try:
        payload_token, signature_token = token.split('.', maxsplit=1)
    except ValueError as exc:
        raise AuthError('Malformed access token.') from exc

    expected_signature = hmac.new(secret_key.encode('utf-8'), payload_token.encode('ascii'), hashlib.sha256).digest()
    actual_signature = _b64url_decode(signature_token)
    if not hmac.compare_digest(actual_signature, expected_signature):
        raise AuthError('Invalid token signature.')

    try:
        payload = json.loads(_b64url_decode(payload_token))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise AuthError('Invalid token payload.') from exc

    exp = payload.get('exp')
    if not isinstance(exp, int) or exp < int(time.time()):
        raise AuthError('Token has expired.')

    sub = payload.get('sub')
    if not isinstance(sub, str) or not sub:
        raise AuthError('Token subject is missing.')

    return payload


def extract_bearer_token(authorization_header: str | None) -> str | None:
    if not authorization_header:
        return None
    parts = authorization_header.strip().split()
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        raise AuthError('Authorization header must be Bearer token.')
    return parts[1]


def _verify_pbkdf2_sha256_modular_crypt(plain_password: str, password_hash: str) -> bool:
    # Format: $pbkdf2-sha256$<rounds>$<salt>$<checksum>
    parts = password_hash.split('$')
    if len(parts) != 5 or parts[1] != 'pbkdf2-sha256':
        return False

    try:
        rounds = int(parts[2])
    except ValueError:
        return False
    salt = parts[3]
    expected = parts[4]
    if rounds <= 0 or not salt or not expected:
        return False

    derived = hashlib.pbkdf2_hmac('sha256', plain_password.encode('utf-8'), salt.encode('utf-8'), rounds)
    encoded = base64.b64encode(derived).decode('ascii').rstrip('=').replace('+', '.')
    return hmac.compare_digest(encoded, expected)


def _verify_pbkdf2_sha256_legacy(plain_password: str, password_hash: str) -> bool:
    # Supported legacy variants:
    # 1) pbkdf2_sha256$<iterations>$<salt>$<base64|hex checksum>
    # 2) pbkdf2_sha256$<salt>$<hex checksum>   (legacy social format)
    parts = password_hash.split('$')
    if not parts or parts[0] != 'pbkdf2_sha256':
        return False

    def pbkdf2_hex(rounds: int, salt: str) -> str:
        return hashlib.pbkdf2_hmac('sha256', plain_password.encode('utf-8'), salt.encode('utf-8'), rounds).hex()

    if len(parts) == 4 and parts[1].isdigit():
        rounds = int(parts[1])
        salt = parts[2]
        expected = parts[3]
        if rounds <= 0 or not salt or not expected:
            return False
        derived = hashlib.pbkdf2_hmac('sha256', plain_password.encode('utf-8'), salt.encode('utf-8'), rounds)
        if len(expected) == 64 and all(ch in '0123456789abcdef' for ch in expected.lower()):
            return hmac.compare_digest(derived.hex(), expected.lower())
        encoded = base64.b64encode(derived).decode('ascii').strip()
        return hmac.compare_digest(encoded, expected)

    if len(parts) == 3:
        salt = parts[1]
        expected = parts[2].lower()
        if not salt or not expected:
            return False

        # Try common PBKDF2 iteration values used across legacy services.
        candidate_rounds = (600000, 390000, 320000, 260000, 200000, 150000, 120000, 100000, 60000, 29000, 10000, 1000, 1)
        for rounds in candidate_rounds:
            if hmac.compare_digest(pbkdf2_hex(rounds, salt), expected):
                return True

        # Final fallback for very old custom implementations.
        if hmac.compare_digest(hashlib.sha256(f'{salt}{plain_password}'.encode('utf-8')).hexdigest(), expected):
            return True
        if hmac.compare_digest(hashlib.sha256(f'{plain_password}{salt}'.encode('utf-8')).hexdigest(), expected):
            return True
    return False


def verify_password(db: Session, *, plain_password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False

    if password_hash.startswith('$pbkdf2-sha256$'):
        return _verify_pbkdf2_sha256_modular_crypt(plain_password, password_hash)
    if password_hash.startswith('pbkdf2_sha256$'):
        return _verify_pbkdf2_sha256_legacy(plain_password, password_hash)

    # Local/dev fallback where passwords may be stored in plaintext.
    if hmac.compare_digest(plain_password, password_hash):
        return True

    # Shared Postgres path: verify crypt() hashes (bcrypt/pgcrypto).
    try:
        result = db.scalar(
            text('SELECT crypt(:plain_password, :password_hash) = :password_hash'),
            {'plain_password': plain_password, 'password_hash': password_hash},
        )
        return bool(result)
    except SQLAlchemyError:
        db.rollback()
        return False
