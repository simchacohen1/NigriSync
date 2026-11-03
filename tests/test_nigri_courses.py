"""Grade + course-ID matching (issue 550, step 3). Mock Nigri course list, made-up teachers; never touches Nigri."""
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
import nigri_courses as N
import nigri_login_routes as R
import nigri_login_store as S
import app as server

WS = 'b3-2026'
T1, T2 = 'teacherUidOne123', 'teacherUidTwo456'
KEY = 'k1:' + base64.b64encode(bytes(range(32))).decode()


def db(**extra):
    """A made-up workspace: T1 teaches et, wt and a custom class; T2 teaches only 'other'."""
    base = 'b3Games/workspaces/%s/' % WS
    data = {
        base + 'teachers/%s/active' % T1: True, base + 'teachers/%s/classIds/et' % T1: True,
        base + 'teachers/%s/classIds/wt' % T1: True, base + 'teachers/%s/classIds/custom' % T1: True,
        base + 'teachers/%s/classIds/nograde' % T1: True, base + 'teachers/%s/classIds/off' % T1: True,
        base + 'teachers/%s/active' % T2: True, base + 'teachers/%s/classIds/other' % T2: True,
        base + 'classes/off/active': False, base + 'classes/custom/nigriGrade': 'B3 WT',
        base + 'classes/nograde/nigriGrade': 'Grade 4',
    }
    data.update({base + k: v for k, v in extra.items()})
    return lambda path: data.get(path)


ET_ALL = [{'id': i, 'name': 'B3 ET course %d' % i} for i in N.COURSES['B3 ET'].values()]
WT_ALL = [{'id': i, 'name': 'B3 WT course %d' % i} for i in N.COURSES['B3 WT'].values()]
ROOMS = [{'id': 90001, 'name': 'Conference Room - Boys'}, {'id': 90002, 'name': 'Rally Room - Boys'}]
CREDS = S.Credentials('fake@example.test', 'Fake-Pw-1')


def status(class_id, visible, uid=T1, creds=CREDS, read=None):
    return N.course_status(read or db(), WS, uid, class_id, creds, lambda c: visible)


def avail(result):
    return {c['slot']: c['available'] for c in result['courses']}


class TableTests(unittest.TestCase):
    def test_table_matches_the_owner_export(self):
        self.assertEqual(N.COURSES['B3 ET'], {'davening': 28734, 'morning1': 28762, 'morning2': 28818, 'morning3': 28819,
                                              'friday1': 28910, 'friday2': 28948, 'friday3': 28947})
        self.assertEqual(N.COURSES['B3 WT'], {'davening': 28733, 'morning1': 28722, 'morning2': 28814, 'morning3': 28815})

    def test_no_course_id_is_shared_between_grades_and_none_is_a_room(self):
        ids = [i for g in N.COURSES.values() for i in g.values()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(isinstance(i, int) for i in ids))

    def test_shared_room_names(self):
        self.assertTrue(N.is_shared_room('Conference Room - Boys') and N.is_shared_room('rally room - boys'))
        self.assertFalse(N.is_shared_room('B3 ET Davening'))


class GradeTests(unittest.TestCase):
    def test_et_and_wt_default_to_their_own_grade(self):
        self.assertEqual(status('et', ET_ALL)['grade'], 'B3 ET')
        self.assertEqual(status('wt', WT_ALL)['grade'], 'B3 WT')

    def test_owner_set_grade_for_other_classes(self):
        self.assertEqual(status('custom', WT_ALL)['grade'], 'B3 WT')

    def test_no_grade_or_junk_grade_means_no_sync(self):
        for cid, read in (('nograde', None),):
            with self.assertRaises(N.MatchError) as cm:
                status(cid, ET_ALL)
            self.assertEqual(cm.exception.code, 'no_grade')
        data = db(**{'classes/zzz/nigriGrade': None, 'teachers/%s/classIds/zzz' % T1: True})
        with self.assertRaises(N.MatchError) as cm:   # a class with no grade and no default
            N.course_status(data, WS, T1, 'zzz', CREDS, lambda c: ET_ALL)
        self.assertEqual(cm.exception.code, 'no_grade')


class OwnershipTests(unittest.TestCase):
    def test_not_his_class(self):
        for cid in ('other', 'nope'):
            with self.assertRaises(N.MatchError) as cm:
                status(cid, ET_ALL)
            self.assertEqual(cm.exception.code, 'not_your_class')

    def test_switched_off_class_and_inactive_teacher(self):
        with self.assertRaises(N.MatchError):
            status('off', ET_ALL)
        with self.assertRaises(N.MatchError):
            status('et', ET_ALL, read=db(**{'teachers/%s/active' % T1: False}))

    def test_bad_class_ids(self):
        for cid in ('', '../x', 'a/b', None, 'x' * 100):
            with self.assertRaises(N.MatchError) as cm:
                status(cid, ET_ALL)
            self.assertEqual(cm.exception.code, 'bad_class')


class VisibilityTests(unittest.TestCase):
    def test_all_visible_et_has_seven_courses_wt_has_four(self):
        self.assertEqual(len(status('et', ET_ALL)['courses']), 7)
        self.assertTrue(all(avail(status('et', ET_ALL)).values()))
        self.assertEqual([c['slot'] for c in status('wt', WT_ALL)['courses']], ['davening', 'morning1', 'morning2', 'morning3'])

    def test_only_courses_his_own_login_can_see_are_on(self):
        seen = [{'id': 28734, 'name': 'x'}, {'id': 28762, 'name': 'y'}]
        a = avail(status('et', seen))
        self.assertEqual([s for s, v in a.items() if v], ['davening', 'morning1'])
        off = [c for c in status('et', seen)['courses'] if not c['available']]
        self.assertTrue(all('cannot see' in c['reason'] for c in off))

    def test_other_grades_courses_do_not_turn_on(self):
        self.assertFalse(any(avail(status('et', WT_ALL)).values()))
        self.assertFalse(any(avail(status('wt', ET_ALL)).values()))

    def test_shared_rooms_never_count_even_with_a_table_id(self):
        sneaky = [{'id': 28734, 'name': 'Conference Room - Boys'}, {'id': 28762, 'name': 'Rally Room - Boys'}] + ROOMS
        self.assertFalse(any(avail(status('et', sneaky)).values()))
        self.assertTrue(all(c['courseId'] in N.COURSES['B3 ET'].values() for c in status('et', ROOMS + ET_ALL)['courses']))
        self.assertNotIn(90001, [c['courseId'] for c in status('et', ROOMS + ET_ALL)['courses']])

    def test_ids_are_matched_not_names(self):
        wrong_name = [{'id': 28734, 'name': 'Totally different name'}]
        self.assertTrue(avail(status('et', wrong_name))['davening'])
        right_name_wrong_id = [{'id': 1, 'name': 'B3 ET Davening'}]
        self.assertFalse(avail(status('et', right_name_wrong_id))['davening'])

    def test_junk_in_the_list_is_ignored(self):
        junk = [None, {}, {'id': 'abc'}, {'id': None}, 5, {'id': '28734', 'name': 'ok'}]
        self.assertTrue(avail(status('et', junk))['davening'])

    def test_no_login_saved(self):
        r = status('et', ET_ALL, creds=None)
        self.assertFalse(any(avail(r).values()))
        self.assertIn('Save your Nigri login', r['courses'][0]['reason'])

    def test_course_reader_not_connected_or_failing_keeps_everything_off(self):
        r = N.course_status(db(), WS, T1, 'et', CREDS, N.not_connected_lister)
        self.assertFalse(any(avail(r).values()))
        self.assertIn('not connected yet', r['courses'][0]['reason'])

        def boom(c): raise RuntimeError('secret detail ' + c.password)
        r = N.course_status(db(), WS, T1, 'et', CREDS, boom)
        self.assertFalse(any(avail(r).values()))
        self.assertNotIn('Fake-Pw-1', str(r))
        self.assertNotIn('secret detail', str(r))


class ResolveTests(unittest.TestCase):
    def go(self, class_id, slot, visible, **kw):
        return N.resolve_course(kw.get('read') or db(), WS, T1, class_id, slot, CREDS, lambda c: visible)

    def test_returns_exactly_one_course_id(self):
        self.assertEqual(self.go('et', 'friday2', ET_ALL), {'grade': 'B3 ET', 'slot': 'friday2', 'courseId': 28948})
        self.assertEqual(self.go('wt', 'morning3', WT_ALL)['courseId'], 28815)
        self.assertEqual(self.go('custom', 'davening', WT_ALL)['courseId'], 28733)

    def test_wt_has_no_friday(self):
        with self.assertRaises(N.MatchError) as cm:
            self.go('wt', 'friday1', WT_ALL)
        self.assertEqual(cm.exception.code, 'no_such_course')

    def test_refuses_when_he_cannot_see_it_or_slot_is_junk_or_class_not_his(self):
        for args, code in ((('et', 'davening', []), 'not_visible'), (('et', 'recess', ET_ALL), 'bad_slot'),
                           (('other', 'davening', ET_ALL), 'not_your_class')):
            with self.assertRaises(N.MatchError) as cm:
                self.go(*args)
            self.assertEqual(cm.exception.code, code)

    def test_a_class_can_never_resolve_into_another_grade(self):
        # 'custom' is B3 WT: even if his login sees every B3 ET course, he cannot reach one through it.
        with self.assertRaises(N.MatchError):
            self.go('custom', 'friday1', ET_ALL)
        self.assertNotIn(self.go('custom', 'davening', ET_ALL + WT_ALL)['courseId'], N.COURSES['B3 ET'].values())


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.backend = S.MemoryBackend()
        R._store = S.NigriLoginStore(self.backend, S.load_keyring(KEY))
        R._teacher_check = lambda uid: uid in (T1, T2)
        R._reader = db()
        R._lister = lambda c: ET_ALL + ROOMS
        R._hits.clear()
        self.patches = [patch.object(R, '_verify_token', lambda t: {'sub': {'t1': T1, 't2': T2}[t], 'email_verified': True}),
                        patch.dict(os.environ, {'NIGRI_LOGIN_ENABLED': '1'})]
        for p in self.patches: p.start()
        self.c = server.app.test_client()

    def tearDown(self):
        for p in self.patches: p.stop()
        R._store = R._teacher_check = R._reader = R._lister = None

    def get(self, cls, token='t1'):
        return self.c.get('/nigri-courses?class=' + cls, headers={'Authorization': 'Bearer ' + token} if token else {})

    def test_needs_login_saved_then_shows_courses_without_rooms(self):
        r = self.get('et').get_json()
        self.assertFalse(any(c['available'] for c in r['courses']))
        R._store.save(T1, 'fake@example.test', 'Fake-Pw-1')
        r = self.get('et').get_json()
        self.assertEqual((r['grade'], len(r['courses'])), ('B3 ET', 7))
        self.assertTrue(all(c['available'] for c in r['courses']))
        text = self.get('et').get_data(as_text=True)
        for bad in ('Conference', 'Rally', '90001', 'Fake-Pw-1', 'fake@example.test'):
            self.assertNotIn(bad, text)

    def test_refusals(self):
        self.assertEqual(self.get('et', token=None).status_code, 401)
        self.assertEqual(self.get('other').status_code, 403)
        self.assertEqual(self.get('nograde').status_code, 400)
        self.assertEqual(self.get('../x').status_code, 400)
        self.assertEqual(self.get('et', token='t2').status_code, 403)

    def test_production_default_has_no_nigri_reader_so_everything_stays_off(self):
        R._lister = None
        R._store.save(T1, 'fake@example.test', 'Fake-Pw-1')
        r = self.get('et').get_json()
        self.assertFalse(any(c['available'] for c in r['courses']))
        self.assertIn('not connected yet', r['courses'][0]['reason'])

    def test_unreadable_saved_login_is_treated_as_none(self):
        R._store.save(T1, 'fake@example.test', 'Fake-Pw-1')
        self.backend.data[T1]['ct'] = 'AAAA'
        r = self.get('et')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(any(c['available'] for c in r.get_json()['courses']))

    def test_off_by_default(self):
        with patch.dict(os.environ, {'NIGRI_LOGIN_ENABLED': '0'}):
            self.assertEqual(self.get('et').status_code, 404)

    def test_browser_access_only_for_staging_and_live_sites(self):
        for origin, ok in (('https://fun-torah-tools-staging.web.app', True), ('https://funtorahtools.com', True),
                           ('https://evil.example.test', False)):
            r = self.c.options('/nigri-courses', headers={'Origin': origin, 'Access-Control-Request-Method': 'GET',
                                                          'Access-Control-Request-Headers': 'authorization'})
            self.assertEqual(r.headers.get('Access-Control-Allow-Origin') == origin, ok, origin)

    def test_module_never_reaches_nigri(self):
        text = Path(N.__file__).read_text(encoding='utf-8').lower()
        for word in ('import requests', 'playwright', 'urllib', 'http://', 'https://'):
            self.assertNotIn(word, text)


if __name__ == '__main__':
    unittest.main()
