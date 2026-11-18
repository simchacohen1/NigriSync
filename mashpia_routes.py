"""Teacher routes for the Daily Checklist -> Mashpia sync.

Off unless MASHPIA_SYNC_ENABLED=1. Uses the same teacher sign-in check, rate limit and key as the Nigri login routes.
  GET/PUT/DELETE /mashpia-login   status / save (after Mashpia accepts it) / remove. Never returns the password.
  POST           /mashpia-sync    {date:"YYYY-MM-DD", students:[{name, items:[names answered Yes]}]}
"""
import os

from flask import Blueprint, jsonify, request

import mashpia_sync as M
import nigri_login_routes as R
from nigri_login_store import LoginStoreError

bp = Blueprint('mashpia', __name__)
MAX_BODY = 200000
_store = None
_client_factory = None   # tests replace this; production builds a real HTTPS client


def enabled():
    return os.environ.get('MASHPIA_SYNC_ENABLED') == '1'


def _get_store():
    global _store
    if _store is None:
        from nigri_login_store import mashpia_store_from_environment
        _store = mashpia_store_from_environment()
    return _store


def _client():
    return _client_factory() if _client_factory else M.MashpiaClient()


@bp.before_request
def _gate():
    if request.method == 'OPTIONS':
        return None
    if not enabled():
        return R._reply({'error': 'not found'}, 404)
    uid = R.teacher_uid()
    if not uid:
        return R._reply({'error': 'unauthorized'}, 401)
    if R._limited(uid):
        return R._reply({'error': 'Too many tries. Wait a minute.'}, 429)
    request.environ['mashpia_uid'] = uid
    return None


@bp.route('/mashpia-login', methods=['GET'])
def login_status():
    try:
        return R._reply(_get_store().status(request.environ['mashpia_uid']))
    except LoginStoreError as exc:
        return R._reply({'error': str(exc)}, 500)


@bp.route('/mashpia-login', methods=['PUT'])
def login_save():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return R._reply({'error': 'Invalid request'}, 400)
    username, password = body.get('username'), body.get('password')
    if not isinstance(username, str) or not isinstance(password, str) or not username.strip() or not password:
        return R._reply({'error': 'Enter your Mashpia username and password.'}, 400)
    try:
        _client().login(username.strip(), password)       # a wrong password is never saved
        return R._reply(_get_store().save(request.environ['mashpia_uid'], username.strip(), password))
    except M.MashpiaError as exc:
        return R._reply({'error': str(exc)}, 400)
    except LoginStoreError as exc:
        return R._reply({'error': str(exc)}, 400)


@bp.route('/mashpia-login', methods=['DELETE'])
def login_remove():
    try:
        return R._reply(_get_store().remove(request.environ['mashpia_uid']))
    except LoginStoreError as exc:
        return R._reply({'error': str(exc)}, 500)


@bp.route('/mashpia-sync', methods=['POST'])
def sync():
    if (request.content_length or 0) > MAX_BODY:
        return R._reply({'error': 'Request too large'}, 413)
    body = request.get_json(silent=True)
    students = body.get('students') if isinstance(body, dict) else None
    if not isinstance(students, list) or not students or len(students) > 300:
        return R._reply({'error': 'Nothing to sync.'}, 400)
    clean = []
    for s in students:
        if not isinstance(s, dict) or not isinstance(s.get('name'), str):
            return R._reply({'error': 'Invalid request'}, 400)
        items = [i for i in (s.get('items') or []) if isinstance(i, str)][:60]
        clean.append({'name': s['name'][:120], 'items': items})
    try:
        creds = _get_store().get_credentials(request.environ['mashpia_uid'])
    except LoginStoreError as exc:
        return R._reply({'error': str(exc)}, 500)
    if creds is None:
        return R._reply({'error': 'Mashpia login needed', 'code': 'need_login'}, 409)
    try:
        client = _client()
        client.login(creds.username, creds.password)
        result = M.sync_day(client, body.get('date'), clean)
    except M.MashpiaError as exc:
        text = str(exc)
        if 'accept that username' in text:      # saved password changed on Mashpia: ask once more
            return R._reply({'error': text, 'code': 'need_login'}, 409)
        return R._reply({'error': text}, 502)
    return R._reply(result)
