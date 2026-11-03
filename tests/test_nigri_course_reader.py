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

    def click(self, sel):
        self.actions.append(('click', sel))
        for req in [Req('POST', LOGIN)] + self.extra:
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
        self.assertTrue(R.allow_request('POST', LOGIN, s))
        self.assertFalse(R.allow_request('POST', LOGIN, s))   # a second POST, even to the login page
        for url in (GOOD + '/main/default_os_prog.asp', GOOD + '/main/default_os_prog.asp?section=teachers&subSection=tests&side=1',
                    GOOD + '/main/default_os_prog.asp?section=teachers&save=1'):
            self.assertFalse(R.allow_request('POST', url, {}), url)   # same page address, different query: refused
        for method in ('POST', 'PUT', 'DELETE', 'PATCH'):
            self.assertFalse(R.allow_request(method, GOOD + '/main/default_os_prog.asp?subSection=tests&save=1', {}), method)

    def test_other_hosts_and_schemes_never_allowed(self):
        for url in ('https://evil.example.test/', 'http://www.nigrijewishonlineschool.com/x', 'https://www.nigrijewishonlineschool.com.evil.test/x',
                    'https://nigrisync.onrender.com/x', 'https://b3-games-default-rtdb.firebaseio.com/x.json'):
            self.assertFalse(R.allow_request('GET', url, {}), url)

    def test_guard_aborts_writes_and_images(self):
        page = FakePage(RAW)
        state = R.install_read_only_guard(page)
        for req, want in ((Req('GET', GOOD + '/a.asp'), 'continue'), (Req('GET', GOOD + '/i.png', 'image'), 'abort'),
                          (Req('POST', GOOD + '/main/save.asp'), 'abort'), (Req('POST', LOGIN), 'continue'), (Req('POST', LOGIN), 'abort')):
            r = Route(req); page.handler(r)
            self.assertEqual(r.result, want, (req.method, req.url))
        self.assertEqual(state['blocked'], ['POST', 'POST'])


class ReadTests(unittest.TestCase):
    def test_reads_only_the_picker_and_closes(self):
        page = FakePage(RAW)
        launch, closed = launcher(page)
        raw = R.login_and_read(USER, PW, launch)
        self.assertEqual(raw, RAW)
        self.assertEqual(closed, [1])
        self.assertEqual([a[0] for a in page.actions], ['goto', 'fill', 'fill', 'click'])
        self.assertEqual(page.actions[0][1], LOGIN)
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
        self.assertEqual(len(re.findall(r'\.click\(', code)), 1)
        self.assertEqual(len(re.findall(r'\.fill\(', code)), 2)

    def test_password_is_never_printed_logged_or_written(self):
        for name in ('nigri_course_reader.py', 'read_courses_cli.py'):
            code = (self.root / name).read_text(encoding='utf-8')
            self.assertNotRegex(code, r'print\([^)]*password|logging|logger|write_text\([^)]*password')
        cli = (self.root / 'read_courses_cli.py').read_text(encoding='utf-8')
        self.assertIn('getpass.getpass', cli)
        self.assertIn("json.dumps(report", cli)

    def test_nothing_else_imports_the_reader(self):
        users = [p.name for p in self.root.glob('*.py') if p.name not in ('nigri_course_reader.py', 'read_courses_cli.py')
                 and 'nigri_course_reader' in p.read_text(encoding='utf-8')]
        self.assertEqual(sorted(users), ['nigri_courses.py'])   # only the off-by-default switch


if __name__ == '__main__':
    unittest.main()
