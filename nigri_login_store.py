"""Per-teacher Nigri login, encrypted here before it is saved to Firebase (issue 550, step 1).

Firebase only ever holds scrambled text. The key lives only in the Render setting NIGRI_LOGIN_KEYS and is never
stored in Firebase, GitHub or the browser. Nothing here is wired to a route yet, so teachers see no change.

NIGRI_LOGIN_KEYS = "newid:<base64 of 32 random bytes>,oldid:<base64 of 32 random bytes>"
  The FIRST key encrypts new records; every listed key can still open old ones (key replacement without re-entry).
  Make a key with:  openssl rand -base64 32
FIREBASE_SERVICE_ACCOUNT_JSON / FIREBASE_DATABASE_URL = how the server reaches Firebase (Render settings).
  Records live at b3Private/nigriLogins/<teacherUid>. That path is already closed to every browser by the rules
  (b3Private is read:false, write:false); only this server's service account can touch it.
"""
import base64
import binascii
import json
import os
import re
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

RECORD_VERSION = 1
LOGIN_PATH = 'b3Private/nigriLogins'
_UID = re.compile(r'[A-Za-z0-9]{6,128}')   # Firebase Auth uids; also blocks path tricks like "../x"
_KID = re.compile(r'[A-Za-z0-9_-]{1,32}')
MAX_FIELD = 256


class LoginStoreError(Exception):
    """Safe to show in a response or log: never contains a username, password or key."""


class Credentials:
    """A decrypted login. repr/str are redacted so it cannot leak into logs or tracebacks by accident."""
    __slots__ = ('username', 'password')

    def __init__(self, username, password):
        self.username = username
        self.password = password

    def __repr__(self):
        return 'Credentials(<redacted>)'

    __str__ = __repr__


def load_keyring(raw=None):
    """Parse NIGRI_LOGIN_KEYS into (current_kid, {kid: 32-byte key}). Fails closed when missing or malformed."""
    raw = os.environ.get('NIGRI_LOGIN_KEYS', '') if raw is None else raw
    keys, current = {}, None
    for part in [p.strip() for p in raw.split(',') if p.strip()]:
        kid, sep, b64 = part.partition(':')
        if not sep or not _KID.fullmatch(kid) or kid in keys:
            raise LoginStoreError('NIGRI_LOGIN_KEYS is not set up correctly')
        try:
            key = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise LoginStoreError('NIGRI_LOGIN_KEYS is not set up correctly')
        if len(key) != 32:
            raise LoginStoreError('NIGRI_LOGIN_KEYS is not set up correctly')
        keys[kid] = key
        current = current or kid
    if not keys:
        raise LoginStoreError('NIGRI_LOGIN_KEYS is not set up correctly')
    return current, keys


def _check_uid(uid):
    if not isinstance(uid, str) or not _UID.fullmatch(uid):
        raise LoginStoreError('Invalid teacher id')
    return uid


def _aad(uid, kid):
    # Ties the record to ONE teacher: copied under another uid, it will not open.
    return ('nigri-login|v%d|%s|%s' % (RECORD_VERSION, uid, kid)).encode('utf-8')


def _b64(data):
    return base64.b64encode(data).decode('ascii')


def encrypt_login(uid, username, password, keyring, now=None):
    uid = _check_uid(uid)
    for value in (username, password):
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_FIELD:
            raise LoginStoreError('Enter both a Nigri username and password')
    kid, keys = keyring
    payload = json.dumps({'u': username, 'p': password, 'h': username[-4:]}).encode('utf-8')
    nonce = os.urandom(12)   # new random value every time
    ct = AESGCM(keys[kid]).encrypt(nonce, payload, _aad(uid, kid))
    return {'v': RECORD_VERSION, 'kid': kid, 'n': _b64(nonce), 'ct': _b64(ct),
            'updatedAt': int((time.time() if now is None else now) * 1000)}


def decrypt_login(uid, record, keyring):
    """Returns a dict {username, password, hint}. Any failure is the same generic error (no oracle)."""
    uid = _check_uid(uid)
    try:
        if not isinstance(record, dict) or record.get('v') != RECORD_VERSION:
            raise ValueError
        kid = record['kid']
        key = keyring[1][kid]
        plain = AESGCM(key).decrypt(base64.b64decode(record['n'], validate=True),
                                    base64.b64decode(record['ct'], validate=True), _aad(uid, kid))
        data = json.loads(plain.decode('utf-8'))
        return {'username': data['u'], 'password': data['p'], 'hint': data['h']}
    except (KeyError, ValueError, TypeError, binascii.Error, InvalidTag):
        raise LoginStoreError('The saved Nigri login could not be opened')


class MemoryBackend:
    """For tests: behaves like the Firebase path, in a dict."""

    def __init__(self):
        self.data = {}

    def get(self, uid):
        return self.data.get(uid)

    def put(self, uid, record):
        self.data[uid] = json.loads(json.dumps(record))

    def delete(self, uid):
        self.data.pop(uid, None)


class FirebaseRestBackend:
    """Reads/writes b3Private/nigriLogins/<uid> over the Realtime Database REST API as the server's service account."""

    def __init__(self, database_url=None, session=None, service_account_json=None):
        self.base = (database_url or os.environ.get('FIREBASE_DATABASE_URL', '')).rstrip('/')
        if not self.base.startswith('https://'):
            raise LoginStoreError('FIREBASE_DATABASE_URL is not set up correctly')
        self._session = session
        self._sa = service_account_json

    def _http(self):
        if self._session is None:
            from google.oauth2 import service_account
            from google.auth.transport.requests import AuthorizedSession
            info = json.loads(self._sa or os.environ.get('FIREBASE_SERVICE_ACCOUNT_JSON', ''))
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=['https://www.googleapis.com/auth/firebase.database',
                              'https://www.googleapis.com/auth/userinfo.email'])
            self._session = AuthorizedSession(creds)
        return self._session

    def _url(self, uid):
        return '%s/%s/%s.json' % (self.base, LOGIN_PATH, _check_uid(uid))

    def _check(self, response):
        if response.status_code >= 300:
            raise LoginStoreError('Could not reach the login storage (status %s)' % response.status_code)
        return response

    def get(self, uid):
        r = self._check(self._http().get(self._url(uid), timeout=15))
        return r.json()

    def put(self, uid, record):
        self._check(self._http().put(self._url(uid), data=json.dumps(record), timeout=15))

    def delete(self, uid):
        self._check(self._http().delete(self._url(uid), timeout=15))


class NigriLoginStore:
    def __init__(self, backend, keyring):
        self.backend = backend
        self.keyring = keyring

    def save(self, uid, username, password, now=None):
        """Save or replace this teacher's login. Returns the safe status (never the password)."""
        record = encrypt_login(uid, username, password, self.keyring, now=now)
        self.backend.put(uid, record)
        return {'saved': True, 'hint': username[-4:], 'updatedAt': record['updatedAt']}

    def status(self, uid):
        """What a teacher may see: whether a login is saved and the last 4 characters of the username."""
        record = self.backend.get(_check_uid(uid))
        if record is None:
            return {'saved': False}
        data = decrypt_login(uid, record, self.keyring)
        return {'saved': True, 'hint': data['hint'], 'updatedAt': record.get('updatedAt')}

    def remove(self, uid):
        self.backend.delete(_check_uid(uid))
        return {'saved': False}

    def get_credentials(self, uid):
        """Server-internal only (the sync code). Never return this from a route."""
        record = self.backend.get(_check_uid(uid))
        if record is None:
            return None
        data = decrypt_login(uid, record, self.keyring)
        return Credentials(data['username'], data['password'])

    def reencrypt(self, uid):
        """After adding a new key first in NIGRI_LOGIN_KEYS: re-lock one record with the current key."""
        record = self.backend.get(_check_uid(uid))
        if record is None:
            return False
        data = decrypt_login(uid, record, self.keyring)
        self.backend.put(uid, encrypt_login(uid, data['username'], data['password'], self.keyring,
                                            now=record.get('updatedAt', 0) / 1000))
        return True


def store_from_environment():
    """Used by the server once routes exist (step 2). Fails closed if the key or Firebase settings are missing."""
    return NigriLoginStore(FirebaseRestBackend(), load_keyring())
