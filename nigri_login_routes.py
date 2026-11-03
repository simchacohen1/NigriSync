"""Teacher screen routes for his own Nigri login: status, save/replace, remove (issue 550, step 2).

Off unless NIGRI_LOGIN_ENABLED=1, so deploying this code changes nothing by itself. Never contacts Nigri: it only
stores the encrypted login (nigri_login_store). Responses carry only {saved, hint, updatedAt}, never a password.
Settings (Render): NIGRI_LOGIN_ENABLED, FIREBASE_PROJECT_ID (default b3-games; staging uses its own project),
NIGRI_LOGIN_WORKSPACE (default b3-2026), plus the store settings (NIGRI_LOGIN_KEYS, FIREBASE_*).
"""
import hmac
import os
import threading
import time

from flask import Blueprint, jsonify, request

from nigri_login_store import LoginStoreError, store_from_environment

bp = Blueprint('nigri_login', __name__)
_lock = threading.Lock()
_hits = {}
LIMIT, WINDOW = 12, 60   # at most 12 requests per teacher per minute
MAX_BODY = 2048
_store = None
_teacher_check = None   # tests replace these two; production builds them from the environment


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


def teacher_uid():
    """Verified Google sign-in of an active teacher, else None. The old shared key never counts here."""
    header = request.headers.get('Authorization', '')
    if not header.startswith('Bearer '):
        return None
    try:
        claims = _verify_token(header[7:])
        uid = claims and claims.get('sub')
        return uid if uid and _is_teacher(uid) else None
    except Exception:
        return None


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


@bp.before_request
def _gate():
    if request.method == 'OPTIONS':
        return None
    if not enabled():
        return _reply({'error': 'not found'}, 404)
    uid = teacher_uid()
    if not uid:
        return _reply({'error': 'unauthorized'}, 401)
    if _limited(uid):
        return _reply({'error': 'Too many tries. Wait a minute.'}, 429)
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
