"""Read-only HTTP probe safety tests. No network and no Nigri writes."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nigri_http_probe as probe


class FakeResponse:
    def __init__(self, html='', status=200, headers=None):
        self.content = html.encode()
        self.text = html
        self.status_code = status
        self.headers = headers or {}


class FakeSession:
    def __init__(self, login_form=None, marks_page=None, redirect=None):
        self.calls = []
        self.login_form = login_form or (
            '<form method="post" action="/main/default_os_prog.asp?section=teachers">'
            '<input type="hidden" name="csrf" value="DO_NOT_DISCLOSE">'
            '<input name="login"><input name="password">'
            '<input type="image" name="submit" src="login-Button.gif"></form>'
        )
        self.marks_page = marks_page or (
            '<p>Welcome</p><a href="/main/default_os_prog.asp?section=teachers&amp;spec=tests&amp;action=create_test">'
            'Create New Mark</a>'
        )
        self.redirect = redirect

    def __enter__(self): return self
    def __exit__(self, *_): return False

    def request(self, method, url, *, data=None, timeout=None, allow_redirects=None):
        self.calls.append((method, url, data))
        if self.redirect and len(self.calls) == 1:
            return FakeResponse(status=302, headers={'Location': self.redirect})
        if method == 'POST':
            return FakeResponse('<p>Welcome</p>')
        if len(self.calls) == 1:
            return FakeResponse(self.login_form)
        return FakeResponse(self.marks_page)


class ProbeTests(unittest.TestCase):
    def execute(self, session):
        with patch('requests.Session', return_value=session), patch.dict('os.environ', {'NIGRI_USERNAME': 'FAKE_USER', 'NIGRI_PASSWORD': 'FAKE_PASSWORD'}):
            return probe.probe()

    def test_only_login_post_and_marks_get(self):
        session = FakeSession()
        result = self.execute(session)
        self.assertEqual(result['status'], 'marks_listing_found')
        self.assertTrue(result['http_login_succeeded'])
        self.assertFalse(result['nigri_writes'])
        self.assertEqual([x[0] for x in session.calls], ['GET', 'POST', 'GET', 'GET'])
        self.assertEqual(result['cookies_after_login_count'], 0)
        self.assertFalse(result['cookies_sent_to_marks'])
        self.assertFalse(result['login_form_at_teacher_home'])
        self.assertFalse(result['login_form_at_marks'])
        self.assertTrue(session.calls[1][1].startswith(probe.BASE))
        self.assertEqual(session.calls[1][2]['csrf'], 'DO_NOT_DISCLOSE')
        self.assertEqual(session.calls[1][2]['submit.x'], '15')
        self.assertEqual(session.calls[1][2]['submit.y'], '15')
        self.assertNotIn('submit', session.calls[1][2])
        self.assertEqual(result['create_link_query_keys'], ['action', 'section', 'spec'])
        self.assertNotIn('FAKE_PASSWORD', str(result))
        self.assertNotIn('DO_NOT_DISCLOSE', str(result))

    def test_refuses_login_form_action_outside_host(self):
        s = FakeSession(login_form='<form method="post" action="https://elsewhere.test/login">'
                                     '<input name="login"><input name="password"></form>')
        with self.assertRaises(ValueError):
            self.execute(s)
        self.assertEqual([x[0] for x in s.calls], ['GET'])

    def test_refuses_external_redirect_before_post(self):
        s = FakeSession(redirect='https://elsewhere.test/')
        with self.assertRaises(ValueError):
            self.execute(s)
        self.assertEqual(len(s.calls), 1)

    def test_does_not_follow_login_post_preserving_redirect(self):
        s = FakeSession()
        orig = s.request
        def blocked(method, url, **kwargs):
            if method == 'POST':
                s.calls.append((method, url, kwargs.get('data')))
                return FakeResponse(status=307, headers={'Location': '/new-place'})
            return orig(method, url, **kwargs)
        s.request = blocked
        with self.assertRaises(ValueError):
            self.execute(s)
        self.assertEqual([c[0] for c in s.calls], ['GET', 'POST'])

    def test_rejects_bad_login(self):
        s = FakeSession(marks_page='<form><input name="login"><input name="password"></form>')
        result = self.execute(s)
        self.assertEqual(result['status'], 'login_not_accepted')
        self.assertFalse(result['login_form_returned_after_post'])
        self.assertTrue(result['image_button_submitted'])
        self.assertFalse(result['nigri_writes'])
        self.assertNotIn('create_link_query_keys', result)

    def test_login_page_after_post_is_reported_without_sensitive_values(self):
        s = FakeSession(marks_page='<form><input name="login"><input name="password"></form>')
        original = s.request
        def login_rejected(method, url, **kwargs):
            if method == 'POST':
                s.calls.append((method, url, kwargs.get('data')))
                return FakeResponse('<form><input name="login"><input name="password"></form>')
            return original(method, url, **kwargs)
        s.request = login_rejected
        result = self.execute(s)
        self.assertTrue(result['login_form_returned_after_post'])
        self.assertNotIn('FAKE_PASSWORD', str(result))
        self.assertEqual([x[0] for x in s.calls], ['GET', 'POST', 'GET', 'GET'])

    def test_shared_login_header_does_not_mask_real_marks_listing(self):
        s = FakeSession(marks_page=(
            '<form><input name="login"><input name="password"></form>'
            '<div>Create New Mark</div>'))
        result = self.execute(s)
        self.assertEqual(result['status'], 'marks_listing_found')
        self.assertTrue(result['login_form_at_marks'])
        self.assertTrue(result['marks_create_text_found'])
        self.assertFalse(result['nigri_writes'])

    def test_session_diagnostics_only_include_safe_counts(self):
        s = FakeSession(marks_page='<form><input name="login"><input name="password"></form>')
        result = self.execute(s)
        self.assertTrue(result['login_form_at_teacher_home'])
        self.assertTrue(result['login_form_at_marks'])
        self.assertFalse(result['cookies_sent_to_marks'])
        self.assertEqual(result['login_post_redirect_count'], 0)
        self.assertNotIn('FAKE_PASSWORD', str(result))
        self.assertNotIn('FAKE_USER', str(result))

    def test_page_is_not_assumed_usable_without_evidence(self):
        s = FakeSession(marks_page='<p>Blank page</p>')
        result = self.execute(s)
        self.assertEqual(result['status'], 'marks_page_unconfirmed')
        self.assertFalse(result['http_login_succeeded'])

    def test_missing_credentials_no_requests(self):
        with patch.dict('os.environ', {'NIGRI_USERNAME': '', 'NIGRI_PASSWORD': ''}):
            self.assertEqual(probe.probe()['status'], 'credentials_missing')


if __name__ == '__main__':
    unittest.main()
