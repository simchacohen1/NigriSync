"""Teacher screen routes (issue 550, step 2). Made-up teachers and passwords; mock storage; never touches Nigri."""
import base64
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['SYNC_API_KEY'] = 'test-key-123'
sys.modules.setdefault('fcntl', types.ModuleType('fcntl'))


class _Stubs(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith('__'): raise AttributeError(name)
        if name == 'SyncError': return type('SyncError', (Exception,), {})
        return lambda *a, **k: {}
for name in ('nigri_playwright', 'attendance_reader'):
    sys.modules.setdefault(name, _Stubs(name))
import nigri_login_routes as R
import nigri_login_store as S
import app as server

KEY = 'k1:' + base64.b64encode(bytes(range(32))).decode()
T1, T2, PUPIL = 'teacherUidOne123', 'teacherUidTwo456', 'studentUidXyz789'
USER, PW = 'faketeacher@example.test', 'Fake-Pa55word!-never-real'
TEACHERS = {T1, T2}


def fake_verify(token):
    uid = {'tok-t1': T1, 'tok-t2': T2, 'tok-pupil': PUPIL}.get(token)
    if uid is None: raise ValueError('bad token')
    return {'sub': uid, 'email_verified': True}


class Base(unittest.TestCase):
    def setUp(self):
        self.backend = S.MemoryBackend()
        R._store = S.NigriLoginStore(self.backend, S.load_keyring(KEY))
        R._teacher_check = lambda uid: uid in TEACHERS
        R._school_check = lambda uid, claims: {'allowed': True, 'reason': 'ok', 'reset_at': None}  # see test_nigri_school_access.py
        R._hits.clear()
        self.patches = [patch.object(R, '_verify_token', fake_verify), patch.dict(os.environ, {'NIGRI_LOGIN_ENABLED': '1'})]
        for p in self.patches: p.start()
        self.c = server.app.test_client()

    def tearDown(self):
        for p in self.patches: p.stop()
        R._store = R._teacher_check = R._school_check = None

    def call(self, method, token='tok-t1', **kw):
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        return getattr(self.c, method)('/nigri-login', headers=headers, **kw)


class RouteTests(Base):
    def test_off_by_default(self):
        with patch.dict(os.environ, {'NIGRI_LOGIN_ENABLED': '0'}):
            self.assertEqual(self.call('get').status_code, 404)

    def test_save_status_replace_remove(self):
        self.assertEqual(self.call('get').get_json(), {'saved': False})
        r = self.call('put', json={'username': USER, 'password': PW})
        self.assertEqual((r.status_code, r.get_json()['hint']), (200, 'test'))
        self.assertEqual(self.call('get').get_json()['hint'], 'test')
        self.call('put', json={'username': 'other.name@example.test', 'password': 'Another-Fake-1'})
        self.assertEqual(self.call('get').get_json()['hint'], 'test')
        self.assertEqual(R._store.get_credentials(T1).password, 'Another-Fake-1')
        self.assertEqual(self.call('delete').get_json(), {'saved': False})
        self.assertEqual(self.backend.data, {})

    def test_password_never_in_any_response_or_storage(self):
        seen = [self.call('put', json={'username': USER, 'password': PW}), self.call('get'), self.call('delete'),
                self.call('put', json={'username': USER, 'password': ''})]
        for r in seen:
            self.assertNotIn(PW, r.get_data(as_text=True))
            self.assertNotIn(USER, r.get_data(as_text=True))
            self.assertEqual(r.headers['Cache-Control'], 'no-store')
        self.call('put', json={'username': USER, 'password': PW})
        self.assertNotIn(PW, json.dumps(self.backend.data))
        self.assertNotIn(USER, json.dumps(self.backend.data))

    def test_only_signed_in_teachers(self):
        for token in (None, 'tok-pupil', 'garbage'):
            for method in ('get', 'put', 'delete'):
                self.assertEqual(self.call(method, token=token, json={'username': USER, 'password': PW}).status_code, 401)
        self.assertEqual(self.backend.data, {})

    def test_old_shared_key_does_not_open_these_routes(self):
        r = self.c.get('/nigri-login', headers={'X-Sync-Key': 'test-key-123'})
        self.assertEqual(r.status_code, 401)

    def test_teachers_only_touch_their_own_login(self):
        self.call('put', json={'username': USER, 'password': PW})
        self.assertEqual(self.call('get', token='tok-t2').get_json(), {'saved': False})
        self.call('delete', token='tok-t2')
        self.assertTrue(self.call('get').get_json()['saved'])
        # a teacher cannot name another teacher: the id comes only from the verified token
        self.call('put', token='tok-t2', json={'username': 'b.name@example.test', 'password': 'Second-Fake-2', 'uid': T1})
        self.assertEqual(R._store.get_credentials(T1).password, PW)
        self.assertEqual(R._store.get_credentials(T2).password, 'Second-Fake-2')

    def test_bad_requests(self):
        self.assertEqual(self.call('put', data='not json').status_code, 400)
        self.assertEqual(self.call('put', json={'username': USER}).status_code, 400)
        self.assertEqual(self.call('put', json={'username': USER, 'password': 'x' * 400}).status_code, 400)
        self.assertEqual(self.call('put', json={'username': USER, 'password': 'x' * 3000}).status_code, 413)
        self.assertEqual(self.backend.data, {})

    def test_too_many_tries(self):
        codes = [self.call('get').status_code for _ in range(R.LIMIT + 1)]
        self.assertEqual(codes[-1], 429)
        self.assertEqual(self.call('get', token='tok-t2').status_code, 200)

    def test_missing_key_setting_is_a_safe_error(self):
        R._store = None
        with patch.dict(os.environ, {'NIGRI_LOGIN_KEYS': '', 'FIREBASE_DATABASE_URL': 'https://x.example.test'}):
            r = self.call('get')
        self.assertEqual(r.status_code, 500)
        self.assertNotIn('Traceback', r.get_data(as_text=True))

    def test_teacher_check_failure_means_refused(self):
        def boom(uid): raise RuntimeError('database down')
        R._teacher_check = boom
        self.assertEqual(self.call('get').status_code, 401)


class BrowserAccessTests(Base):
    def test_staging_and_live_sites_may_call_and_others_may_not(self):
        for origin, ok in (('https://fun-torah-tools-staging.web.app', True), ('https://funtorahtools.com', True),
                           ('https://evil.example.test', False)):
            r = self.c.options('/nigri-login', headers={'Origin': origin, 'Access-Control-Request-Method': 'PUT',
                                                        'Access-Control-Request-Headers': 'authorization,content-type'})
            self.assertEqual(r.headers.get('Access-Control-Allow-Origin') == origin, ok, origin)

    def test_login_routes_do_not_take_the_browser_slot(self):
        import browser_runtime
        called = []
        original = browser_runtime.browser_slot
        browser_runtime.browser_slot = lambda: called.append(1)
        try:
            self.assertEqual(self.call('get').status_code, 200)
        finally:
            browser_runtime.browser_slot = original
        self.assertEqual(called, [])

    def test_never_reaches_nigri(self):
        text = Path(R.__file__).read_text(encoding='utf-8') + Path(S.__file__).read_text(encoding='utf-8')
        for word in ('nigri_playwright', 'nigrishluchim', 'nigrijewish', 'playwright'):
            self.assertNotIn(word, text.lower())


class ProductionTeacherCheckTests(unittest.TestCase):
    def test_reads_active_flag_from_the_workspace(self):
        class Sess:
            urls = []
            def get(self, url, timeout=None):
                self.urls.append(url)
                return type('R', (), {'status_code': 200, 'json': lambda s: True})()
        sess = Sess()
        backend = S.FirebaseRestBackend('https://fake.example.test', session=sess)
        with patch.object(S, 'FirebaseRestBackend', lambda: backend):
            R._teacher_check = None
            self.assertTrue(R._is_teacher(T1))
        self.assertEqual(sess.urls, ['https://fake.example.test/b3Games/workspaces/b3-2026/teachers/%s/active.json' % T1])

    def test_read_path_refuses_tricks(self):
        backend = S.FirebaseRestBackend('https://fake.example.test', session=object())
        for bad in ('../x', 'a b', '', 'a?b'):
            with self.assertRaises(S.LoginStoreError):
                backend.read_path(bad)


if __name__ == '__main__':
    unittest.main()
