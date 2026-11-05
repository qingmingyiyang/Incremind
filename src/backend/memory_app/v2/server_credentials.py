import re
from threading import BoundedSemaphore

from argon2 import PasswordHasher, profiles

from backend.security.user_context import UserError

CREDENTIALS = 'server_login_credentials'
NAMES = 'server_login_names'
LOGIN_DEVICES = 'server_login_devices'
ATTEMPTS = 'server_login_attempts'
_NAME = re.compile(r'[A-Za-z0-9._-]{3,64}\Z')
_HASHER = PasswordHasher.from_parameters(profiles.RFC_9106_LOW_MEMORY)
_ARGON = BoundedSemaphore(2)


def prepare_credentials(login_name, password):
    if not isinstance(login_name, str) or not _NAME.fullmatch(login_name):
        raise UserError('login_name_invalid')
    if not isinstance(password, str) or not 15 <= len(password) <= 256:
        raise UserError('login_password_invalid')
    try:
        password.encode('utf-8')
    except UnicodeEncodeError:
        raise UserError('login_password_invalid') from None
    if not _ARGON.acquire(blocking=False):
        raise UserError('login_rate_limited')
    try:
        return login_name.casefold(), _HASHER.hash(password)
    finally:
        _ARGON.release()


def login_public(tx, user_id):
    row = tx.read(CREDENTIALS, user_id)
    return {'login_name': row.payload['login_name'] if row else None,
            'login_configured': row is not None, 'login_revision': row.revision if row else 0}


def store_credentials(tx, user_id, login_name, password_hash, expected_revision):
    old = tx.read(CREDENTIALS, user_id)
    if (old.revision if old else 0) != expected_revision:
        raise UserError('login_revision_conflict')
    key = 'name-' + login_name
    existing = tx.read(NAMES, key)
    if existing is not None and existing.payload['user_id'] != user_id:
        raise UserError('login_name_conflict')
    if old and old.payload['login_name'] != login_name:
        previous = tx.read(NAMES, 'name-' + old.payload['login_name'])
        tx.delete(NAMES, previous.object_id, expected_revision=previous.revision)
        attempt = tx.read(ATTEMPTS, previous.object_id)
        if attempt is not None:
            tx.delete(ATTEMPTS, attempt.object_id, expected_revision=attempt.revision)
    tx.put(NAMES, key, {'user_id': user_id}, expected_revision=existing.revision if existing else 0)
    tx.put(CREDENTIALS, user_id, {'user_id': user_id, 'login_name': login_name,
        'password_hash': password_hash}, expected_revision=expected_revision)
