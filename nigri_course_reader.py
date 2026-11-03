"""Read-only reader: which Nigri courses can THIS login see? (issue 550, step 3b)

Safety rules, enforced in code and tested:
- Only the login itself is ever sent to Nigri: ONE form POST to the sign-in address. Every other request must be a GET;
  anything else (a save, a submit, a change) is aborted by `allow_request` before it leaves the browser.
- It reads only the course picker (select#selNewCours: each option's value and label). It never opens the attendance
  frame, marks, or any student list, so no student data is read, kept or sent anywhere.
- It never clicks anything except the sign-in button and never changes the picker.
- The output is course IDs and names only. Passwords are never printed, logged or put in the output.
Nothing calls this unless a person runs read_courses_cli.py, or NIGRI_COURSE_READER=1 is set on the server (off by default).
"""
import re
from urllib.parse import parse_qs, urlsplit

import nigri_courses as N

NIGRI_BASE_URL = 'https://www.nigrijewishonlineschool.com'
LOGIN_URL = NIGRI_BASE_URL + '/main/default_os_prog.asp?section=teachers'
PICKER = 'select#selNewCours'
SKIP_TYPES = ('image', 'media', 'font')


class ReaderError(Exception):
    """Plain-words failure, never contains a password."""


def allow_request(method, url, state):
    """True if this request may go out. `state` is a dict used to allow exactly one sign-in POST."""
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in ('www.nigrijewishonlineschool.com', 'nigrijewishonlineschool.com'):
        return False
    if method == 'GET':
        return True
    login = urlsplit(LOGIN_URL)
    # Nigri uses ONE page address for everything (sign-in, attendance, marks), told apart only by the query. So the
    # single allowed POST must match the sign-in address EXACTLY, query included; if the real form posts elsewhere the
    # reader stops (fails closed) and says so, rather than letting some other POST through.
    if method == 'POST' and parts.path == login.path and parse_qs(parts.query) == parse_qs(login.query) and not state.get('posted'):
        state['posted'] = True
        return True
    return False


def install_read_only_guard(page):
    state = {'posted': False, 'blocked': []}

    def handler(route):
        req = route.request
        if req.resource_type in SKIP_TYPES:
            return route.abort()
        if allow_request(req.method, req.url, state):
            return route.continue_()
        state['blocked'].append(req.method)
        return route.abort()
    page.route('**/*', handler)
    return state


def read_picker(page):
    """Every option of the course picker, from whichever frame holds it. Reads the DOM only."""
    for frame in page.frames:
        try:
            if frame.query_selector(PICKER):
                return frame.eval_on_selector_all(
                    PICKER + ' option', 'els => els.map(o => ({value: o.value, name: (o.textContent || "").trim()}))')
        except Exception:
            continue
    raise ReaderError('Could not find the class list on Nigri after signing in.')


def parse_options(raw):
    """[{value, name}] -> [{id:int|None, name}] with blanks and shared rooms dropped. Unparseable IDs stay visible as None."""
    courses = []
    for item in raw or []:
        name = str((item or {}).get('name') or '').strip()
        value = str((item or {}).get('value') or '').strip()
        if not name or N.is_shared_room(name):
            continue
        courses.append({'id': int(value) if re.fullmatch(r'\d{1,9}', value) else None, 'name': name})
    return courses


def login_and_read(username, password, launch):
    """`launch()` returns (page, close). Signs in once, reads the picker, closes. Returns raw option dicts."""
    page, close = launch()
    try:
        state = install_read_only_guard(page)
        page.goto(LOGIN_URL)
        page.fill('input[name="login"]', username)
        page.fill('input[name="password"]', password)
        page.click('input[name="submit"]')
        page.wait_for_load_state('networkidle')
        if page.query_selector('input[name="password"]') and not any(f.query_selector(PICKER) for f in page.frames):
            raise ReaderError('Nigri did not accept the sign-in.')
        raw = read_picker(page)
        if state['blocked']:
            raise ReaderError('Stopped: a non-read request was blocked (%s).' % ', '.join(sorted(set(state['blocked']))))
        return raw
    finally:
        close()


def summarize(courses):
    """What the owner reviews: per grade, which known courses were found/missing, and anything unrecognised."""
    by_id = {c['id']: c['name'] for c in courses if c['id'] is not None}
    report = {'total': len(courses), 'noNumericId': [c['name'] for c in courses if c['id'] is None], 'grades': {}}
    known = set()
    for grade, slots in N.COURSES.items():
        found, missing = [], []
        for slot, cid in slots.items():
            known.add(cid)
            (found if cid in by_id else missing).append({'slot': slot, 'label': N.LABELS[slot], 'courseId': cid,
                                                         'nigriName': by_id.get(cid)})
        report['grades'][grade] = {'found': found, 'missing': missing}
    report['other'] = [{'id': i, 'name': n} for i, n in by_id.items() if i not in known]
    return report


def playwright_launcher(channel='chrome'):
    """A launcher for login_and_read using Playwright. Tries the installed Chrome first, then bundled Chromium."""
    def launch():
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch(headless=True, channel=channel) if channel else pw.chromium.launch(headless=True)
        except Exception:
            browser = pw.chromium.launch(headless=True)
        page = browser.new_page()

        def close():
            try: browser.close()
            finally: pw.stop()
        return page, close
    return launch


def server_lister(credentials):
    """The lister the server uses when NIGRI_COURSE_READER=1: this teacher's OWN login, read only."""
    from browser_runtime import browser_slot
    try:
        with browser_slot():
            raw = login_and_read(credentials.username, credentials.password, playwright_launcher(None))
    except N.CoursesUnavailable:
        raise
    except ReaderError as exc:
        raise N.CoursesUnavailable(str(exc).rstrip('.'))
    except ValueError as exc:   # browser_slot busy
        raise N.CoursesUnavailable(str(exc).rstrip('.'))
    except Exception:
        raise N.CoursesUnavailable('Could not read your Nigri classes right now')
    return [c for c in parse_options(raw) if c['id'] is not None]
