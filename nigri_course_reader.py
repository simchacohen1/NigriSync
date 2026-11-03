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


NIGRI_HOSTS = ('www.nigrijewishonlineschool.com', 'nigrijewishonlineschool.com')
# Nigri's real teacher sign-in POST (seen in the owner's first run): same page, with exactly these query parts.
SIGNIN_POST_KEYS = {'section', 'subSection', 'login', 'pagesreturn'}
TRACKING_SUFFIXES = ('googletagmanager.com', 'google-analytics.com', 'analytics.google.com', 'doubleclick.net', 'clarity.ms',
                     'googlesyndication.com', 'googleadservices.com', 'facebook.net', 'connect.facebook.com', 'hotjar.com',
                     'bat.bing.com', 'c.bing.com')


def is_tracking(url):
    host = (urlsplit(str(url or '')).hostname or '').lower()
    return any(host == t or host.endswith('.' + t) for t in TRACKING_SUFFIXES)


def is_signin_post(url):
    """The one POST Nigri's sign-in form makes: same host and path as the sign-in page, section=teachers, and exactly the
    query keys section, subSection, login, pagesreturn (each once, values short). Any other key, or a missing one: no."""
    parts = urlsplit(url)
    login = urlsplit(LOGIN_URL)
    if parts.scheme != 'https' or parts.hostname not in NIGRI_HOSTS or parts.path != login.path:
        return False
    query = parse_qs(parts.query, keep_blank_values=True)
    if set(query) != SIGNIN_POST_KEYS or any(len(v) != 1 for v in query.values()):
        return False
    if query['section'] != ['teachers']:
        return False
    return all(len(v[0]) <= 200 and v[0].isprintable() for v in query.values())


def allow_request(method, url, state):
    """True if this request may go out. `state` is a dict used to allow exactly one sign-in POST."""
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in NIGRI_HOSTS:
        return False
    if method == 'GET':
        return True
    # Nigri uses ONE page address for everything (sign-in, attendance, marks), told apart only by the query, so the single
    # allowed POST must match the sign-in POST exactly (is_signin_post). Anything else stops the run and is reported.
    if method == 'POST' and is_signin_post(url) and not state.get('posted'):
        state['posted'] = True
        return True
    return False


def install_read_only_guard(page):
    state = {'posted': False, 'blocked': [], 'blocked_detail': [], 'tracking_blocked': 0}

    def handler(route):
        req = route.request
        if req.resource_type in SKIP_TYPES:
            return route.abort()
        if is_tracking(req.url):   # analytics/ads scripts: dropped quietly, never a failure
            state['tracking_blocked'] += 1
            return route.abort()
        if allow_request(req.method, req.url, state):
            return route.continue_()
        state['blocked'].append(req.method)
        state['blocked_detail'].append(req.method + ' ' + safe_address(req.url))
        return route.abort()
    page.route('**/*', handler)
    return state


def blocked_writes(state):
    """Blocked requests that were not plain reads: the only ones that make a run fail."""
    return [d for d in state['blocked_detail'] if not d.startswith('GET ')]


SAFE_QUERY = {'section', 'subSection', 'side'}   # page-type words only; every other value is hidden
_SAFE_VALUE = re.compile(r'[A-Za-z]{1,30}|\d{1,2}')


def safe_address(url):
    """Address for the owner to read: host + path, query NAMES only (values shown only for page-type words)."""
    parts = urlsplit(str(url or ''))
    shown = []
    for key, values in parse_qs(parts.query, keep_blank_values=True).items():
        key = re.sub(r'[^A-Za-z0-9_.-]', '?', key)[:40]
        value = values[0] if values else ''
        shown.append('%s=%s' % (key, value if key in SAFE_QUERY and _SAFE_VALUE.fullmatch(value) else '(hidden)'))
    base = '%s://%s%s' % (parts.scheme or '?', parts.netloc, parts.path) if parts.netloc else (parts.path or '(none)')
    return base + ('?' + '&'.join(shown) if shown else '')


# Reads names and types only. NEVER values, option text, page text, links or table contents (those could hold student data).
STRUCTURE_JS = r"""() => {
  const cap = (v, n) => (v == null ? null : String(v).slice(0, n));
  const every = [...document.querySelectorAll('input,select,textarea,button')];
  const picked = every.filter(e => e.tagName === 'SELECT').slice(0, 40).concat(every.filter(e => e.tagName !== 'SELECT').slice(0, 400));
  const fields = picked.map(e => {
    const tag = e.tagName.toLowerCase();
    const row = {tag, type: cap(e.type, 20) || null, id: cap(e.id, 60) || null, name: cap(e.name, 60) || null};
    if (tag === 'select') {
      const opts = [...e.options];
      row.optionCount = opts.length;
      row.optionsMatchingClassNames = opts.filter(o => /B3 (ET|WT)/i.test(o.textContent || '')).length;
      row.optionsWithNumericValue = opts.filter(o => /^\d+$/.test(o.value)).length;
    }
    return row;
  });
  const frames = [...document.querySelectorAll('iframe,frame')].slice(0, 20).map(f => ({id: cap(f.id, 60) || null, name: cap(f.name, 60) || null, src: f.getAttribute('src')}));
  return {title: cap(document.title, 120), hasPasswordField: !!document.querySelector('input[type=password]'), fields, frames};
}"""


def _mask(text):
    """Long digit runs in ids/names could be student numbers: show them as #."""
    return re.sub(r'\d{3,}', '#', text) if isinstance(text, str) else text


def collapse_fields(fields, limit=60):
    """Mask ID-like digits, merge repeats (one row per kind of field, with a count), keep dropdowns first."""
    merged, order = {}, []
    for f in fields:
        row = dict(f, id=_mask(f.get('id')), name=_mask(f.get('name')))
        key = (row['tag'], row.get('type'), row['id'], row['name'], row.get('optionCount'),
               row.get('optionsMatchingClassNames'), row.get('optionsWithNumericValue'))
        if key in merged:
            merged[key]['count'] += 1
        else:
            row['count'] = 1
            merged[key] = row
            order.append(key)
    rows = [merged[k] for k in order]
    return ([r for r in rows if r['tag'] == 'select'] + [r for r in rows if r['tag'] != 'select'])[:limit]


def inspect_page(page):
    """Structure of the page and every frame: title, masked address, field/dropdown names. Read only."""
    out = []
    for frame in page.frames:
        try:
            info = frame.evaluate(STRUCTURE_JS)
        except Exception:
            info = {'title': None, 'hasPasswordField': False, 'fields': [], 'frames': [], 'unreadable': True}
        info['fields'] = collapse_fields(info.get('fields', []))
        info['address'] = safe_address(getattr(frame, 'url', ''))
        for sub in info.get('frames', []):
            sub['src'] = safe_address(sub.get('src'))
        out.append(info)
    return out


def sign_in_verdict(posted, blocked_detail, structure):
    """(verdict, plain sentence). SUCCEEDED only if the sign-in was sent and no password box is left on screen."""
    if blocked_detail and not posted:
        return 'NOT SENT', 'The sign-in form tried to send to an address this tool does not allow, so it was stopped before anything was sent: ' + '; '.join(blocked_detail)
    if any(f.get('hasPasswordField') for f in structure):
        return 'FAILED', 'The sign-in form is still showing after sending it, so Nigri did not accept the sign-in.'
    if posted:
        return 'SUCCEEDED', 'The sign-in was sent and the sign-in form is gone, so Nigri accepted it.'
    return 'COULD NOT TELL', 'The sign-in button was clicked but no sign-in request was seen.'


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
        if blocked_writes(state):
            raise ReaderError('Stopped: a non-read request was blocked (%s).' % ', '.join(sorted({d.split(' ')[0] for d in blocked_writes(state)})))
        return raw
    finally:
        close()


def login_and_inspect(username, password, launch):
    """For the owner's own check: sign in once, then report the verdict and the page structure (names only)."""
    page, close = launch()
    try:
        state = install_read_only_guard(page)
        page.goto(LOGIN_URL)
        page.fill('input[name="login"]', username)
        page.fill('input[name="password"]', password)
        page.click('input[name="submit"]')
        page.wait_for_load_state('networkidle')
        try:
            page.wait_for_timeout(1500)   # frames can finish loading a moment after the page
        except Exception:
            pass
        structure = inspect_page(page)
        verdict, sentence = sign_in_verdict(state['posted'], blocked_writes(state), structure)
        picker = None
        for frame in page.frames:
            try:
                if frame.query_selector(PICKER):
                    picker = parse_options(frame.eval_on_selector_all(
                        PICKER + ' option', 'els => els.map(o => ({value: o.value, name: (o.textContent || "").trim()}))'))
                    break
            except Exception:
                continue
        return {'signIn': verdict, 'signInDetail': sentence, 'blockedRequests': state['blocked_detail'], 'trackingScriptsDropped': state['tracking_blocked'],
                'pages': structure, 'classPickerFound': picker is not None, 'courses': picker}
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
