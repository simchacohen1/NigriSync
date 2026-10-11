import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import classtime_http as ch


class FakeResponse:
    def __init__(self, content=b'', status=200): self.content, self.status_code = content, status


class FakeHttp:
    """Stands in for Classtime: records calls, replays the message shapes seen in a real login."""
    def __init__(self, export_url=b'https://export.classtime.com/abc/x.zip', authorize_status=200):
        self.headers, self.calls, self.export_url, self.authorize_status = {}, [], export_url, authorize_status
    def post(self, url, data=None, timeout=None):
        self.calls.append((url.rsplit('/service/global/', 1)[-1], data))
        if url.endswith('Account/login'): return FakeResponse(ch._raw(2, b'STATE' * 5))
        if url.endswith('Account/authorize'): return FakeResponse(ch._text(1, 'https://www.classtime.com/questions?authenticationMethodId=x'), self.authorize_status)
        if url.endswith('Session/exportSession'): return FakeResponse(ch._raw(1, self.export_url))
    def get(self, url, **kw):
        self.calls.append(('GET', url)); return FakeResponse(b'')


class HttpTests(unittest.TestCase):
    def test_message_sizes_match_the_real_recording(self):
        self.assertEqual(len(ch.login_request()), 88)
        self.assertEqual(len(ch.export_request('TIKEV7', 'America/New_York')), 28)
        self.assertEqual(len(ch.password_hash('A@b.c', 'pw')), 32)
    def test_password_hash_ignores_email_case_only(self):
        self.assertEqual(ch.password_hash('Teacher@X.com', 'pw'), ch.password_hash('teacher@x.com', 'pw'))
        self.assertNotEqual(ch.password_hash('a@x.com', 'pw'), ch.password_hash('a@x.com', 'pw2'))
    def test_authorize_echoes_login_state_and_never_sends_password_text(self):
        fake = FakeHttp(); client = ch.ClasstimeHttp(fake)
        client.sign_in('teacher@x.com', 'secret-pass')
        body = dict(fake.calls)['Account/authorize']
        self.assertNotIn(b'secret-pass', body)
        self.assertIn(b'STATE' * 5, body)
        self.assertEqual(ch._read_fields(ch._read_fields(body)[2])[1], b'teacher@x.com')
    def test_export_returns_only_classtime_export_links(self):
        client = ch.ClasstimeHttp(FakeHttp())
        self.assertTrue(client.export_zip_url('TIKEV7').startswith('https://export.classtime.com/'))
        for bad in (b'https://evil.example/x.zip', b'http://export.classtime.com/x.zip'):
            with self.assertRaises(ValueError): ch.ClasstimeHttp(FakeHttp(export_url=bad)).export_zip_url('TIKEV7')
        with self.assertRaises(ValueError): client.export_zip_url('bad')
    def test_refusal_message_has_no_secrets(self):
        client = ch.ClasstimeHttp(FakeHttp(authorize_status=401))
        with self.assertRaises(ValueError) as caught: client.sign_in('t@x.com', 'secret-pass')
        self.assertNotIn('secret-pass', str(caught.exception)); self.assertNotIn('t@x.com', str(caught.exception))
    def test_missing_credentials(self):
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(ValueError): ch.fetch_report_zip('TIKEV7')

if __name__ == '__main__':
    unittest.main()
