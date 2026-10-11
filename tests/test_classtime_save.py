"""The real save route, tested with a STAND-IN for run_marks_sync: nothing here can reach Nigri. Made-up names only."""
import base64
import os
import sys
import tempfile
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['SYNC_API_KEY'] = 'test-key-123'
os.environ['CLASSTIME_TEST_TOKEN'] = 'diagnostic-abc'
os.environ['CLASSTIME_REVIEW_OWNER_EMAIL'] = 'owner@example.test'
sys.modules.setdefault('fcntl', types.ModuleType('fcntl'))


class _Stubs(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith('__'): raise AttributeError(name)
        return lambda *a, **k: {}


class SyncError(Exception):
    def __init__(self, message, partial_results=None):
        super().__init__(message); self.partial_results = partial_results or []


STATE = {'saves': [], 'titles': {'titles': ['Some Other Quiz'], 'discovered_ids': 1}, 'save_behavior': None, 'limits': [], 'browser_error': None}


def stand_in_save(**kwargs):
    STATE['saves'].append(kwargs)
    if STATE['save_behavior']: return STATE['save_behavior'](kwargs)
    lines = ['Mark created (testID=4242)'] + [s['name'] + ': filled (attendance=' + s.get('attendance', 'present') + ')' for s in kwargs['students']] + ['Final Save! submitted']
    return {'test_id': '4242', 'results': lines}


def list_titles():
    value = STATE['titles']
    if isinstance(value, Exception): raise value
    return value


stub = sys.modules.setdefault('nigri_playwright', _Stubs('nigri_playwright'))
sys.modules.setdefault('attendance_reader', _Stubs('attendance_reader'))
stub.MARKS_TOPIC_IDS = {'Chumash': '10', 'Shoroshim': '99'}
stub.REWARDS_CHILD_IDS = {}
stub._MARKS_CLASS_STUDENTS = {'B3 ET': [], 'B3 WT': []}
stub.SyncError = SyncError
stub.run_marks_sync = stand_in_save
stub.list_mark_titles = list_titles
import browser_runtime


@contextmanager
def _no_slot(): yield


def fake_run_browser(target, *args, **kwargs):
    STATE['limits'].append(kwargs.pop('_time_limit', None))
    if STATE['browser_error'] and getattr(target, '__name__', '') == 'save_job': raise STATE['browser_error']
    return target(*args, **kwargs)


browser_runtime.browser_slot = _no_slot
browser_runtime.run_browser = fake_run_browser
import app as server
import classtime_review as cr
import classtime_save as save

PDF = b'%PDF-1.4 made-up report'
AUTH = {'Authorization': 'Bearer t'}


def student(name, points, matched=True, pdf='verified'):
    return {'classtime_name': name, 'points': points, 'maximum_points': 12.0, 'pdf_status': pdf, 'pdf_id': name.lower(),
            'nigri': {'name': name + ' Nigri', 'child_id': name, 'class_section': 'B3 ET'} if matched else None}


def claims(email='owner@example.test'):
    return {'iss': 'https://securetoken.google.com/b3-games', 'email': email, 'email_verified': True, 'sub': 'uid1'}


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.addCleanup(self.dir.cleanup)
        self.pdfs = {}
        for key in ('ann', 'ben', 'cal', 'dee'):
            path = Path(self.dir.name) / (key + '.pdf'); path.write_bytes(PDF); self.pdfs[key] = str(path)
        cr.JOBS.clear(); save.LEDGER.clear()
        STATE.update(saves=[], titles={'titles': ['Some Other Quiz'], 'discovered_ids': 1}, save_behavior=None, limits=[], browser_error=None)
        self.add_review('rev1')
        previous_pause = server.CLASSTIME_SAVES_PAUSED
        server.CLASSTIME_SAVES_PAUSED = False  # Mock-only; no real Nigri writes.
        self.addCleanup(setattr, server, 'CLASSTIME_SAVES_PAUSED', previous_pause)
        self.client = server.app.test_client()
        patcher = patch('google.oauth2.id_token.verify_firebase_token', return_value=claims()); patcher.start(); self.addCleanup(patcher.stop)
        self.addCleanup(cr.JOBS.clear)
    def add_review(self, rid, students=None, code='ABC123'):
        cr.JOBS[rid] = {'owner': 'uid1', 'expires': time.time() + 3600, 'status': 'ready', 'pdfs': self.pdfs,
                        'result': {'session_code': code, 'session_name': 'Made Up Quiz', 'class_section': 'B3 ET',
                                   'students': students or [student('Ann', 9), student('Ben', None), student('Cal', 7, matched=False), student('Dee', 12)]}}
    def post(self, body=None, rid='rev1', headers=AUTH):
        base = {'topic': 'Shoroshim', 'date': '2026-10-09'}
        return self.client.post('/classtime/reviews/' + rid + '/sync-save', json=base if body is None else {**base, **body}, headers=headers)
    def state(self, rid='rev1'): return (cr.JOBS[rid].get('save') or {}).get('state')

    def test_new_production_pause_blocks_all_classtime_writes(self):
        server.CLASSTIME_SAVES_PAUSED = True
        response = self.post()
        self.assertEqual(response.status_code, 423)
        self.assertFalse(response.get_json()['saved'])
        self.assertEqual(STATE['saves'], [])
        self.assertIsNone(self.state())

    # ---- who may call it ----
    def test_only_the_owner_google_sign_in_works(self):
        self.assertEqual(self.post(headers={}).status_code, 401)
        self.assertEqual(self.post(headers={'X-Sync-Key': 'test-key-123'}).status_code, 401)
        def only_real_tokens(token, *args, **kwargs):
            if token == 'diagnostic-abc': raise ValueError('not a Google token')
            return claims()
        with patch('google.oauth2.id_token.verify_firebase_token', side_effect=only_real_tokens):
            self.assertEqual(self.post(headers={'Authorization': 'Bearer diagnostic-abc'}).status_code, 401)
        with patch('google.oauth2.id_token.verify_firebase_token', return_value=claims('other@example.test')):
            self.assertEqual(self.post().status_code, 401)
        self.assertEqual(STATE['saves'], []); self.assertIsNone(self.state())

    # ---- the normal full save ----
    def test_full_save_rebuilds_rows_on_the_server_and_attaches_pdfs(self):
        response = self.post({'students': [{'name': 'Hacker', 'mark': 100}], 'report_base64': 'AAAA', 'rows': 'x', 'mark_all_done': False})
        data = response.get_json()
        self.assertEqual(response.status_code, 200); self.assertTrue(data['saved']); self.assertEqual(data['test_id'], '4242'); self.assertTrue(data['marking_done'])
        self.assertEqual(len(STATE['saves']), 1); call = STATE['saves'][0]
        self.assertEqual((call['class_section'], call['topic'], call['mark_type'], call['test_date']), ('B3 ET', 'Shoroshim', 'quiz', '2026-10-09'))
        self.assertEqual(call['test_name'], 'Made Up Quiz (ABC123)'); self.assertTrue(call['mark_all_done'])
        self.assertTrue(call['classtime_memory_saver'])  # Only Classtime saves use lighter Chromium
        rows = {s['name']: s for s in call['students']}
        self.assertEqual(set(rows), {'Ann Nigri', 'Ben Nigri', 'Dee Nigri'})            # unmatched boy never sent; browser rows ignored
        self.assertEqual((rows['Ann Nigri']['mark'], rows['Dee Nigri']['mark']), (75, 100))
        self.assertIsNone(rows['Ben Nigri']['mark']); self.assertEqual(rows['Ben Nigri']['attendance'], 'review')   # no score: blank, never 0
        self.assertEqual(base64.b64decode(rows['Ann Nigri']['report_base64']), PDF); self.assertNotIn('report_base64', rows['Ben Nigri'])
        outcomes = {b['classtime_name']: b['outcome'] for b in data['per_boy']}
        self.assertEqual(outcomes, {'Ann': 'saved', 'Ben': 'left blank', 'Cal': 'skipped', 'Dee': 'saved'})
        self.assertEqual(self.state(), 'saved'); self.assertEqual(STATE['limits'][-1], 420)

    # ---- the lock and the second save ----
    def test_second_save_of_the_same_review_is_refused(self):
        self.assertEqual(self.post().status_code, 200)
        again = self.post(); self.assertEqual(again.status_code, 409); self.assertIn('already saved', again.get_json()['error'])
        self.assertEqual(len(STATE['saves']), 1)
        # even the override cannot save the same review twice
        forced = self.post({'allow_duplicate': True, 'confirm_session_code': 'ABC123', 'confirm_test_name': 'Made Up Quiz (ABC123)'})
        self.assertEqual(forced.status_code, 409); self.assertEqual(len(STATE['saves']), 1)
    def test_a_second_click_while_saving_is_refused(self):
        cr.JOBS['rev1']['save'] = {'state': 'saving'}
        out = self.post(); self.assertEqual(out.status_code, 409); self.assertIn('already running', out.get_json()['error']); self.assertEqual(STATE['saves'], [])
    def test_a_click_during_the_save_itself_is_refused(self):
        seen = {}
        def during(kwargs):
            seen['second'] = self.post(); return {'test_id': '1', 'results': ['Mark created (testID=1)', 'Ann Nigri: filled (attendance=present)', 'Final Save! submitted']}
        STATE['save_behavior'] = during
        self.assertEqual(self.post({'only_student': 'Ann Nigri'}).status_code, 200)
        self.assertEqual(seen['second'].status_code, 409); self.assertEqual(len(STATE['saves']), 1)
    def test_refusals_before_saving_release_the_lock(self):
        self.assertEqual(self.post({'topic': 'Nope'}).status_code, 400); self.assertIsNone(self.state())
        self.assertEqual(self.post().status_code, 200)

    # ---- one-boy option ----
    def test_one_boy_save_is_named_test_and_never_marks_done(self):
        data = self.post({'only_student': 'Ann Nigri'}).get_json()
        call = STATE['saves'][0]
        self.assertEqual([s['name'] for s in call['students']], ['Ann Nigri']); self.assertFalse(call['mark_all_done'])
        self.assertTrue(call['test_name'].startswith('TEST DELETE ME')); self.assertIn('ABC123', call['test_name'])
        self.assertTrue(data['one_boy']); self.assertFalse(data['marking_done']); self.assertTrue(data['mark_name'].startswith('TEST DELETE ME'))
        self.assertEqual([(b['classtime_name'], b['outcome']) for b in data['per_boy']], [('Ann', 'saved')])   # only the chosen boy is reported
    def test_one_boy_choices_are_checked(self):
        for bad in ('Nobody Nigri', 'Ben Nigri', 'Cal Nigri', 7, ''):
            self.assertEqual(self.post({'only_student': bad}).status_code, 400, bad)
        self.assertEqual(STATE['saves'], []); self.assertIsNone(self.state())
    def test_one_boy_ignores_other_boys_report_problems(self):
        self.add_review('rev1', [student('Ann', 9), student('Dee', 12, pdf='missing')])
        self.assertEqual(self.post({'only_student': 'Ann Nigri'}).status_code, 200)
    def test_custom_name_keeps_the_test_prefix(self):
        self.post({'only_student': 'Ann Nigri', 'test_name': 'My Name (ABC123)'}); self.assertEqual(STATE['saves'][0]['test_name'], 'TEST DELETE ME - My Name (ABC123)')

    # ---- duplicate check and the override ----
    def override(self, **extra): return {'allow_duplicate': True, 'confirm_session_code': 'ABC123', 'confirm_test_name': 'Made Up Quiz (ABC123)', **extra}
    def test_save_never_launches_the_nigri_duplicate_lookup(self):
        with patch.object(stub, 'list_mark_titles', side_effect=AssertionError('duplicate browser must not run')) as lookup:
            out = self.post({'only_student': 'Ann Nigri'})
        self.assertEqual(out.status_code, 200)
        lookup.assert_not_called()
        self.assertEqual(len(STATE['saves']), 1)
        self.assertEqual(STATE['saves'][0]['test_name'], 'TEST DELETE ME - Made Up Quiz (ABC123)')

    def test_pdf_problems_and_no_scores_can_never_be_overridden(self):
        STATE['titles'] = {'titles': ['Made Up Quiz (ABC123)'], 'discovered_ids': 1}
        self.add_review('rev1', [student('Ann', 9, pdf='missing'), student('Dee', 12)])
        out = self.post(self.override()); self.assertEqual(out.status_code, 409); self.assertFalse(out.get_json()['can_override']); self.assertIn('not verified', out.get_json()['error'])
        self.add_review('rev1', [student('Ben', None)])
        out = self.post(self.override()); self.assertEqual(out.status_code, 409); self.assertFalse(out.get_json()['can_override'])
        self.assertEqual(STATE['saves'], []); self.assertIsNone(self.state())
    def test_a_session_saved_earlier_by_this_server_counts_as_a_duplicate(self):
        self.assertEqual(self.post().status_code, 200)
        self.add_review('rev2')                      # the same session loaded again
        out = self.post(rid='rev2'); self.assertEqual(out.status_code, 409); self.assertEqual(out.get_json()['duplicate_check']['status'], 'duplicate')
        self.assertEqual(self.post(self.override(), rid='rev2').status_code, 200); self.assertEqual(len(STATE['saves']), 2)

    # ---- a save that stops partway: never retried, always says a partial mark may exist ----
    def assert_partial_and_locked(self, out, rid='rev1'):
        data = out.get_json()
        self.assertTrue(data['partial_mark_may_exist']); self.assertTrue(data['do_not_retry']); self.assertFalse(data['saved'])
        self.assertNotIn('No Nigri data was changed', str(data))
        before = len(STATE['saves']); again = self.post(rid=rid)
        self.assertEqual(again.status_code, 409); self.assertIn('will not be retried', again.get_json()['error']); self.assertEqual(len(STATE['saves']), before)
        return data
    def test_failed_boy_after_the_mark_was_saved_is_reported_per_boy(self):
        STATE['save_behavior'] = lambda k: {'test_id': '4242', 'results': ['Mark created (testID=4242)', 'Ann Nigri: filled (attendance=present)', 'Ben Nigri: FAILED - field not found', 'Dee Nigri: filled (attendance=present)', 'Final Save! submitted']}
        out = self.post(); data = self.assert_partial_and_locked(out)
        self.assertEqual(out.status_code, 200); self.assertIn('Partly saved', data['message']); self.assertEqual(data['test_id'], '4242')
        self.assertEqual({b['classtime_name']: b['outcome'] for b in data['per_boy']}['Ben'], 'failed'); self.assertEqual(self.state(), 'partial')
    def test_nigri_error_partway_reports_the_mark_number_and_is_not_retried(self):
        def fail(k): raise SyncError('Could not select topic', partial_results=['Mark created (testID=77)', 'Ann Nigri: filled (attendance=present)'])
        STATE['save_behavior'] = fail
        out = self.post(); data = self.assert_partial_and_locked(out)
        self.assertEqual(out.status_code, 500); self.assertEqual(data['test_id'], '77'); self.assertIn('77', data['message'])
        self.assertEqual({b['classtime_name']: b['outcome'] for b in data['per_boy']}['Dee'], 'not reached')
    def test_unexpected_error_in_the_save_is_treated_as_partial(self):
        def boom(k): raise RuntimeError('something odd')
        STATE['save_behavior'] = boom
        out = self.post(); self.assert_partial_and_locked(out); self.assertEqual(out.status_code, 500)
    def test_memory_stop_timeout_and_exit_never_claim_nothing_changed(self):
        cases = {'memory': 'Classtime retrieval stopped safely before the service memory limit. Restore a saved review ZIP instead. No Nigri data was changed.',
                 'longer than the time limit': 'Classtime retrieval timed out and its browser was stopped. No Nigri data was changed.',
                 'ended unexpectedly': 'Classtime browser exited. No Nigri data was changed.'}
        for expect, text in cases.items():
            cr.JOBS.clear(); save.LEDGER.clear(); self.add_review('rev1'); STATE['saves'] = []
            STATE['browser_error'] = ValueError(text)
            out = self.post(); data = self.assert_partial_and_locked(out)
            self.assertEqual(out.status_code, 500); self.assertIn(expect, data['message']); self.assertIn('MAY exist', data['message']); self.assertEqual(self.state(), 'unknown')
    def test_busy_server_means_nothing_was_written_and_a_retry_is_allowed(self):
        STATE['browser_error'] = ValueError('Another browser operation is running. Please try again after it finishes.')
        out = self.post(); self.assertEqual(out.status_code, 409); self.assertFalse(out.get_json().get('partial_mark_may_exist', False)); self.assertIsNone(self.state())
        STATE['browser_error'] = None; self.assertEqual(self.post().status_code, 200)
    def test_time_limit_setting_is_bounded(self):
        for value, expected in (('600', 600), ('5', 120), ('99999', 900), ('abc', 420)):
            with patch.dict(os.environ, {'CLASSTIME_SAVE_TIME_LIMIT': value}): self.assertEqual(save.save_time_limit(), expected)

    # ---- the rest of the service ----
    def test_missing_expired_and_unready_reviews(self):
        self.assertEqual(self.post(rid='nope').status_code, 404)
        cr.JOBS['rev1']['expires'] = time.time() - 1; self.assertEqual(self.post().status_code, 404)
        cr.JOBS['loading'] = {'owner': 'uid1', 'expires': time.time() + 60, 'status': 'loading', 'pdfs': {}}; self.assertEqual(self.post(rid='loading').status_code, 409)
        cr.JOBS['rev1']['owner'] = 'someone-else'; cr.JOBS['rev1']['expires'] = time.time() + 60; self.assertEqual(self.post().status_code, 404)
        self.assertEqual(STATE['saves'], [])
    def test_sync_marks_and_the_preview_are_unchanged(self):
        self.assertEqual(self.client.post('/sync-marks', json={}).status_code, 401)
        self.assertNotEqual(self.client.post('/sync-marks', json={}, headers={'X-Sync-Key': 'test-key-123'}).status_code, 401)
        preview = self.client.post('/classtime/reviews/rev1/sync-preview', json={'topic': 'Shoroshim', 'date': '2026-10-09'}, headers=AUTH)
        self.assertEqual(preview.status_code, 200); self.assertFalse(preview.get_json()['nigri_writes']); self.assertEqual(STATE['saves'], [])
    def test_only_the_real_save_function_is_used(self):
        text = (Path(__file__).resolve().parents[1] / 'classtime_save.py').read_text(encoding='utf-8')
        for forbidden in ('debug_marks', 'del_test', 'delete_after', 'page.'): self.assertNotIn(forbidden, text)


if __name__ == '__main__':
    unittest.main()
