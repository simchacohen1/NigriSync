"""Read-only Nigri course reader (issue 550, step 3b). Fake browser pages and made-up logins only: no network, no Nigri."""
import os
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.modules.setdefault('fcntl', types.ModuleType('fcntl'))
import nigri_course_reader as R
import nigri_courses as N

USER, PW = 'fakeowner@example.test', 'Fake-Owner-Pw-9'
GOOD = 'https://www.nigrijewishonlineschool.com'
LOGIN = R.LOGIN_URL
SIGNIN_POST = GOOD + '/main/default_os_prog.asp?section=teachers&subSection=signin&login=made%20up%20name&pagesreturn=home'


class Frame:
    def __init__(self, options=None):
        self.options = options

    def query_selector(self, sel):
        return object() if (self.options is not None and sel == R.PICKER) else None

    def eval_on_selector_all(self, sel, js):
        assert sel == R.PICKER + ' option'
        return self.options


class Req:
    def __init__(self, method, url, rtype='document'):
        self.method, self.url, self.resource_type = method, url, rtype


class Route:
    def __init__(self, req):
        self.request, self.result = req, None

    def abort(self): self.result = 'abort'
    def continue_(self): self.result = 'continue'


class FakePage:
    """Records everything the reader does. `after_login` controls what the sign-in produces."""

    def __init__(self, options, signed_in=True, extra_requests=()):
        self.options, self.signed_in, self.extra = options, signed_in, list(extra_requests)
        self.actions, self.handler, self.results = [], None, []
        self.frames = []

    def route(self, pattern, handler): self.handler = handler
    def goto(self, url): self.actions.append(('goto', url))
    def fill(self, sel, value): self.actions.append(('fill', sel))   # value deliberately not recorded
    def wait_for_load_state(self, s): pass
    def wait_for_timeout(self, ms): pass

    def click(self, sel):
        self.actions.append(('click', sel))
        for req in [Req('POST', SIGNIN_POST)] + self.extra:
            r = Route(req); self.handler(r); self.results.append((req.method, r.result))
        self.frames = [Frame(self.options)] if self.signed_in else [Frame(None)]

    def query_selector(self, sel):
        return None if self.signed_in else (object() if sel == 'input[name="password"]' else None)


def launcher(page):
    closed = []
    return (lambda: (page, lambda: closed.append(1))), closed


RAW = [{'value': '', 'name': ''}, {'value': '28734', 'name': 'B3 ET - Davening'}, {'value': '28762', 'name': 'B3 ET - Morning Class 1'},
       {'value': '28733', 'name': 'B3 WT - Davening'}, {'value': '90001', 'name': 'Conference Room - Boys'},
       {'value': '90002', 'name': 'Rally Room - Boys'}, {'value': '77777', 'name': 'Some Other Course'},
       {'value': 'abc', 'name': 'Odd One'}]


class GuardTests(unittest.TestCase):
    def test_only_gets_and_exactly_one_sign_in_post(self):
        s = {}
        self.assertTrue(R.allow_request('GET', GOOD + '/main/anything.asp?x=1', s))
        self.assertTrue(R.allow_request('POST', SIGNIN_POST, s))
        self.assertFalse(R.allow_request('POST', SIGNIN_POST, s))   # a second POST, even an identical one
        for method in ('PUT', 'DELETE', 'PATCH'):
            self.assertFalse(R.allow_request(method, SIGNIN_POST, {}), method)

    def test_the_sign_in_post_must_match_exactly(self):
        base = GOOD + '/main/default_os_prog.asp?'
        good = 'section=teachers&subSection=signin&login=x&pagesreturn=home'
        self.assertTrue(R.is_signin_post(base + good))
        self.assertTrue(R.is_signin_post('https://nigrijewishonlineschool.com/main/default_os_prog.asp?' + good))
        for url in (base + good + '&save=1',                                        # an extra key
                    base + 'section=teachers&subSection=signin&login=x',            # a missing key
                    base + good.replace('section=teachers', 'section=students'),    # other section
                    base + good + '&section=teachers',                              # repeated key
                    base + 'section=teachers&subSection=tests&side=1',              # a marks page
                    GOOD + '/main/other.asp?' + good,                               # other path
                    GOOD + '/main/default_os_prog.asp',
                    'http://www.nigrijewishonlineschool.com/main/default_os_prog.asp?' + good,
                    'https://www.nigrijewishonlineschool.com.evil.test/main/default_os_prog.asp?' + good,
                    base + good.replace('login=x', 'login=' + 'x' * 300)):
            self.assertFalse(R.is_signin_post(url), url)
            self.assertFalse(R.allow_request('POST', url, {}), url)

    def test_other_hosts_and_schemes_never_allowed(self):
        for url in ('https://evil.example.test/', 'http://www.nigrijewishonlineschool.com/x', 'https://www.nigrijewishonlineschool.com.evil.test/x',
                    'https://nigrisync.onrender.com/x', 'https://b3-games-default-rtdb.firebaseio.com/x.json'):
            self.assertFalse(R.allow_request('GET', url, {}), url)

    def test_guard_aborts_writes_and_images(self):
        page = FakePage(RAW)
        state = R.install_read_only_guard(page)
        for req, want in ((Req('GET', GOOD + '/a.asp'), 'continue'), (Req('GET', GOOD + '/i.png', 'image'), 'abort'),
                          (Req('POST', GOOD + '/main/save.asp'), 'abort'), (Req('POST', SIGNIN_POST), 'continue'), (Req('POST', SIGNIN_POST), 'abort')):
            r = Route(req); page.handler(r)
            self.assertEqual(r.result, want, (req.method, req.url))
        self.assertEqual(state['blocked'], ['POST', 'POST'])
        self.assertEqual(state['tracking_blocked'], 0)


class TrackingTests(unittest.TestCase):
    URLS = ('https://www.googletagmanager.com/gtag/js?id=G-X', 'https://www.clarity.ms/tag/abc', 'https://c.clarity.ms/collect',
            'https://www.google-analytics.com/g/collect', 'https://stats.g.doubleclick.net/x')

    def test_tracking_hosts_are_recognised_exactly(self):
        for url in self.URLS:
            self.assertTrue(R.is_tracking(url), url)
        for url in (GOOD + '/x', 'https://clarity.ms.evil.test/x', 'https://notclarity.ms/x', 'https://example.test/x', None):
            self.assertFalse(R.is_tracking(url), url)

    def test_tracking_is_dropped_quietly_and_is_not_a_failure(self):
        page = FakePage(RAW)
        state = R.install_read_only_guard(page)
        for url in self.URLS:
            r = Route(Req('GET', url, 'script')); page.handler(r)
            self.assertEqual(r.result, 'abort')
        self.assertEqual(state['tracking_blocked'], len(self.URLS))
        self.assertEqual((state['blocked'], state['blocked_detail']), ([], []))

    def test_a_tracking_url_can_never_carry_a_write(self):
        page = FakePage(RAW)
        state = R.install_read_only_guard(page)
        r = Route(Req('POST', 'https://www.clarity.ms/collect')); page.handler(r)
        self.assertEqual(r.result, 'abort')

    def test_run_with_tracking_and_a_stray_get_still_succeeds(self):
        page = InspectPage([InspectFrame(GOOD + '/main/home.asp', HOME)], extra_requests=[
            Req('GET', 'https://www.googletagmanager.com/gtag/js?id=G-X', 'script'), Req('GET', 'https://cdn.example.test/lib.js', 'script')])
        launch, _ = launcher(page)
        res = R.login_and_inspect(USER, PW, launch)
        self.assertEqual(res['signIn'], 'SUCCEEDED')
        self.assertEqual(res['trackingScriptsDropped'], 1)
        self.assertEqual(res['blockedRequests'], ['GET https://cdn.example.test/lib.js'])   # shown for information, not a failure

    def test_a_blocked_write_still_stops_the_server_reader(self):
        page = FakePage(RAW, extra_requests=[Req('POST', GOOD + '/main/default_os_prog.asp?section=teachers&subSection=tests&save=1')])
        launch, _ = launcher(page)
        with self.assertRaises(R.ReaderError):
            R.login_and_read(USER, PW, launch)


class ReadTests(unittest.TestCase):
    def test_reads_only_the_picker_and_closes(self):
        page = FakePage(RAW)
        launch, closed = launcher(page)
        raw = R.login_and_read(USER, PW, launch)
        self.assertEqual(raw, RAW)
        self.assertEqual(closed, [1])
        self.assertEqual([a[0] for a in page.actions], ['goto', 'fill', 'fill', 'click'])
        self.assertEqual(page.actions[0][1], LOGIN)   # the page it opens is the plain sign-in page (a GET)
        self.assertEqual(page.actions[3][1], 'input[name="submit"]')
        self.assertEqual(page.results, [('POST', 'continue')])

    def test_a_write_attempt_stops_everything_and_is_reported(self):
        page = FakePage(RAW, extra_requests=[Req('POST', GOOD + '/main/save-attendance.asp')])
        launch, closed = launcher(page)
        with self.assertRaises(R.ReaderError) as cm:
            R.login_and_read(USER, PW, launch)
        self.assertIn('non-read request was blocked', str(cm.exception))
        self.assertEqual(closed, [1])
        self.assertIn(('POST', 'abort'), page.results)

    def test_refused_sign_in_is_a_plain_error_without_the_password(self):
        launch, closed = launcher(FakePage(RAW, signed_in=False))
        with self.assertRaises(R.ReaderError) as cm:
            R.login_and_read(USER, PW, launch)
        self.assertNotIn(PW, str(cm.exception))
        self.assertNotIn(USER, str(cm.exception))
        self.assertEqual(closed, [1])

    def test_picker_missing_is_a_plain_error(self):
        page = FakePage(RAW)
        page.frames = []
        with self.assertRaises(R.ReaderError):
            R.read_picker(page)


class ParseSummaryTests(unittest.TestCase):
    def test_parse_drops_blanks_and_rooms_and_keeps_odd_ones_visible(self):
        courses = R.parse_options(RAW)
        names = [c['name'] for c in courses]
        self.assertNotIn('Conference Room - Boys', names)
        self.assertNotIn('Rally Room - Boys', names)
        self.assertIn({'id': None, 'name': 'Odd One'}, courses)
        self.assertIn({'id': 28734, 'name': 'B3 ET - Davening'}, courses)
        self.assertEqual(R.parse_options(None), [])
        self.assertEqual(R.parse_options([None, {}, {'value': '1'}]), [])

    def test_summary_found_missing_other(self):
        report = R.summarize(R.parse_options(RAW))
        et, wt = report['grades']['B3 ET'], report['grades']['B3 WT']
        self.assertEqual([c['slot'] for c in et['found']], ['davening', 'morning1'])
        self.assertEqual(len(et['missing']), 5)
        self.assertEqual([c['slot'] for c in wt['found']], ['davening'])
        self.assertEqual(report['other'], [{'id': 77777, 'name': 'Some Other Course'}])
        self.assertEqual(report['noNumericId'], ['Odd One'])
        self.assertEqual(report['total'], 5)
        self.assertNotIn('90001', str(report))

    def test_server_lister_result_feeds_the_matching_unchanged(self):
        visible = [c for c in R.parse_options(RAW) if c['id'] is not None]
        db = lambda path: {'b3Games/workspaces/w/teachers/uidtest1/active': True,
                           'b3Games/workspaces/w/teachers/uidtest1/classIds/et': True}.get(path)
        res = N.course_status(db, 'w', 'uidtest1', 'et', object(), lambda c: visible)
        self.assertEqual([c['slot'] for c in res['courses'] if c['available']], ['davening', 'morning1'])


class WiringTests(unittest.TestCase):
    def test_server_reader_is_off_unless_switched_on(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('NIGRI_COURSE_READER', None)
            self.assertIs(N.default_lister(), N.not_connected_lister)
        with patch.dict(os.environ, {'NIGRI_COURSE_READER': '1'}):
            self.assertIs(N.default_lister(), R.server_lister)

    def test_server_lister_failures_become_plain_unavailable(self):
        class C: username, password = USER, PW
        class Slot:
            def __enter__(s): raise ValueError('The browser is busy')
            def __exit__(s, *a): return False
        with patch('browser_runtime.browser_slot', lambda: Slot()):
            with self.assertRaises(N.CoursesUnavailable) as cm:
                R.server_lister(C())
        self.assertNotIn(PW, str(cm.exception))

        class Boom:
            def __enter__(s): return s
            def __exit__(s, *a): return False
        with patch('browser_runtime.browser_slot', lambda: Boom()), \
                patch.object(R, 'login_and_read', lambda u, p, l: (_ for _ in ()).throw(RuntimeError('detail ' + p))):
            with self.assertRaises(N.CoursesUnavailable) as cm:
                R.server_lister(C())
        self.assertNotIn(PW, str(cm.exception))
        self.assertNotIn('detail', str(cm.exception))


class SourceRulesTests(unittest.TestCase):
    root = Path(R.__file__).parent

    def test_reader_never_touches_students_marks_or_saves(self):
        import ast
        tree = ast.parse((self.root / 'nigri_course_reader.py').read_text(encoding='utf-8'))
        for node in ast.walk(tree):   # drop docstrings; comments are not in the syntax tree
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and node.body and isinstance(node.body[0], ast.Expr)                     and isinstance(getattr(node.body[0], 'value', None), ast.Constant) and isinstance(node.body[0].value.value, str):
                node.body.pop(0) if len(node.body) > 1 else node.body.__setitem__(0, ast.Pass())
        code = ast.unparse(tree)
        for word in ('#attend', 'attend', 'student', 'marks', 'select_option', '.check(', '.type(', 'press('):
            self.assertNotIn(word, code.lower(), word)
        self.assertEqual(len(re.findall(r'\.click\(', code)), 2)   # the sign-in button, once in each of the two flows
        self.assertEqual(len(re.findall(r'\.fill\(', code)), 4)   # username + password, once in each flow

    def test_password_is_never_printed_logged_or_written(self):
        for name in ('nigri_course_reader.py', 'read_courses_cli.py'):
            code = (self.root / name).read_text(encoding='utf-8')
            self.assertNotRegex(code, r'print\([^)]*password|logging|logger|write_text\([^)]*password')
        cli = (self.root / 'read_courses_cli.py').read_text(encoding='utf-8')
        self.assertIn('getpass.getpass', cli)
        self.assertIn("json.dumps(result", cli)

    def test_nothing_else_imports_the_reader(self):
        users = [p.name for p in self.root.glob('*.py') if p.name not in ('nigri_course_reader.py', 'read_courses_cli.py')
                 and 'nigri_course_reader' in p.read_text(encoding='utf-8')]
        self.assertEqual(sorted(users), ['nigri_courses.py'])   # only the off-by-default switch


class InspectFrame(Frame):
    """A frame that can describe itself the way the real page script would (names only)."""

    def __init__(self, url, info, options=None):
        super().__init__(options)
        self.url, self.info = url, info

    def evaluate(self, js):
        assert js == R.STRUCTURE_JS
        return dict(self.info)


class InspectPage(FakePage):
    def __init__(self, frames, post_goes_to=SIGNIN_POST, **kw):
        super().__init__(RAW, **kw)
        self._frames, self.post_goes_to = frames, post_goes_to

    def click(self, sel):
        self.actions.append(('click', sel))
        for req in [Req('POST', self.post_goes_to)] + self.extra:
            r = Route(req); self.handler(r); self.results.append((req.method, r.result))
        self.frames = self._frames


HOME = {'title': 'Teacher home', 'hasPasswordField': False, 'frames': [{'id': 'list', 'name': 'list', 'src': '/main/list.asp?classID=28734&x=1'}],
        'fields': [{'tag': 'select', 'type': 'select-one', 'id': 'selNewCours', 'name': 'sel', 'optionCount': 3,
                    'optionsMatchingClassNames': 2, 'optionsWithNumericValue': 3}]}
LOGIN_FORM = {'title': 'Login', 'hasPasswordField': True, 'frames': [], 'fields': [{'tag': 'input', 'type': 'password', 'id': None, 'name': 'password'}]}


class InspectTests(unittest.TestCase):
    def run_inspect(self, frames, **kw):
        launch, closed = launcher(InspectPage(frames, **kw))
        return R.login_and_inspect(USER, PW, launch), closed

    def test_succeeded_says_so_and_reports_names_only(self):
        res, closed = self.run_inspect([InspectFrame(GOOD + '/main/default_os_prog.asp?section=teachers&uid=998877', HOME)])
        self.assertEqual(res['signIn'], 'SUCCEEDED')
        self.assertEqual(res['pages'][0]['title'], 'Teacher home')
        self.assertEqual(res['pages'][0]['address'], GOOD + '/main/default_os_prog.asp?section=teachers&uid=(hidden)')
        self.assertEqual(res['pages'][0]['frames'][0]['src'], '/main/list.asp?classID=(hidden)&x=(hidden)')
        self.assertEqual(res['pages'][0]['fields'][0]['id'], 'selNewCours')
        self.assertEqual(closed, [1])
        blob = str(res)
        for secret in (PW, USER, '998877', '28734'):
            self.assertNotIn(secret, blob)

    def test_failed_when_the_form_is_still_there(self):
        res, _ = self.run_inspect([InspectFrame(LOGIN, LOGIN_FORM)])
        self.assertEqual(res['signIn'], 'FAILED')
        self.assertIn('did not accept', res['signInDetail'])
        self.assertFalse(res['classPickerFound'])

    def test_not_sent_when_the_form_posts_somewhere_else(self):
        res, _ = self.run_inspect([InspectFrame(LOGIN, LOGIN_FORM)], post_goes_to=GOOD + '/main/other.asp?token=abc123')
        self.assertEqual(res['signIn'], 'NOT SENT')
        self.assertIn('stopped before anything was sent', res['signInDetail'])
        self.assertIn('POST ' + GOOD + '/main/other.asp?token=(hidden)', res['blockedRequests'])
        self.assertNotIn('abc123', str(res))

    def test_verdict_rules(self):
        self.assertEqual(R.sign_in_verdict(True, [], [HOME])[0], 'SUCCEEDED')
        self.assertEqual(R.sign_in_verdict(False, [], [HOME])[0], 'COULD NOT TELL')
        self.assertEqual(R.sign_in_verdict(True, [], [HOME, LOGIN_FORM])[0], 'FAILED')

    def test_class_picker_flag_and_courses(self):
        res, _ = self.run_inspect([InspectFrame(GOOD + '/x', HOME, options=RAW)])
        self.assertTrue(res['classPickerFound'])
        self.assertEqual(len(res['courses']), 5)

    def test_unreadable_frame_is_reported_not_fatal(self):
        class Bad(InspectFrame):
            def evaluate(self, js): raise RuntimeError('boom ' + PW)
        res, _ = self.run_inspect([Bad(GOOD + '/x', HOME)])
        self.assertTrue(res['pages'][0]['unreadable'])
        self.assertNotIn(PW, str(res))


class SafeAddressTests(unittest.TestCase):
    def test_hides_values_except_page_type_words(self):
        self.assertEqual(R.safe_address(GOOD + '/m.asp?section=teachers&subSection=tests&side=1&id=77&n=Moshe'),
                         GOOD + '/m.asp?section=teachers&subSection=tests&side=1&id=(hidden)&n=(hidden)')
        self.assertEqual(R.safe_address(GOOD + '/m.asp?section=Moshe%20Cohen'), GOOD + '/m.asp?section=Moshe Cohen'.replace('Moshe Cohen', '(hidden)'))
        self.assertEqual(R.safe_address(None), '(none)')
        self.assertEqual(R.safe_address('/a/b.asp?z=9'), '/a/b.asp?z=(hidden)')
        self.assertNotIn('#frag', R.safe_address(GOOD + '/p#frag'))

    def test_collapse_masks_ids_and_merges_repeats(self):
        rows = [{'tag': 'input', 'type': 'checkbox', 'id': 'present_%d' % n, 'name': 'cb'} for n in (11111, 22222, 33333)]
        rows.append({'tag': 'select', 'type': 'select-one', 'id': 'x', 'name': 'y', 'optionCount': 1})
        out = R.collapse_fields(rows)
        self.assertEqual(out[0]['tag'], 'select')   # dropdowns first
        self.assertEqual([(r['id'], r['count']) for r in out[1:]], [('present_#', 3)])

    def test_structure_script_reads_names_only(self):
        js = R.STRUCTURE_JS
        for banned in ('innerText', 'innerHTML', 'href', 'outerHTML', 'cookie', 'localStorage', 'fetch(', 'click(', 'submit(', '.value =', 'dispatchEvent'):
            self.assertNotIn(banned, js, banned)
        self.assertEqual(js.count('.value'), 1)         # only inside the numeric-count filter, never output
        self.assertEqual(js.count('textContent'), 1)    # only inside the "looks like a class" count, never output

    def test_cli_prints_no_values_and_asks_hidden(self):
        cli = (Path(R.__file__).parent / 'read_courses_cli.py').read_text(encoding='utf-8')
        self.assertIn('getpass.getpass', cli)
        self.assertNotIn("['courses']", cli.split('def main')[1].split('show(result)')[1].replace("result.pop('courses', None)", ''))
        self.assertIn("result.pop('courses', None)", cli)   # course names are not written to the saved file


if __name__ == '__main__':
    unittest.main()
