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
        self.router = None
    def route(self, selector, handler):
        assert selector == '**/*'
        self.router = handler
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
    def run_probe(self, html, cookies=None, memory_mib=160, inspect_blank_form=False):
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

        def fake_blank_probe(session, marks):
            self.assertTrue(state['browser_closed'])
            state['blank_probe_calls'] = state.get('blank_probe_calls', 0) + 1
            self.assertTrue(marks.has_create_text)
            return {'status': 'blank_form_found', 'no_submit_performed': True, 'nigri_writes': False}

        with patch.object(probe, '_probe_blank_mark_form', side_effect=fake_blank_probe), \
             patch('playwright.sync_api.sync_playwright', return_value=fake_playwright), \
             patch('nigri_playwright.login', side_effect=fake_login), \
             patch('classtime_reader._chromium_args', return_value=[]), \
             patch('browser_runtime.log_memory'), \
             patch('browser_runtime.memory_snapshot', return_value={
                 'working_set': memory_mib * 1024 * 1024,
                 'limit': 512 * 1024 * 1024,
             }), \
             patch.object(probe, '_request', side_effect=fake_http), \
             patch.dict('os.environ', {'NIGRI_USERNAME': 'FAKE_LOGIN',
                                        'NIGRI_PASSWORD': 'FAKE_PASSWORD'}):
            answer = probe.probe_hybrid(inspect_blank_form=inspect_blank_form)
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

    def test_mark_link_inspection_does_not_follow_any_navigation(self):
        html = ('<a href="/main/default_os_prog.asp?section=teachers&amp;spec=tests'
                '&amp;action=new_test">Create New Mark</a>'
                '<form method="post" action="/unsafe">'
                '<input name="testGradeID" value="DONOTRETURN">'
                '<input name="testName" value="SECRETNAME"></form>')
        result, state = self.run_probe(html)
        self.assertEqual(result['status'], 'hybrid_marks_found')
        self.assertEqual(state['requests'], [('GET', probe.MARKS_URL)])
        summary = result['mark_entry_readonly']
        self.assertEqual(summary['entry_link_kind'], 'same_host_http_link')
        self.assertEqual(summary['entry_link_action_kind'], 'new_test')
        self.assertEqual(summary['known_form_field_names'], ['testGradeID', 'testName'])
        self.assertTrue(summary['entry_form_was_not_opened'])
        self.assertNotIn('DONOTRETURN', str(result))
        self.assertNotIn('SECRETNAME', str(result))

    def test_mark_link_external_and_delete_urls_never_followed(self):
        html = '<a href="https://other.test/delete">Create New Mark</a>'
        result, state = self.run_probe(html)
        self.assertEqual(result['mark_entry_readonly']['entry_link_kind'], 'external_or_unsafe')
        self.assertEqual(state['requests'], [('GET', probe.MARKS_URL)])
        html = '<a href="/main/default_os_prog.asp?section=teachers&amp;spec=tests&amp;action=del_test&amp;testID=42">Create New Mark</a>'
        result, state = self.run_probe(html)
        self.assertEqual(result['mark_entry_readonly']['entry_link_action_kind'], 'unknown')
        self.assertEqual(state['requests'], [('GET', probe.MARKS_URL)])

    def test_insufficient_browser_startup_headroom_stops_before_login(self):
        result, state = self.run_probe('<div>Create New Mark</div>', memory_mib=350)
        self.assertEqual(result['status'], 'hybrid_insufficient_headroom')
        self.assertTrue(state['browser_closed'])
        self.assertEqual(state['requests'], [])
        self.assertEqual(state['signins'], 0)
        self.assertFalse(result['nigri_writes'])

    def test_login_asset_blocker_keeps_scripts_and_forms(self):
        class Request:
            def __init__(self, resource_type):
                self.resource_type = resource_type
        class Route:
            def __init__(self, kind):
                self.request = Request(kind)
                self.call = None
            def abort(self): self.call = 'abort'
            def continue_(self): self.call = 'continue'
        for kind in ('image', 'font', 'media', 'texttrack'):
            route = Route(kind)
            probe._hybrid_login_router(route)
            self.assertEqual(route.call, 'abort')
        for kind in ('document', 'script', 'stylesheet', 'xhr', 'fetch'):
            route = Route(kind)
            probe._hybrid_login_router(route)
            self.assertEqual(route.call, 'continue')

    def test_optional_blank_form_integration_runs_after_browser_closed(self):
        result, state = self.run_probe('<div>Create New Mark</div>', inspect_blank_form=True)
        self.assertEqual(result['status'], 'hybrid_marks_found')
        self.assertEqual(result['blank_form']['status'], 'blank_form_found')
        self.assertEqual(state['blank_probe_calls'], 1)
        self.assertTrue(state['browser_closed'])
        self.assertFalse(result['nigri_writes'])

    def test_blank_form_inspection_refused_when_marks_unconfirmed(self):
        result, state = self.run_probe(
            '<form><input name="login"><input name="password"></form>',
            inspect_blank_form=True)
        self.assertEqual(result['blank_form']['status'], 'blank_form_marks_unconfirmed')
        self.assertEqual(state.get('blank_probe_calls', 0), 0)
        self.assertFalse(result['nigri_writes'])

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
