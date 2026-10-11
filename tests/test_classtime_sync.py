"""Dry run of 'Sync to Nigri' from a Classtime review. Made-up names and values only; no real PDFs."""
import json
import os
import re
import sys
import tempfile
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

CALLS = {'write': 0, 'list': 0}


class _Stubs(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith('__'): raise AttributeError(name)
        if name == 'SyncError': return type('SyncError', (Exception,), {})
        return lambda *a, **k: {}


def _real_write(*a, **k):
    CALLS['write'] += 1
    raise AssertionError('a real Nigri save must never run from the dry run')


def _list_titles():
    CALLS['list'] += 1
    return {'titles': ['Some Other Quiz', 'Class Work 4'], 'discovered_ids': 2}


stub = sys.modules.setdefault('nigri_playwright', _Stubs('nigri_playwright'))
sys.modules.setdefault('attendance_reader', _Stubs('attendance_reader'))
stub.MARKS_TOPIC_IDS = {'Chumash': '10', 'Shoroshim': '99'}
stub.REWARDS_CHILD_IDS = {}
stub._MARKS_CLASS_STUDENTS = {'B3 ET': [], 'B3 WT': []}
stub.run_marks_sync = _real_write
stub.debug_marks_full_flow = _real_write
stub.debug_marks_create_and_view = _real_write
stub.list_mark_titles = _list_titles
import browser_runtime


@contextmanager
def _no_slot(): yield


browser_runtime.browser_slot = _no_slot
browser_runtime.run_browser = lambda target, *a, **k: target(*a, **k)
import app as server
import classtime_sync as cs
import classtime_review as cr

OPTIONS = {'topic': 'Shoroshim', 'type': 'quiz', 'date': '2026-10-09', 'test_name': 'Made Up Quiz (ABC123)', 'description': None}


def student(name, points, maximum=12.0, matched=True, pdf='verified', pdf_id=None):
    return {'classtime_name': name, 'points': points, 'maximum_points': maximum, 'pdf_status': pdf, 'pdf_id': pdf_id,
            'nigri': {'name': name + ' Nigri', 'child_id': name, 'class_section': 'B3 ET'} if matched else None}


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.addCleanup(self.dir.cleanup)
        self.pdfs = {}
        for key in ('a', 'b', 'c'):
            path = Path(self.dir.name) / (key + '.pdf'); path.write_bytes(b'%PDF-1.4 fake ' + key.encode() * 500); self.pdfs[key] = str(path)
    def result(self, *students):
        return {'session_code': 'ABC123', 'session_name': 'Made Up Quiz', 'class_section': 'B3 ET', 'students': list(students)}

    def test_no_score_is_blank_never_zero_and_unmatched_is_skipped(self):
        packet, rows, blockers = cs.build_packet(self.result(student('Ann', 9, pdf_id='a'), student('Ben', None), student('Cal', 7, matched=False)), self.pdfs, OPTIONS)
        by = {r['classtime_name']: r for r in rows}
        self.assertEqual((by['Ann']['action'], by['Ben']['action'], by['Cal']['action']), ('save', 'blank', 'skip'))
        sent = {s['name']: s for s in packet['students']}
        self.assertEqual(set(sent), {'Ann Nigri', 'Ben Nigri'})            # the unmatched boy is never sent
        self.assertIsNone(sent['Ben Nigri']['mark']); self.assertEqual(sent['Ben Nigri']['attendance'], 'review')
        self.assertNotIn(0, [s['mark'] for s in packet['students']])
        self.assertEqual(blockers, [])
    def test_percent_rounds_half_up(self):
        self.assertEqual(cs.percent(7, 12), 58); self.assertEqual(cs.percent(1, 8), 13); self.assertEqual(cs.percent(12, 12), 100); self.assertEqual(cs.percent(0, 12), 0)
    def test_a_real_zero_score_is_a_zero_but_missing_is_not(self):
        packet, rows, _ = cs.build_packet(self.result(student('Ann', 0, pdf_id='a'), student('Ben', None)), self.pdfs, OPTIONS)
        marks = {s['name']: s['mark'] for s in packet['students']}
        self.assertEqual(marks, {'Ann Nigri': 0, 'Ben Nigri': None})
    def test_unverified_report_or_no_scores_block_saving(self):
        _, _, blockers = cs.build_packet(self.result(student('Ann', 9, pdf='missing')), self.pdfs, OPTIONS)
        self.assertTrue(any('not verified' in b for b in blockers))
        _, _, blockers = cs.build_packet(self.result(student('Ben', None)), self.pdfs, OPTIONS)
        self.assertIn('No matched boy has a score', blockers)
    def test_reports_are_encoded_only_when_asked(self):
        quiet, _, _ = cs.build_packet(self.result(student('Ann', 9, pdf_id='a')), self.pdfs, OPTIONS)
        self.assertNotIn('report_base64', quiet['students'][0])
        full, _, _ = cs.build_packet(self.result(student('Ann', 9, pdf_id='a')), self.pdfs, OPTIONS, with_reports=True)
        self.assertTrue(full['students'][0]['report_base64']); self.assertTrue(full['students'][0]['report_filename'].endswith('.pdf'))

    def test_options_are_validated(self):
        r = self.result(); topics = {'Chumash': 1, 'Shoroshim': 2}
        ok = cs.validate_options({'topic': 'Chumash', 'date': '2026-10-09'}, r, topics)
        self.assertEqual(ok['test_name'], 'Made Up Quiz (ABC123)'); self.assertEqual(ok['type'], 'quiz')
        for bad in ({'topic': 'Nope', 'date': '2026-10-09'}, {'topic': 'Chumash', 'date': '9/10/2026'}, {'topic': 'Chumash', 'date': '2026-10-09', 'type': 'exam'},
                    {'topic': 'Chumash', 'date': '2026-10-09', 'test_name': 'x' * 121}, {'topic': 'Chumash', 'date': '2026-10-09', 'test_name': 'bad\nname'}):
            with self.assertRaises(ValueError): cs.validate_options(bad, r, topics)
    def test_duplicate_detection(self):
        self.assertEqual(cs.find_duplicates(['Other', 'made up quiz (abc123)'], OPTIONS, 'ABC123'), ['made up quiz (abc123)'])
        self.assertEqual(cs.find_duplicates(['Renamed Quiz - ABC123 - redo'], OPTIONS, 'ABC123'), ['Renamed Quiz - ABC123 - redo'])
        self.assertEqual(cs.find_duplicates(['Other', 'ABC1234 quiz'], OPTIONS, 'ABC123'), [])
    def test_preview_statuses_and_never_saveable(self):
        r = self.result(student('Ann', 9, pdf_id='a'), student('Ben', None))
        clear = cs.preview(r, self.pdfs, OPTIONS, {'titles': ['Other']})
        self.assertEqual(clear['duplicate_check']['status'], 'clear'); self.assertEqual(clear['blockers'], [])
        dup = cs.preview(r, self.pdfs, OPTIONS, {'titles': ['Made Up Quiz (ABC123)']})
        self.assertEqual(dup['duplicate_check']['status'], 'duplicate'); self.assertIn('A mark for this session already exists in Nigri', dup['blockers'])
        for unreadable in (cs.preview(r, self.pdfs, OPTIONS, None, 'offline'), cs.preview(r, self.pdfs, OPTIONS, {'titles': []})):
            self.assertEqual(unreadable['duplicate_check']['status'], 'could_not_check'); self.assertTrue(unreadable['blockers'])
        for out in (clear, dup):
            self.assertFalse(out['can_save']); self.assertFalse(out['real_save_enabled']); self.assertFalse(out['nigri_writes'])
        self.assertNotIn('report_base64', json.dumps(clear)); self.assertEqual(clear['would_send']['reports'], 1)
        self.assertEqual(clear['counts'], {'will_save': 1, 'left_blank_no_score': 1, 'skipped_unmatched': 0})


class ReadOnlyTests(unittest.TestCase):
    def test_the_nigri_lookup_only_reads(self):
        source = (Path(__file__).resolve().parents[1] / 'nigri_playwright.py').read_text(encoding='utf-8')
        body = source[source.index('def list_mark_titles'):]
        for forbidden in ('.click(', '.fill(', '.check(', 'select_option', 'set_input_files', 'del_test', 'Create Mark', 'Save!', 'run_marks_sync'):
            self.assertNotIn(forbidden, body)


def claims(email='owner@example.test'):
    return {'iss': 'https://securetoken.google.com/b3-games', 'email': email, 'email_verified': True, 'sub': 'uid1'}


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.addCleanup(self.dir.cleanup)
        path = Path(self.dir.name) / 'a.pdf'; path.write_bytes(b'%PDF-1.4 fake')
        import time
        cr.JOBS.clear()
        cr.JOBS['rev1'] = {'owner': 'uid1', 'expires': time.time() + 3600, 'status': 'ready', 'pdfs': {'a': str(path)},
                           'result': {'session_code': 'ABC123', 'session_name': 'Made Up Quiz', 'class_section': 'B3 ET',
                                      'students': [student('Ann', 9, pdf_id='a'), student('Ben', None)]}}
        cr.JOBS['loading1'] = {'owner': 'uid1', 'expires': time.time() + 3600, 'status': 'loading', 'pdfs': {}}
        self.client = server.app.test_client(); CALLS['write'] = CALLS['list'] = 0
        self.addCleanup(cr.JOBS.clear)
    def post(self, rid='rev1', body=None, **kw):
        return self.client.post('/classtime/reviews/' + rid + '/sync-preview', json=body if body is not None else {'topic': 'Shoroshim', 'date': '2026-10-09'}, **kw)
    def owner(self): return patch('google.oauth2.id_token.verify_firebase_token', return_value=claims())

    def test_only_the_owner_google_sign_in_is_accepted(self):
        self.assertEqual(self.post().status_code, 401)
        self.assertEqual(self.post(headers={'X-Sync-Key': 'test-key-123'}).status_code, 401)             # old key
        self.assertEqual(self.post(headers={'Authorization': 'Bearer diagnostic-abc'}).status_code, 401)   # diagnostic token
        with patch('google.oauth2.id_token.verify_firebase_token', return_value=claims('someone@example.test')):
            self.assertEqual(self.post(headers={'Authorization': 'Bearer t'}).status_code, 401)
        self.assertEqual(CALLS['list'], 0)
    def test_owner_gets_a_dry_run_and_nothing_is_written(self):
        with self.owner():
            response = self.post(headers={'Authorization': 'Bearer t'})
        data = response.get_json()
        self.assertEqual(response.status_code, 200); self.assertTrue(data['dry_run']); self.assertFalse(data['nigri_writes']); self.assertFalse(data['can_save'])
        self.assertEqual(data['duplicate_check']['status'], 'clear'); self.assertEqual(CALLS['write'], 0); self.assertEqual(CALLS['list'], 1)
        self.assertEqual(data['counts']['will_save'], 1); self.assertNotIn('report_base64', json.dumps(data))
    def test_duplicate_and_unreadable_lists_are_reported(self):
        with self.owner():
            with patch.object(stub, 'list_mark_titles', lambda: {'titles': ['Made Up Quiz (ABC123)'], 'discovered_ids': 1}):
                self.assertEqual(self.post(headers={'Authorization': 'Bearer t'}).get_json()['duplicate_check']['status'], 'duplicate')
            def boom(): raise ValueError('Nigri login failed')
            with patch.object(stub, 'list_mark_titles', boom):
                out = self.post(headers={'Authorization': 'Bearer t'}).get_json()
        self.assertEqual(out['duplicate_check']['status'], 'could_not_check'); self.assertTrue(out['blockers']); self.assertEqual(CALLS['write'], 0)
    def test_bad_input_missing_and_unready_reviews(self):
        h = {'Authorization': 'Bearer t'}
        with self.owner():
            self.assertEqual(self.post(body={'topic': 'Nope', 'date': '2026-10-09'}, headers=h).status_code, 400)
            self.assertEqual(self.post(rid='nope', headers=h).status_code, 404)
            self.assertEqual(self.post(rid='loading1', headers=h).status_code, 409)
        self.assertEqual(CALLS['list'], 0)
    def test_another_owners_review_is_not_visible(self):
        cr.JOBS['rev1']['owner'] = 'someone-else'
        with self.owner(): self.assertEqual(self.post(headers={'Authorization': 'Bearer t'}).status_code, 404)
    def test_existing_sync_marks_route_still_uses_the_key_or_sign_in(self):
        self.assertEqual(self.client.post('/sync-marks', json={}).status_code, 401)
        self.assertNotEqual(self.client.post('/sync-marks', json={}, headers={'X-Sync-Key': 'test-key-123'}).status_code, 401)


if __name__ == '__main__':
    unittest.main()
