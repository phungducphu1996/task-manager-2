import base64
import hashlib
from uuid import uuid4

from app.models import User


def make_modular_pbkdf2_sha256(password: str, *, rounds: int, salt: str) -> str:
    derived = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), rounds)
    checksum = base64.b64encode(derived).decode('ascii').rstrip('=').replace('+', '.')
    return f'$pbkdf2-sha256${rounds}${salt}${checksum}'


def make_legacy_social_pbkdf2_sha256(password: str, *, rounds: int, salt: str) -> str:
    derived_hex = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), rounds).hex()
    return f'pbkdf2_sha256${salt}${derived_hex}'


def test_login_and_me(client) -> None:
    login = client.post('/auth/login', json={'username': 'trang', 'password': 'trang123'})
    assert login.status_code == 200

    payload = login.json()
    assert payload['token_type'] == 'bearer'
    assert payload['access_token']
    assert payload['user']['name'] == 'Trang'
    assert payload['user']['username'] == 'trang'
    assert 'zalo_user_id' in payload['user']
    assert 'avatar_url' in payload['user']

    me = client.get('/auth/me', headers={'Authorization': f"Bearer {payload['access_token']}"})
    assert me.status_code == 200
    assert me.json()['name'] == 'Trang'


def test_login_invalid_password(client) -> None:
    login = client.post('/auth/login', json={'username': 'trang', 'password': 'wrong-password'})
    assert login.status_code == 401


def test_login_rejects_user_without_password_hash(client, db_session) -> None:
    db_session.add(
        User(
            id=str(uuid4()),
            username='nohash_user',
            full_name='No Hash',
            password_hash=None,
            role='content',
            is_active=True,
        )
    )
    db_session.commit()

    login = client.post('/auth/login', json={'username': 'nohash_user', 'password': 'anything'})
    assert login.status_code == 403


def test_login_with_django_pbkdf2_hash(client, db_session) -> None:
    db_session.add(
        User(
            id=str(uuid4()),
            username='django_user',
            full_name='Django User',
            password_hash=make_legacy_social_pbkdf2_sha256('secret123', rounds=29000, salt='legacysalt001'),
            role='content',
            is_active=True,
        )
    )
    db_session.commit()

    login = client.post('/auth/login', json={'username': 'django_user', 'password': 'secret123'})
    assert login.status_code == 200


def test_login_with_passlib_pbkdf2_hash(client, db_session) -> None:
    db_session.add(
        User(
            id=str(uuid4()),
            username='passlib_user',
            full_name='Passlib User',
            password_hash=make_modular_pbkdf2_sha256('secret456', rounds=29000, salt='modularsalt002'),
            role='content',
            is_active=True,
        )
    )
    db_session.commit()

    login = client.post('/auth/login', json={'username': 'passlib_user', 'password': 'secret456'})
    assert login.status_code == 200
