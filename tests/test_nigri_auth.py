"""Nigri routes accept the old shared key OR the website owner's verified Google sign-in (never the Classtime diagnostic token)."""
import os
import sys
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['SYNC_API_KEY'] = 'test-key-123'           # made-up values only
os.environ['CLASSTIME_TEST_TOKEN'] = 'diagnostic-abc'
os.environ['CLASSTIME_REVIEW_OWNER_EMAIL'] = 'owner@example.test'
sys.modules.setdefault('fcntl', types.ModuleType('fcntl'))

class _Stubs(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith('__'): raise AttributeError(name)
        if name == 'SyncError': return type('SyncError', (Exception,), {})
        return lambda *a, **k: {}
for name in ('nigri_playwright', 'attendance_reader'):
    sys.modules.setdefault(name, _Stubs(name))
import browser_runtime
@contextmanager
def _no_slot(): yield
browser_runtime.browser_slot = _no_slot
import app as server

KEY_ONLY = ['/sync-points', '/debug-page', '/debug-points', '/debug-rewards', '/sync-marks']
KEY_OR_QUERY = ['/debug-marks', '/debug-marks-create', '/read-marks', '/read-attendance', '/debug-marks-full']
ALL = KEY_ONLY + KEY_OR_QUERY


def claims(email='owner@example.test', verified=True):
    return {'iss': 'https://securetoken.google.com/b3-games', 'email': email, 'email_verified': verified, 'sub': 'uid1'}


class NigriAuthTests(unittest.TestCase):
    def setUp(self):
        server.app.config['TESTING'] = False
        self.client = server.app.test_client()
        server.SYNC_API_KEY = 'test-key-123'
    def post(self, path, **kw): return self.client.post(path, json={}, **kw)

    def test_no_credentials_refused_everywhere(self):
        for path in ALL: self.assertEqual(self.post(path).status_code, 401, path)
    def test_old_key_still_works_everywhere(self):
        for path in ALL: self.assertNotEqual(self.post(path, headers={'X-Sync-Key': 'test-key-123'}).status_code, 401, path)
    def test_wrong_key_refused_and_unset_key_never_matches(self):
        for path in ALL: self.assertEqual(self.post(path, headers={'X-Sync-Key': 'nope'}).status_code, 401, path)
        server.SYNC_API_KEY = None
        for path in ALL: self.assertEqual(self.post(path, headers={'X-Sync-Key': 'None'}).status_code, 401, path)
    def test_key_in_address_only_where_it_was_allowed_before(self):
        for path in KEY_OR_QUERY: self.assertNotEqual(self.post(path + '?key=test-key-123').status_code, 401, path)
        for path in KEY_ONLY: self.assertEqual(self.post(path + '?key=test-key-123').status_code, 401, path)
    def test_owner_google_sign_in_works_on_all_ten_routes(self):
        with patch('google.oauth2.id_token.verify_firebase_token', return_value=claims()):
            for path in ALL: self.assertNotEqual(self.post(path, headers={'Authorization': 'Bearer sometoken'}).status_code, 401, path)
    def test_other_google_accounts_and_unverified_are_refused(self):
        for c in (claims('someone@example.test'), claims(verified=False), {**claims(), 'iss': 'https://evil.example'}):
            with patch('google.oauth2.id_token.verify_firebase_token', return_value=c):
                for path in ALL: self.assertEqual(self.post(path, headers={'Authorization': 'Bearer sometoken'}).status_code, 401, path)
    def test_bad_token_refused(self):
        with patch('google.oauth2.id_token.verify_firebase_token', side_effect=ValueError('bad')):
            for path in ALL: self.assertEqual(self.post(path, headers={'Authorization': 'Bearer junk'}).status_code, 401, path)
    def test_classtime_diagnostic_token_does_not_open_nigri_routes(self):
        for path in ALL: self.assertEqual(self.post(path, headers={'Authorization': 'Bearer diagnostic-abc'}).status_code, 401, path)
    def test_classtime_routes_unchanged(self):
        self.assertEqual(self.client.get('/classtime/resources').status_code, 401)
        self.assertEqual(self.client.get('/classtime/resources', headers={'Authorization': 'Bearer diagnostic-abc'}).status_code, 200)
    def test_cors_allows_authorization_header_for_nigri_routes(self):
        r = self.client.options('/sync-marks', headers={'Origin': 'https://simchacohen1.github.io', 'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'authorization,content-type'})
        self.assertIn('authorization', r.headers.get('Access-Control-Allow-Headers', '').lower())

if __name__ == '__main__':
    unittest.main()
