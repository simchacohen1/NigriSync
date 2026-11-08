"""Schools: only the owner and teachers whose school has "Syncs to Nigri" on may use Nigri (issue 550, B3-games#631).
Made-up teachers, passwords and schools; fake database; never touches Nigri or Firebase."""
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
import nigri_login_routes as R
import nigri_login_store as S
import nigri_school_access as A
import app as server

KEY = 'k1:' + base64.b64encode(bytes(range(32))).decode()
WS = 'b3Games/workspaces/b3-2026'
ON, OFF, PRIN, ALIEN, NOSCHOOL, MOVED = 'uidOnSchool0001', 'uidOffSchool002', 'uidPrincipal003', 'uidWrongInvite4', 'uidNoSchool0005', 'uidMovedTeach06'
OWNER = 'uidOwnerAcct007'
USER, PW = 'faketeacher@example.test', 'Fake-Pa55word!-never-real'


def claims_for(uid):
    email = {OWNER: 'simcha5770@gmail.com'}.get(uid, uid.lower() + '@example.test')
    return {'sub': uid, 'email': email, 'email_verified': True}


def teacher(uid, **invite):
    key = A.invite_key(uid.lower() + '@example.test')
    return {WS + '/teachers/' + uid: {'active': True, 'inviteId': key},
            WS + '/teacherInvites/' + key: {'email': uid.lower() + '@example.test', 'active': True, **invite}}


def fake_db(reset_at=2000):
    data = {WS + '/schools/negri_shluchim': {'name': 'Negri Shluchim Online School', 'syncsToNigri': True},
            WS + '/schools/day_school': {'name': 'Example Day School', 'syncsToNigri': False}}
    data.update(teacher(ON, schoolId='negri_shluchim'))
    data.update(teacher(OFF, schoolId='day_school'))
    data.update(teacher(PRIN, schoolId='negri_shluchim', accountType='principal'))
    data.update(teacher(NOSCHOOL))
    data.update(teacher(MOVED, schoolId='negri_shluchim', nigriLoginResetAt=reset_at))
    # ALIEN's teacher record points at somebody else's invitation.
    data[WS + '/teachers/' + ALIEN] = {'active': True, 'inviteId': A.invite_key(ON.lower() + '@example.test')}
    return data


class RuleTests(unittest.TestCase):
    def check(self, uid, data=None, claims=None):
        db = fake_db() if data is None else data
        return A.nigri_access(db.get, 'b3-2026', uid, claims or claims_for(uid))

    def test_owner_always_allowed_without_any_records(self):
        self.assertEqual(self.check(OWNER, data={})['reason'], 'owner')

    def test_owner_email_must_be_verified(self):
        self.assertFalse(self.check(OWNER, data={}, claims={**claims_for(OWNER), 'email_verified': False})['allowed'])

    def test_teacher_in_a_school_that_syncs(self):
        r = self.check(ON)
        self.assertEqual((r['allowed'], r['school_id']), (True, 'negri_shluchim'))

    def test_school_off_no_school_principal_are_refused(self):
        self.assertEqual(self.check(OFF)['reason'], 'school_off')
        self.assertEqual(self.check(NOSCHOOL)['reason'], 'no_school')
        self.assertEqual(self.check(PRIN)['reason'], 'principal')

    def test_someone_elses_invitation_is_refused(self):
        self.assertEqual(self.check(ALIEN)['reason'], 'not_teacher')

    def test_inactive_teacher_or_invitation_is_refused(self):
        db = fake_db()
        db[WS + '/teachers/' + ON]['active'] = False
        self.assertFalse(self.check(ON, db)['allowed'])
        db = fake_db()
        db[WS + '/teacherInvites/' + A.invite_key(ON.lower() + '@example.test')]['active'] = False
        self.assertFalse(self.check(ON, db)['allowed'])

    def test_sync_must_be_exactly_true_and_school_must_exist(self):
        db = fake_db()
        db[WS + '/schools/negri_shluchim']['syncsToNigri'] = 'yes'
        self.assertEqual(self.check(ON, db)['reason'], 'school_off')
        del db[WS + '/schools/negri_shluchim']
        self.assertEqual(self.check(ON, db)['reason'], 'school_off')

    def test_reset_marker_is_reported(self):
        self.assertEqual(self.check(MOVED)['reset_at'], 2000)

    def test_login_was_reset(self):
        self.assertTrue(A.login_was_reset({'updatedAt': 1000}, 2000))
        self.assertFalse(A.login_was_reset({'updatedAt': 3000}, 2000))
        self.assertFalse(A.login_was_reset({'updatedAt': 1000}, None))
        self.assertTrue(A.login_was_reset({}, 2000))


class RouteTests(unittest.TestCase):
    """The real school check (no stand-in), against the fake database."""
    def setUp(self):
        self.backend = S.MemoryBackend()
        self.store = S.NigriLoginStore(self.backend, S.load_keyring(KEY))
        R._store = self.store
        R._teacher_check = lambda uid: uid in (ON, OFF, PRIN, ALIEN, NOSCHOOL, MOVED)
        R._school_check = None
        self.db = fake_db()
        R._reader = self.db.get
        R._hits.clear()
        self.patches = [patch.object(R, '_verify_token', claims_for), patch.dict(os.environ, {'NIGRI_LOGIN_ENABLED': '1'})]
        for p in self.patches: p.start()
        self.c = server.app.test_client()

    def tearDown(self):
        for p in self.patches: p.stop()
        R._store = R._teacher_check = R._school_check = R._reader = None

    def call(self, method, uid, path='/nigri-login', **kw):
        return getattr(self.c, method)(path, headers={'Authorization': 'Bearer ' + uid}, **kw)

    def test_teacher_in_syncing_school_can_save_and_read(self):
        self.assertEqual(self.call('put', ON, json={'username': USER, 'password': PW}).status_code, 200)
        self.assertEqual(self.call('get', ON).get_json()['saved'], True)

    def test_teacher_in_school_with_sync_off_is_refused_but_keeps_his_login(self):
        self.store.save(OFF, USER, PW)
        for method, kw in (('get', {}), ('put', {'json': {'username': USER, 'password': PW}})):
            r = self.call(method, OFF, **kw)
            self.assertEqual(r.status_code, 403)
            self.assertEqual(r.get_json(), {'error': 'Nigri sync is not turned on for your school.', 'code': 'school_off'})
        self.assertIn(OFF, self.backend.data)   # kept, but unusable until the owner turns sync back on
        self.assertEqual(self.call('get', OFF, path='/nigri-courses?class=et').status_code, 403)

    def test_teacher_in_school_with_sync_off_may_still_remove_his_login(self):
        self.store.save(OFF, USER, PW)
        self.assertEqual(self.call('delete', OFF).status_code, 200)
        self.assertNotIn(OFF, self.backend.data)

    def test_turning_sync_back_on_lets_him_use_the_kept_login(self):
        self.store.save(OFF, USER, PW)
        self.db[WS + '/schools/day_school']['syncsToNigri'] = True
        self.assertEqual(self.call('get', OFF).get_json()['saved'], True)

    def test_principal_and_no_school_are_refused(self):
        self.assertEqual(self.call('get', PRIN).get_json()['code'], 'principal')
        self.assertEqual(self.call('get', NOSCHOOL).get_json()['code'], 'no_school')

    def test_teacher_pointing_at_someone_elses_invitation_is_unauthorized(self):
        self.assertEqual(self.call('get', ALIEN).status_code, 401)

    def test_owner_can_save_his_own_login_without_a_teacher_record(self):
        self.assertEqual(self.call('put', OWNER, json={'username': USER, 'password': PW}).status_code, 200)
        self.assertIn(OWNER, self.backend.data)

    def test_moved_teacher_loses_a_login_saved_before_the_move(self):
        self.store.save(MOVED, USER, PW, now=1)          # updatedAt 1000 ms, before the move at 2000
        self.assertEqual(self.call('get', MOVED).get_json(), {'saved': False})
        self.assertNotIn(MOVED, self.backend.data)
        # Saving again after the move works and is kept.
        self.assertEqual(self.call('put', MOVED, json={'username': USER, 'password': PW}).status_code, 200)
        self.assertEqual(self.call('get', MOVED).get_json()['saved'], True)

    def test_database_failure_refuses(self):
        def boom(path): raise RuntimeError('firebase down')
        R._reader = boom
        r = self.call('get', ON)
        self.assertEqual(r.status_code, 500)
        self.assertNotIn('firebase', r.get_data(as_text=True))

    def test_password_never_in_any_response(self):
        self.store.save(OFF, USER, PW)
        for uid in (ON, OFF, PRIN, MOVED, OWNER):
            for method in ('get', 'delete'):
                self.assertNotIn(PW, self.call(method, uid).get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
