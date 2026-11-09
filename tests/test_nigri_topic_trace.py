"""No-write tests for network-blocked Nigri class/topic-change diagnostic."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nigri_topic_trace as trace


class FakeRequest:
    def __init__(self, method='GET', resource_type='xhr', url=None):
        self.method = method
        self.resource_type = resource_type
        self.url = url or trace.BLANK_FORM_URL


class FakeRoute:
    def __init__(self, request):
        self.request = request
        self.outcome = None
    def abort(self):
        self.outcome = 'aborted'
    def continue_(self):
        self.outcome = 'continued'


class TraceSafetyTests(unittest.TestCase):
    def test_blank_form_url_is_exact(self):
        self.assertTrue(trace._is_blank_form_get(trace.BLANK_FORM_URL))
        for wrong in [
            trace.BLANK_FORM_URL.replace('testID=0', 'testID=7'),
            trace.BLANK_FORM_URL.replace('testID=0', 'testID='),
            trace.BLANK_FORM_URL.replace('testID=0', 'testID=00'),
            trace.BLANK_FORM_URL + '&action=save',
            trace.BLANK_FORM_URL + '&testID=0',
            trace.BLANK_FORM_URL.replace('testTopicSelect=', 'testTopicSelect=142'),
            trace.BLANK_FORM_URL.replace('https:', 'http:'),
            trace.BLANK_FORM_URL.replace('www.nigrijewishonlineschool.com', 'evil.test'),
        ]:
            with self.subTest(wrong=wrong):
                self.assertFalse(trace._is_blank_form_get(wrong))

    def test_preform_route_allows_one_get_and_static_scripts_only(self):
        cases = [
            ('GET', 'document', trace.BLANK_FORM_URL, 'continued'),
            ('POST', 'document', trace.BLANK_FORM_URL, 'aborted'),
            ('GET', 'script', trace.BASE + '/js/os_prog.js?v=1a', 'continued'),
            ('GET', 'stylesheet', trace.BASE + '/css/os_prog.css', 'continued'),
            ('GET', 'xhr', trace.BASE + '/main/default_os_prog.asp?section=teachers&action=save', 'aborted'),
            ('POST', 'xhr', trace.BASE + '/main/default_os_prog.asp?section=teachers&action=update', 'aborted'),
            ('GET', 'script', 'https://evil.test/js/attack.js', 'aborted'),
            ('GET', 'document', trace.BASE + '/main/default_os_prog.asp?section=teachers&spec=tests&testID=1', 'aborted'),
        ]
        for method, kind, url, expected in cases:
            with self.subTest(url=url, method=method):
                route = FakeRoute(FakeRequest(method, kind, url))
                trace._preform_route(route)
                self.assertEqual(route.outcome, expected)

    def test_capture_cancels_get_post_external_without_leaking_urls(self):
        seen = []
        for req in [
            FakeRequest('POST', 'xhr', trace.BASE+'/main/default_os_prog.asp?submit=save&childID=PRIVATE'),
            FakeRequest('GET', 'fetch', trace.BASE+'/ajax/other?secret=TRUE'),
            FakeRequest('GET', 'document', 'https://other.test/private'),
            FakeRequest('DELETE', 'xhr', trace.BASE+'/private'),
        ]:
            route = FakeRoute(req)
            trace._capture_and_abort(route, seen)
            self.assertEqual(route.outcome, 'aborted')
        self.assertEqual(len(seen), 4)
        self.assertEqual(seen[0]['method'], 'POST')
        self.assertEqual(seen[-1]['method'], 'OTHER')
        self.assertFalse(seen[2]['same_host'])
        for secret in ('PRIVATE', 'secret=TRUE', 'other.test', 'childID'):
            self.assertNotIn(secret, str(seen))

    def test_capture_is_bounded_and_all_requests_aborted(self):
        seen = []
        for _ in range(100):
            route = FakeRoute(FakeRequest('GET', 'xhr'))
            trace._capture_and_abort(route, seen)
            self.assertEqual(route.outcome, 'aborted')
        self.assertEqual(len(seen), 20)


class FakeGrade:
    def __init__(self, context):
        self.context = context
    def count(self):
        return 1
    def select_option(self, value, **kwargs):
        assert value == '22590'
        self.context.page.grade_selected = True
        self.context.page.change_seen = True
        if not self.context.simulate_requests:
            return
        for method, kind, url in [
            ('POST', 'xhr', trace.BASE + '/secret/save?studentId=PRIVATE'),
            ('GET', 'fetch', trace.BASE + '/main/topics?class=22590'),
        ]:
            route = FakeRoute(FakeRequest(method, kind, url))
            self.context.router(route)
            assert route.outcome == 'aborted'


class FakeTopic:
    def count(self):
        return 1


class FakePage:
    def __init__(self, context):
        self.context = context
        self.grade_selected = False
        self.change_seen = False
    def evaluate(self, code, instrument=False):
        assert 'select[name="testGradeID"]' in code
        return {
            'selected_b3_et': self.grade_selected,
            'shoroshim_option_present': True,
            'topic_option_count_capped': 3,
            'change_event_seen': self.change_seen,
            'topic_element_replaced': False,
            'onchange_attribute_present': True,
        }
    def goto(self, url, **kwargs):
        route = FakeRoute(FakeRequest('GET', 'document', url))
        self.context.router(route)
        assert route.outcome == 'continued'
    def locator(self, selector):
        if selector == 'select[name="testGradeID"]':
            return FakeGrade(self.context)
        if selector == 'select[name="testTopicID"]':
            return FakeTopic()
        if selector == 'select[name="testGradeID"] option[value="22590"]':
            return FakeTopic()
        raise AssertionError(selector)
    def on(self, event, callback):
        assert event in ('dialog', 'pageerror')
    def wait_for_timeout(self, value):
        assert value in (250, 700)


class FakeContext:
    def __init__(self):
        self.router = None
        self.simulate_requests = True
        self.page = FakePage(self)
    def route(self, selector, handler):
        assert selector == '**/*'
        self.router = handler
    def unroute(self, selector, handler):
        assert selector == '**/*'
    def new_page(self):
        return self.page


class FakeBrowser:
    def __init__(self):
        self.ctx = FakeContext()
        self.closed = False
    def new_context(self, **kwargs):
        assert kwargs.get('service_workers') == 'block'
        return self.ctx
    def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self, browser):
        self.chromium = self
        self.browser = browser
    def launch(self, **kwargs):
        assert kwargs['headless']
        return self.browser
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


class FullBlockedTraceTests(unittest.TestCase):
    def test_b3_et_event_is_traced_but_post_and_get_are_blocked(self):
        b = FakeBrowser()
        p = FakePlaywright(b)
        with patch('playwright.sync_api.sync_playwright', return_value=p), \
             patch('classtime_reader._chromium_args', return_value=[]), \
             patch('nigri_playwright.login', return_value=None), \
             patch('browser_runtime.log_memory'), \
             patch.object(trace, '_headroom_or_stop', return_value=True), \
             patch.dict('os.environ', {'NIGRI_USERNAME': 'FAKE', 'NIGRI_PASSWORD': 'FAKE'}):
            answer = trace.blocked_topic_refresh_trace()
        self.assertTrue(b.closed)
        self.assertEqual(answer['status'], 'requests_blocked_and_class_change_inspected')
        self.assertEqual(answer['attempted_request_count_capped'], 2)
        self.assertEqual(answer['attempted_methods'], ['GET', 'POST'])
        self.assertTrue(answer['post_blocked'])
        self.assertTrue(answer['attempted_xhr_or_fetch'])
        self.assertTrue(answer['all_change_requests_blocked'])
        self.assertTrue(answer['change_event_observed'])
        self.assertTrue(answer['b3_et_selected_after'])
        self.assertTrue(answer['shoroshim_present_after_blocked_change'])
        self.assertFalse(answer['post_refresh_topic_verified'])
        self.assertFalse(answer['nigri_writes'])
        self.assertNotIn('PRIVATE', str(answer))

    def test_no_requests_reported_as_inconclusive_even_if_change_fires(self):
        b = FakeBrowser()
        b.ctx.simulate_requests = False
        with patch('playwright.sync_api.sync_playwright', return_value=FakePlaywright(b)), \
             patch('classtime_reader._chromium_args', return_value=[]), \
             patch('nigri_playwright.login', return_value=None), \
             patch('browser_runtime.log_memory'), \
             patch.object(trace, '_headroom_or_stop', return_value=True), \
             patch.dict('os.environ', {'NIGRI_USERNAME': 'FAKE', 'NIGRI_PASSWORD': 'FAKE'}):
            answer = trace.blocked_topic_refresh_trace()
        self.assertTrue(b.closed)
        self.assertEqual(answer['status'], 'no_class_change_requests_observed')
        self.assertEqual(answer['attempted_methods'], [])
        self.assertEqual(answer['attempted_request_count_capped'], 0)
        self.assertTrue(answer['change_event_observed'])
        self.assertTrue(answer['b3_et_selected_after'])
        self.assertTrue(answer['shoroshim_present_after_blocked_change'])
        self.assertFalse(answer['post_refresh_topic_verified'])
        self.assertFalse(answer['nigri_writes'])

    def test_preform_records_denied_script_categories_not_urls(self):
        stats = {'blocked_scripts': 0, 'blocked_stylesheets': 0}
        for kind, url in [
            ('script', 'https://external.example/private.js'),
            ('stylesheet', 'https://external.example/private.css'),
            ('xhr', trace.BASE + '/main/grade-refresh?secret=PRIVATE')
        ]:
            route = FakeRoute(FakeRequest('GET', kind, url))
            trace._preform_route(route, stats)
            self.assertEqual(route.outcome, 'aborted')
        self.assertEqual(stats['blocked_scripts'], 1)
        self.assertEqual(stats['blocked_stylesheets'], 1)
        self.assertNotIn('external.example', str(stats))
        self.assertNotIn('PRIVATE', str(stats))

    def test_preflight_low_memory_closes_browser_without_selecting(self):
        b = FakeBrowser()
        p = FakePlaywright(b)
        with patch('playwright.sync_api.sync_playwright', return_value=p), \
             patch('classtime_reader._chromium_args', return_value=[]), \
             patch('nigri_playwright.login', return_value=None), \
             patch('browser_runtime.log_memory'), \
             patch.object(trace, '_headroom_or_stop', return_value=False), \
             patch.dict('os.environ', {'NIGRI_USERNAME': 'FAKE', 'NIGRI_PASSWORD': 'FAKE'}):
            answer = trace.blocked_topic_refresh_trace()
        self.assertEqual(answer['status'], 'insufficient_memory')
        self.assertTrue(b.closed)
        self.assertFalse(answer['nigri_writes'])


if __name__ == '__main__':
    unittest.main()
