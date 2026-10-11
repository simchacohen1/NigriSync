"""Nigri hybrid sign-in -> HTTP read-only probe tests. No network/grade writes."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nigri_http_probe as probe


class FakeResponse:
    def __init__(self, html):
        self.content = html.encode('utf-8')
        self.text = html
        self.status_code = 200
        self.headers = {}


class FakePage:
    def evaluate(self, expression):
        assert expression == 'navigator.userAgent'
        return 'TestBrowser/1'


class FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies
    def new_page(self):
        return FakePage()
    def cookies(self):
        return self._cookies


class FakeBrowser:
    def __init__(self, cookies, state):
        self._cookies, self._state = cookies, state
    def new_context(self):
        return FakeContext(self._cookies)
    def close(self):
        self._state['browser_closed'] = True


class FakeSyncPlaywright:
    def __init__(self, browser):
        self.chromium = self
        self.browser = browser
    def launch(self, *, headless, args):
        assert headless is True
        return self.browser
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


COOKIE = {'name': 'FAKE_SESSION', 'value': 'FAKE_PRIVATE_VALUE',
          'domain': '.nigrijewishonlineschool.com', 'path': '/',
          'secure': True}


class HybridProbeTests(unittest.TestCase):
    def run_probe(self, html, cookies=None):
        state = {'browser_closed': False, 'requests': [], 'signins': 0}
        browser = FakeBrowser(COOKIE and ([COOKIE] if cookies is None else cookies), state)
        fake_playwright = FakeSyncPlaywright(browser)

        def fake_login(page):
            state['signins'] += 1
            self.assertIsInstance(page, FakePage)

        def fake_http(session, method, url, **kwargs):
            self.assertTrue(state['browser_closed'], 'Browser must close BEFORE any marks HTTP GET')
            self.assertEqual(method, 'GET')
            self.assertEqual(url, probe.MARKS_URL)
            self.assertFalse(kwargs.get('data'))
            self.assertEqual(session.headers['User-Agent'], 'TestBrowser/1')
            state['requests'].append((method, url))
            return FakeResponse(html)

        with patch('playwright.sync_api.sync_playwright', return_value=fake_playwright), \
             patch('nigri_playwright.login', side_effect=fake_login), \
             patch('classtime_reader._chromium_args', return_value=[]), \
             patch('browser_runtime.log_memory'), \
             patch.object(probe, '_request', side_effect=fake_http), \
             patch.dict('os.environ', {'NIGRI_USERNAME': 'FAKE_LOGIN',
                                        'NIGRI_PASSWORD': 'FAKE_PASSWORD'}):
            answer = probe.probe_hybrid()
        return answer, state

    def test_closes_browser_before_authenticated_marks_http_read(self):
        html = '<div>Create New Mark</div>'
        result, state = self.run_probe(html)
        self.assertEqual(result['status'], 'hybrid_marks_found')
        self.assertTrue(result['cookies_sent_to_marks'])
        self.assertEqual(result['transferred_cookie_count'], 1)
        self.assertTrue(result['browser_closed_before_http'])
        self.assertFalse(result['nigri_writes'])
        self.assertEqual(state['signins'], 1)
        self.assertEqual(state['requests'], [('GET', probe.MARKS_URL)])
        self.assertNotIn('FAKE_PASSWORD', str(result))
        self.assertNotIn('FAKE_PRIVATE_VALUE', str(result))

    def test_marks_unconfirmed_without_auth(self):
        html = '<form><input name="login"><input name="password"></form>'
        result, state = self.run_probe(html)
        self.assertEqual(result['status'], 'hybrid_marks_unconfirmed')
        self.assertTrue(result['login_form_on_marks'])
        self.assertFalse(result['nigri_writes'])
        self.assertTrue(state['browser_closed'])

    def test_missing_browser_cookies_does_not_claim_auth(self):
        html = '<form><input name="login"><input name="password"></form>'
        result, _ = self.run_probe(html, cookies=[])
        self.assertFalse(result['cookies_sent_to_marks'])
        self.assertEqual(result['transferred_cookie_count'], 0)

    def test_unrelated_cookies_not_transferred(self):
        html = '<form><input name="login"><input name="password"></form>'
        foreign = dict(COOKIE, domain='.other-example.test')
        result, _ = self.run_probe(html, cookies=[foreign])
        self.assertEqual(result['transferred_cookie_count'], 0)
        self.assertFalse(result['cookies_sent_to_marks'])


if __name__ == '__main__':
    unittest.main()
