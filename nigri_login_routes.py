"""Teacher screen routes for his own Nigri login: status, save/replace, remove (issue 550, step 2).

Off unless NIGRI_LOGIN_ENABLED=1, so deploying this code changes nothing by itself. Never contacts Nigri: it only
stores the encrypted login (nigri_login_store). Responses carry only {saved, hint, updatedAt}, never a password.
Settings (Render): NIGRI_LOGIN_ENABLED, FIREBASE_PROJECT_ID (default b3-games; staging uses its own project),
NIGRI_LOGIN_WORKSPACE (default b3-2026), plus the store settings (NIGRI_LOGIN_KEYS, FIREBASE_*).
Schools (B3-games#631): every request also needs the caller's school to have "Syncs to Nigri" on, or the caller to be
the owner (NIGRI_OWNER_EMAIL, default the production owner; staging sets its own). See nigri_school_access.py.
"""
import hmac
import os
import threading
import time

from flask import Blueprint, jsonify, request

from nigri_login_store import LoginStoreError, store_from_environment
import nigri_school_access as A

bp = Blueprint('nigri_login', __name__)
_lock = threading.Lock()
_hits = {}
LIMIT, WINDOW = 12, 60   # at most 12 requests per teacher per minute
MAX_BODY = 2048
_store = None
_teacher_check = None   # tests replace these three; production builds them from the environment
_school_check = None


def enabled():
    return os.environ.get('NIGRI_LOGIN_ENABLED') == '1'


def project_id():
    return os.environ.get('FIREBASE_PROJECT_ID', 'b3-games')


def _verify_token(token):
    from google.oauth2 import id_token
    from google.auth.transport.requests import Request
    claims = id_token.verify_firebase_token(token, Request(), audience=project_id())
    if claims.get('iss') != 'https://securetoken.google.com/' + project_id() or claims.get('email_verified') is not True:
        return None
    return claims


def _is_teacher(uid):
    """True only for an active teacher of the workspace (checked server-side in Firebase)."""
    if _teacher_check is not None:
        return _teacher_check(uid)
    from nigri_login_store import FirebaseRestBackend
    ws = os.environ.get('NIGRI_LOGIN_WORKSPACE', 'b3-2026')
    return FirebaseRestBackend().read_path('b3Games/workspaces/%s/teachers/%s/active' % (ws, uid)) is True


def _get_store():
    global _store
    if _store is None:
        _store = store_from_environment()
    return _store


def owner_email():
    return os.environ.get('NIGRI_OWNER_EMAIL', A.OWNER_EMAIL)


def _caller():
    """(uid, claims) of a verified Google sign-in, else (None, None). The old shared key never counts here."""
    header = request.headers.get('Authorization', '')
    if not header.startswith('Bearer '):
        return None, None
    try:
        claims = _verify_token(header[7:])
        uid = claims and claims.get('sub')
        return (uid, claims) if uid else (None, None)
    except Exception:
        return None, None


def teacher_uid():
    """Verified Google sign-in of the owner or an active teacher, else None."""
    uid, claims = _caller()
    if not uid:
        return None
    try:
        return uid if A.is_owner(claims, owner_email()) or _is_teacher(uid) else None
    except Exception:
        return None


def _school_access(uid, claims):
    """May he use Nigri? His school must have "Syncs to Nigri" on (nigri_school_access). Checked on every request."""
    if _school_check is not None:
        return _school_check(uid, claims)
    workspace = os.environ.get('NIGRI_LOGIN_WORKSPACE', 'b3-2026')
    return A.nigri_access(_read_path, workspace, uid, claims, owner_email())


def _limited(uid):
    now = time.time()
    with _lock:
        recent = [t for t in _hits.get(uid, []) if now - t < WINDOW]
        if len(recent) >= LIMIT:
            _hits[uid] = recent
            return True
        recent.append(now)
        _hits[uid] = recent
    return False


def _reply(body, status=200):
    response = jsonify(body)
    response.headers['Cache-Control'] = 'no-store'
    return response, status


def teacher_sync_credentials():
    """For the real sync routes (e.g. /sync-marks) when the caller is a TEACHER, not the owner or the old key.
    Returns (credentials, None) to log in to Nigri as him, or (None, (response, status)) to refuse.
    Same rules as the login screen: active teacher, his school syncs to Nigri, his own saved login, checked every time.
    Credentials never leave the server."""
    if not enabled():
        return None, _reply({'error': 'unauthorized'}, 401)
    uid, claims = _caller()
    try:
        if not uid or not _is_teacher(uid):
            return None, _reply({'error': 'unauthorized'}, 401)
    except Exception:
        return None, _reply({'error': 'unauthorized'}, 401)
    if _limited(uid):
        return None, _reply({'error': 'Too many tries. Wait a minute.'}, 429)
    try:
        access = _school_access(uid, claims)
    except Exception:
        return None, _reply({'error': 'Could not check right now.'}, 500)
    if not access.get('allowed'):
        if access.get('reason') == 'not_teacher':
            return None, _reply({'error': 'unauthorized'}, 401)
        reason = access.get('reason', 'school_off')
        return None, _reply({'error': A.MESSAGES.get(reason, A.MESSAGES['school_off']), 'code': reason}, 403)
    try:
        store = _get_store()
        if access.get('reset_at'):
            store.drop_if_saved_before(uid, access['reset_at'])
        credentials = store.get_credentials(uid)
    except LoginStoreError as exc:
        return None, _reply({'error': str(exc)}, 500)
    if credentials is None:
        return None, _reply({'error': 'Connect your Nigri account first.', 'code': 'no_login'}, 409)
    return credentials, None


@bp.before_request
def _gate():
    if request.method == 'OPTIONS':
        return None
    if not enabled():
        return _reply({'error': 'not found'}, 404)
    uid, claims = _caller()
    try:
        allowed_person = bool(uid) and (A.is_owner(claims, owner_email()) or _is_teacher(uid))
    except Exception:
        allowed_person = False
    if not allowed_person:
        return _reply({'error': 'unauthorized'}, 401)
    if _limited(uid):
        return _reply({'error': 'Too many tries. Wait a minute.'}, 429)
    try:
        access = _school_access(uid, claims)
    except Exception:
        return _reply({'error': 'Could not check right now.'}, 500)
    # Removing his own saved login is always allowed; everything else needs a school that syncs to Nigri.
    removing = request.method == 'DELETE' and request.path == '/nigri-login'
    if not access.get('allowed') and not removing:
        if access.get('reason') == 'not_teacher':
            return _reply({'error': 'unauthorized'}, 401)
        reason = access.get('reason', 'school_off')
        return _reply({'error': A.MESSAGES.get(reason, A.MESSAGES['school_off']), 'code': reason}, 403)
    # Moved to another school by the owner: a login saved before the move is deleted, never used.
    if access.get('reset_at'):
        try:
            _get_store().drop_if_saved_before(uid, access['reset_at'])
        except LoginStoreError as exc:
            return _reply({'error': str(exc)}, 500)
    request.environ['nigri_login_uid'] = uid
    return None


@bp.route('/nigri-login', methods=['GET'])
def login_status():
    try:
        return _reply(_get_store().status(request.environ['nigri_login_uid']))
    except LoginStoreError as exc:
        return _reply({'error': str(exc)}, 500)


@bp.route('/nigri-login', methods=['PUT'])
def login_save():
    if (request.content_length or 0) > MAX_BODY:
        return _reply({'error': 'Request too large'}, 413)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _reply({'error': 'Invalid request'}, 400)
    try:
        return _reply(_get_store().save(request.environ['nigri_login_uid'], body.get('username'), body.get('password')))
    except LoginStoreError as exc:
        return _reply({'error': str(exc)}, 400)


@bp.route('/nigri-login', methods=['DELETE'])
def login_remove():
    try:
        return _reply(_get_store().remove(request.environ['nigri_login_uid']))
    except LoginStoreError as exc:
        return _reply({'error': str(exc)}, 500)


_reader = None   # tests replace these two; production reads Firebase and has no Nigri course reader yet
_lister = None


def _read_path(path):
    if _reader is not None:
        return _reader(path)
    from nigri_login_store import FirebaseRestBackend
    return FirebaseRestBackend().read_path(path)


@bp.route('/nigri-courses', methods=['GET'])
def course_matches():
    """For one of his classes: its Nigri grade and which course IDs he may sync to (never lists shared rooms)."""
    import nigri_courses as N
    uid = request.environ['nigri_login_uid']
    try:
        try:
            credentials = _get_store().get_credentials(uid)
        except LoginStoreError:
            credentials = None
        workspace = os.environ.get('NIGRI_LOGIN_WORKSPACE', 'b3-2026')
        result = N.course_status(_read_path, workspace, uid, request.args.get('class', ''), credentials,
                                 _lister or N.default_lister())
        return _reply(result)
    except N.MatchError as exc:
        return _reply({'error': exc.message, 'code': exc.code}, 403 if exc.code == 'not_your_class' else 400)
    except Exception:
        return _reply({'error': 'Could not check right now.'}, 500)
