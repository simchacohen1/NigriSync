"""Daily Checklist -> Mashpia sync. Made-up students and passwords; a fake Mashpia; never touches the real site."""
import base64
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
import mashpia_routes as MR
import mashpia_sync as M
import nigri_login_routes as R
import nigri_login_store as S
import app as server

KEY = 'k1:' + base64.b64encode(bytes(range(32))).decode()
T1, T2 = 'teacherUidOne123', 'teacherUidTwo456'
USER, PW = 'faketeacher', 'Fake-Pa55word!-never-real'


def grid():
    cats = [(22001, 'Daily Tzedaka', 0), (22002, 'Shacharis', 0), (22004, 'Mincha', 0), (16006, "Rebbe's Kapitel", 0),
            (11014, 'Listen to Parents', 1), (13002, 'נעגל וואסער', 0)]
    missions = [{'grid_id': g, 'cat': c, 'disabled': d} for g, c, d in cats]
    soldiers = [
        {'user_id': 1, 'name': 'Arik Traxler', 'registered': True, 'absent': False, 'marks': {}},
        {'user_id': 2, 'name': 'Moshe Lapine', 'registered': True, 'absent': False,
         'marks': {'22002': {'mark_date': 1, 'done_qty': 1}}},
        {'user_id': 3, 'name': 'Chaim Chaikin', 'registered': False, 'absent': False, 'marks': {}},
        {'user_id': 4, 'name': 'Levi Rozmarin', 'registered': True, 'absent': True, 'marks': {}},
        {'user_id': 5, 'name': 'Sam Twin', 'registered': True, 'absent': False, 'marks': {}},
        {'user_id': 6, 'name': 'Twin Sam', 'registered': True, 'absent': False, 'marks': {}},
    ]
    return {'missions': missions, 'soldiers': soldiers}


class FakeMashpia:
    def __init__(self, ok_login=True, fail_users=()):
        self.ok_login, self.fail_users, self.marks, self.logins = ok_login, set(fail_users), [], []

    def login(self, u, p):
        self.logins.append(u)
        if not self.ok_login: raise M.MashpiaError('Mashpia did not accept that username and password.')

    def grid(self, jdn): return grid()

    def mark(self, ids, gid, jdn):
        self.marks.append((gid, list(ids), jdn))
        return {str(i) for i in ids if i not in self.fail_users}


class LogicTests(unittest.TestCase):
    def test_julian_day_matches_site(self):
        self.assertEqual(M.julian_day('2026-11-18'), 2461363)

    def test_matching_and_skips(self):
        students = [
            {'name': 'Traxler Arik', 'items': ['Daily Tzedaka', 'shacharis', 'Rebbes Kapitel', 'נעגל וואסער', 'Homework']},
            {'name': 'Moshe Lapine', 'items': ['Shacharis', 'Mincha']},
            {'name': 'Chaim Chaikin', 'items': ['Mincha']}, {'name': 'Levi Rozmarin', 'items': ['Mincha']},
            {'name': 'Sam Twin', 'items': ['Mincha']}, {'name': 'Nobody Here', 'items': ['Mincha']}]
        p = M.plan(grid(), students)
        self.assertEqual(p['sends'], {22001: [1], 22002: [1], 16006: [1], 13002: [1], 22004: [2]})  # Shacharis for #2 already checked
        self.assertEqual(p['greyed'], ['Chaim Chaikin', 'Levi Rozmarin'])
        self.assertEqual(p['not_found'], ['Sam Twin', 'Nobody Here'])   # twin names are ambiguous: never guess
        self.assertEqual(p['missing_items'], ['Homework'])

    def test_disabled_item_never_matched(self):
        p = M.plan(grid(), [{'name': 'Arik Traxler', 'items': ['Listen to Parents']}])
        self.assertEqual(p['sends'], {})

    def test_only_checks_never_unchecks(self):
        c = FakeMashpia()
        M.sync_day(c, '2026-11-18', [{'name': 'Arik Traxler', 'items': ['Mincha']}, {'name': 'Moshe Lapine', 'items': []}])
        self.assertEqual(c.marks, [(22004, [1], 2461363)])

    def test_failed_save_reported(self):
        r = M.sync_day(FakeMashpia(fail_users={1}), '2026-11-18', [{'name': 'Arik Traxler', 'items': ['Mincha']}])
        self.assertEqual((r['synced'], r['not_found']), (0, ['Arik Traxler']))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.backend = S.MemoryBackend()
        MR._store = S.NigriLoginStore(self.backend, S.load_keyring(KEY), purpose='mashpia-login')
        self.fake = FakeMashpia()
        MR._client_factory = lambda: self.fake
        R._teacher_check = lambda uid: uid in {T1, T2}
        R._hits.clear()
        uids = {'tok-t1': T1, 'tok-t2': T2}
        def fake_verify(token):
            if token not in uids: raise ValueError('bad')
            return {'sub': uids[token], 'email_verified': True}
        self.patches = [patch.object(R, '_verify_token', fake_verify), patch.dict(os.environ, {'MASHPIA_SYNC_ENABLED': '1'})]
        for p in self.patches: p.start()
        self.c = server.app.test_client()

    def tearDown(self):
        for p in self.patches: p.stop()
        MR._store = MR._client_factory = R._teacher_check = None

    def call(self, method, path, token='tok-t1', **kw):
        return getattr(self.c, method)(path, headers={'Authorization': 'Bearer ' + token} if token else {}, **kw)

    SYNC = {'date': '2026-11-18', 'students': [{'name': 'Arik Traxler', 'items': ['Mincha']}]}

    def test_off_and_unsigned(self):
        self.assertEqual(self.call('post', '/mashpia-sync', token=None, json=self.SYNC).status_code, 401)
        with patch.dict(os.environ, {'MASHPIA_SYNC_ENABLED': '0'}):
            self.assertEqual(self.call('post', '/mashpia-sync', json=self.SYNC).status_code, 404)

    def test_needs_login_then_one_press(self):
        r = self.call('post', '/mashpia-sync', json=self.SYNC)
        self.assertEqual((r.status_code, r.get_json()['code']), (409, 'need_login'))
        self.assertEqual(self.call('put', '/mashpia-login', json={'username': USER, 'password': PW}).status_code, 200)
        r = self.call('post', '/mashpia-sync', json=self.SYNC)
        self.assertEqual((r.status_code, r.get_json()['synced']), (200, 1))
        self.assertEqual(self.fake.logins, [USER, USER])

    def test_wrong_password_is_not_saved(self):
        self.fake.ok_login = False
        r = self.call('put', '/mashpia-login', json={'username': USER, 'password': PW})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.backend.data, {})

    def test_password_never_leaks_and_stored_locked(self):
        seen = [self.call('put', '/mashpia-login', json={'username': USER, 'password': PW}), self.call('get', '/mashpia-login'),
                self.call('post', '/mashpia-sync', json=self.SYNC)]
        for r in seen:
            self.assertNotIn(PW, r.get_data(as_text=True))
        self.assertNotIn(PW, str(self.backend.data))

    def test_other_teacher_and_nigri_cannot_open_it(self):
        self.call('put', '/mashpia-login', json={'username': USER, 'password': PW})
        self.assertEqual(self.call('get', '/mashpia-login', token='tok-t2').get_json(), {'saved': False})
        record = self.backend.data[T1]
        with self.assertRaises(S.LoginStoreError):
            S.decrypt_login(T1, record, S.load_keyring(KEY))              # as a Nigri login: refused
        with self.assertRaises(S.LoginStoreError):
            S.decrypt_login(T2, record, S.load_keyring(KEY), 'mashpia-login')  # under another teacher: refused

    def test_bad_requests(self):
        self.assertEqual(self.call('post', '/mashpia-sync', json={'date': '2026-11-18', 'students': []}).status_code, 400)
        self.assertEqual(self.call('put', '/mashpia-login', json={'username': USER}).status_code, 400)


if __name__ == '__main__':
    unittest.main()
