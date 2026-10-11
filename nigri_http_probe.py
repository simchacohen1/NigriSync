"""Browser-free, read-only Nigri connectivity probe for the owner's Classtime page.

Permitted network operations: GET login page, POST *only* the sign-in form,
GET the teacher home and existing marks listing. NEVER submit Create Mark!, Save!, delete or
upload. Never return passwords, cookies, hidden field values, HTML, or names.
"""
import os
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, parse_qsl

import requests

BASE = 'https://www.nigrijewishonlineschool.com'
LOGIN_URL = BASE + '/main/default_os_prog.asp?section=teachers'
MARKS_URL = BASE + '/main/default_os_prog.asp?section=teachers&subSection=tests&side=1'
MAX_RESPONSE = 2 * 1024 * 1024
HOST = 'www.nigrijewishonlineschool.com'


def _safe_url(url):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname != HOST or parts.username or parts.password or parts.port not in (None, 443):
        raise ValueError('Nigri redirected outside its approved HTTPS host; no further requests were made')
    return url


class _Page(HTMLParser):
    """Collect structural metadata, never raw student information."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.links = []
        self._form = None
        self._a = None
        self.has_create_text = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'form':
            self._form = {'action': a.get('action', ''), 'method': (a.get('method') or 'get').lower(), 'inputs': []}
            self.forms.append(self._form)
        elif tag == 'input' and self._form is not None:
            self._form['inputs'].append({'name': a.get('name', ''), 'type': (a.get('type') or 'text').lower(), 'value': a.get('value', '')})
        elif tag in ('select', 'textarea') and self._form is not None:
            self._form['inputs'].append({'name': a.get('name', ''), 'type': tag, 'value': ''})
        elif tag == 'button' and self._form is not None and a.get('name'):
            self._form['inputs'].append({'name': a['name'], 'type': a.get('type', 'submit').lower(), 'value': a.get('value', '')})
        elif tag == 'a':
            self._a = {'href': a.get('href', ''), 'label': ''}
            self.links.append(self._a)

    def handle_data(self, data):
        if 'create new mark' in data.lower():
            self.has_create_text = True
        if self._a is not None:
            self._a['label'] += data[:200]

    def handle_endtag(self, tag):
        if tag == 'form':
            self._form = None
        if tag == 'a':
            self._a = None


def _page(response):
    if len(response.content) > MAX_RESPONSE:
        raise ValueError('Nigri returned an unusually large page; probe stopped')
    parsed = _Page()
    parsed.feed(response.text)
    return parsed


def _request(session, method, url, *, data=None, trace=None):
    """Follow redirects only as same-host GETs; never replay a sign-in POST."""
    url = _safe_url(url)
    for _ in range(6):
        response = session.request(method, url, data=data, timeout=(8, 20), allow_redirects=False)
        if trace is not None:
            trace.append(response.status_code)  # Status codes only; never URLs, cookies or page data.
        if response.status_code in (301, 302, 303):
            location = response.headers.get('Location')
            if not location:
                raise ValueError('Nigri returned an incomplete redirect')
            url = _safe_url(urljoin(url, location))
            method, data = 'GET', None
            continue
        if response.status_code in (307, 308):
            raise ValueError('Nigri asked to repeat a request during redirect; probe stopped safely')
        if response.status_code != 200:
            raise ValueError('Nigri returned HTTP ' + str(response.status_code))
        return response
    raise ValueError('Too many Nigri redirects')


def probe():
    """Read only, login + marks index; return safe capability details."""
    username = os.environ.get('NIGRI_USERNAME')
    password = os.environ.get('NIGRI_PASSWORD')
    if not username or not password:
        return {'status': 'credentials_missing', 'nigri_writes': False}
    with requests.Session() as session:
        login = _page(_request(session, 'GET', LOGIN_URL))
        candidates = [f for f in login.forms if {'login', 'password'}.issubset({x['name'] for x in f['inputs']})]
        if len(candidates) != 1:
            return {'status': 'login_form_not_found', 'login_form_count': len(candidates), 'nigri_writes': False}
        form = candidates[0]
        if form['method'] != 'post':
            return {'status': 'unsupported_login_method', 'nigri_writes': False}
        action = _safe_url(urljoin(LOGIN_URL, form['action'] or LOGIN_URL))
        # Retain hidden form tokens solely in memory; never return/log them.
        fields = {x['name']: x['value'] for x in form['inputs'] if x['name'] and x['type'] in ('hidden', 'submit')}
        # Nigri uses an <input type="image" name="submit"> login button.
        # A real click submits submit.x and submit.y, NOT a plain "submit"
        # field. Classic ASP login handlers may depend on those coordinates.
        image_buttons = [x for x in form['inputs'] if x['type'] == 'image' and x['name']]
        if len(image_buttons) == 1:
            fields[image_buttons[0]['name'] + '.x'] = '15'
            fields[image_buttons[0]['name'] + '.y'] = '15'
        fields.update(login=username, password=password)
        post_trace, home_trace, marks_trace = [], [], []
        login_response = _request(session, 'POST', action, data=fields, trace=post_trace)  # SIGN-IN ONLY
        login_after_post = _page(login_response)
        login_field_names = {'login', 'password'}
        def has_login(page):
            return any(login_field_names.issubset({i['name'] for i in f['inputs']}) for f in page.forms)
        post_still_login = has_login(login_after_post)
        cookies_after_post = len(session.cookies)  # Count only; never expose cookie names or values.
        home = _page(_request(session, 'GET', LOGIN_URL, trace=home_trace))  # Read-only teacher home.
        home_still_login = has_login(home)
        # Check whether requests would attach the existing session cookie to the marks GET.
        marks_has_cookie = bool(session.prepare_request(requests.Request('GET', MARKS_URL)).headers.get('Cookie'))
        marks = _page(_request(session, 'GET', MARKS_URL, trace=marks_trace))
        marks_still_login = has_login(marks)
        create = [a for a in marks.links if 'create new mark' in a['label'].lower()]
        field_names = {i['name'] for f in marks.forms for i in f['inputs']}
        recognizable = bool(create or marks.has_create_text or {'testGradeID', 'testName', 'testDate'} & field_names)
        diagnostics = {
            'login_form_returned_after_post': post_still_login,
            'login_form_at_teacher_home': home_still_login,
            'login_form_at_marks': marks_still_login,
            'image_button_submitted': len(image_buttons) == 1,
            'cookies_after_login_count': cookies_after_post,
            'cookies_sent_to_marks': marks_has_cookie,
            'login_post_redirect_count': len(post_trace) - 1,
            'teacher_home_redirect_count': len(home_trace) - 1,
            'marks_redirect_count': len(marks_trace) - 1,
            'marks_create_text_found': marks.has_create_text,
            'nigri_writes': False,
        }
        # A login form in a shared header is not enough to prove login failure if
        # the page also contains the authenticated marks controls.
        if marks_still_login and not recognizable:
            return {'status': 'login_not_accepted', **diagnostics}
        kind = 'none'
        keys = []
        if create:
            href = create[0]['href']
            kind = 'javascript' if href.lower().startswith('javascript:') else 'http_link'
            if kind == 'http_link':
                target = _safe_url(urljoin(MARKS_URL, href))
                keys = sorted(set(k for k, _ in parse_qsl(urlsplit(target).query)))[:16]
        return {'status': 'marks_listing_found' if recognizable else 'marks_page_unconfirmed',
                **diagnostics, 'http_login_succeeded': recognizable, 'create_link_found': bool(create),
                'create_link_kind': kind, 'create_link_query_keys': keys,
                'form_count': len(marks.forms), 'nigri_writes': False}



def _mark_entry_readonly_summary(page):
    """Inspect structure of the already-fetched marks listing. Zero extra HTTP requests.

    We deliberately DO NOT follow the Create New Mark link here. A URL
    advertised by a third-party site is not proof that GET is side-effect-free.
    Return only a preapproved set of control names and query keys, never values.
    """
    from urllib.parse import urlsplit
    known_fields = {
        'testGradeID', 'testTopicID', 'testName', 'testDate',
        'testDesc', 'testCompleted',
    }
    fields = {i.get('name', '') for f in page.forms for i in f.get('inputs', [])}
    detected = sorted(known_fields & fields)
    links = [a for a in page.links if 'create new mark' in a.get('label', '').lower()]
    target_kind, query_keys, action_kind = 'not_found', [], 'unknown'
    if links:
        href = links[0]['href'].strip()
        if href.lower().startswith('javascript:'):
            target_kind = 'javascript'
        elif href.startswith('#'):
            target_kind = 'fragment'
        else:
            try:
                absolute = _safe_url(urljoin(MARKS_URL, href))
                query = parse_qsl(urlsplit(absolute).query, keep_blank_values=True)
                query_keys = sorted({k for k, _ in query
                                     if k in {'section', 'spec', 'action', 'subSection', 'side'}})
                actions = [v.lower() for k, v in query if k == 'action']
                if len(actions) == 1 and actions[0] in {
                    'new_test', 'add_test', 'create_test', 'new', 'new_mark',
                    'add', 'create', 'new_test_form',
                }:
                    action_kind = actions[0]
                target_kind = 'same_host_http_link'
            except ValueError:
                target_kind = 'external_or_unsafe'
    return {
        'entry_link_kind': target_kind,
        'entry_link_action_kind': action_kind,
        'entry_link_query_keys': query_keys,
        'known_form_field_names': detected,
        'create_button_text_found': page.has_create_text,
        'forms_on_listing': len(page.forms),
        'entry_form_was_not_opened': True,
    }


def _hybrid_login_router(route):
    """Skip Nigri login images/fonts/media only; preserve all functional requests."""
    if route.request.resource_type in ('image', 'media', 'font', 'texttrack'):
        return route.abort()
    return route.continue_()


# Only the already displayed "Create New Mark" link is eligible for the
# new read-only form probe. Never guess a mark URL, follow a link with IDs,
# or execute a GET with save/delete/submit/edit semantics.
_SAFE_NEW_MARK_QUERY_KEYS = frozenset({
    'section', 'subSection', 'spec', 'side', 'action', 'mode', 'task', 'view',
})
_SAFE_NEW_MARK_ACTIONS = frozenset({
    'new', 'new_test', 'new_mark', 'new_test_form', 'form',
})


def _safe_blank_mark_url(marks):
    links = [a for a in marks.links if 'create new mark' in a.get('label', '').lower()]
    if len(links) != 1:
        return None, 'link_missing_or_ambiguous'
    href = links[0].get('href', '').strip()
    if not href or href.startswith('#') or href.lower().startswith('javascript:'):
        return None, 'link_not_a_get'
    try:
        target = _safe_url(urljoin(MARKS_URL, href))
        bits = urlsplit(target)
        if bits.path.lower() != '/main/default_os_prog.asp' or bits.fragment:
            return None, 'link_unrecognized'
        pairs = parse_qsl(bits.query, keep_blank_values=True)
        if not pairs or len(pairs) > 9 or len(set(k for k, _ in pairs)) != len(pairs):
            return None, 'link_unrecognized'
        params = dict(pairs)
        if set(params) - _SAFE_NEW_MARK_QUERY_KEYS:
            return None, 'link_unrecognized'
        if params.get('section', '').lower() != 'teachers':
            return None, 'link_unrecognized'
        if (params.get('spec', '').lower() != 'tests'
                and params.get('subSection', '').lower() != 'tests'):
            return None, 'link_unrecognized'
        for key in ('action', 'mode', 'task', 'view'):
            if key in params and params[key].lower() not in _SAFE_NEW_MARK_ACTIONS:
                return None, 'link_unrecognized'
        # A link back to the School Marks listing is NOT the New Mark form.
        if target == MARKS_URL:
            return None, 'link_is_listing'
        return target, 'allowed'
    except (ValueError, UnicodeError):
        return None, 'link_unrecognized'


def _blank_mark_link_diagnostics(marks):
    """Safe structural facts for a rejected Create New Mark link.

    No GET/POST, and never disclose URL, query values, unknown key names,
    credentials, session cookies or student information.
    """
    links = [a for a in marks.links if 'create new mark' in a.get('label', '').lower()]
    if len(links) != 1:
        return {'link_count': 'none' if not links else 'multiple',
                'no_navigation': True}
    href = links[0].get('href', '').strip()
    if href.lower().startswith('javascript:'):
        return {'link_kind': 'javascript', 'no_navigation': True}
    if href.startswith('#'):
        return {'link_kind': 'fragment', 'no_navigation': True}
    if not href:
        return {'link_kind': 'empty', 'no_navigation': True}
    try:
        target = _safe_url(urljoin(MARKS_URL, href))
        url = urlsplit(target)
        pairs = parse_qsl(url.query, keep_blank_values=True)
        params = dict(pairs)
        keys = {k for k, _ in pairs}
        known = sorted(keys & _SAFE_NEW_MARK_QUERY_KEYS)
        action_flags = [k for k in ('action', 'mode', 'task', 'view')
                        if k in params and params[k].lower() not in _SAFE_NEW_MARK_ACTIONS]
        return {
            'link_kind': 'same_host_https',
            'expected_asp_path': url.path.lower() == '/main/default_os_prog.asp',
            'has_fragment': bool(url.fragment),
            'query_key_count': min(len(pairs), 20),
            'recognized_query_keys': known,
            'unknown_query_key_count': min(len(keys - _SAFE_NEW_MARK_QUERY_KEYS), 20),
            'has_duplicate_keys': len(pairs) != len(keys),
            'section_is_teachers': params.get('section', '').lower() == 'teachers',
            'tests_section_present': (params.get('spec', '').lower() == 'tests' or
                                      params.get('subSection', '').lower() == 'tests'),
            'unsupported_action_key_count': len(action_flags),
            'is_marks_listing': target == MARKS_URL,
            'no_navigation': True,
        }
    except (ValueError, UnicodeError):
        return {'link_kind': 'other_host_or_invalid', 'no_navigation': True}


def _probe_blank_mark_form(session, marks):
    """One carefully validated GET, never a submit/POST. No redirects followed."""
    target, why = _safe_blank_mark_url(marks)
    if not target:
        return {'status': 'blank_form_link_' + why, 'link_diagnostics': _blank_mark_link_diagnostics(marks), 'nigri_writes': False}
    response = session.request('GET', target, timeout=(8, 20), allow_redirects=False)
    if response.status_code != 200:
        return {'status': 'blank_form_get_not_200',
                'http_status': response.status_code,
                'redirect_not_followed': response.status_code in (301, 302, 303, 307, 308),
                'nigri_writes': False}
    page = _page(response)
    # Only approved field names; never expose field values, options, HTML,
    # personal information, form actions, session cookies, or student records.
    recognized = frozenset({
        'testGradeID', 'testTopicID', 'testName', 'testDate',
        'testDesc', 'testType', 'testCompleted',
    })
    inputs = [i for f in page.forms for i in f['inputs']]
    names = sorted({i['name'] for i in inputs
                    if i['name'] in recognized or i['name'].startswith('testType_') and
                    len(i['name']) < 35 and i['name'][9:].isalnum()})
    has_blank = {'testGradeID', 'testName'}.issubset(names)
    create_button = any(i['type'] in ('submit', 'image', 'button')
                        and i.get('value', '').strip() == 'Create Mark!' for i in inputs)
    return {
        'status': 'blank_form_found' if has_blank else 'blank_form_unconfirmed',
        'form_detected': has_blank,
        'field_names': names[:30],
        'form_count': len(page.forms),
        'create_button_present': create_button,
        'only_one_extra_get': True,
        'no_submit_performed': True,
        'nigri_writes': False,
    }


def probe_hybrid(*, inspect_blank_form=False):
    """Read-only login through Chromium, then CLOSE it and GET marks over HTTP.

    When inspect_blank_form is True, follow ONLY an allowlisted GET from
    Nigri's authenticated Create New Mark link; never submit a form.
    No Create Mark!, Save!, upload or delete requests. Cookies stay in memory
    only; returned results contain no credentials, cookie details or student data.
    """
    from browser_runtime import log_memory, memory_snapshot, MIB, STOP_PERCENT
    from classtime_reader import _chromium_args
    from nigri_playwright import login
    from playwright.sync_api import sync_playwright

    if not os.environ.get('NIGRI_USERNAME') or not os.environ.get('NIGRI_PASSWORD'):
        return {'status': 'credentials_missing', 'nigri_writes': False}

    stage = 'starting_browser'
    browser_cookies = []
    user_agent = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=_chromium_args() + [
                '--disk-cache-size=0', '--media-cache-size=0',
            ])
            log_memory('hybrid: chromium launched')
            try:
                # The cold-start browser sometimes exceeds 350 MiB BEFORE any
                # page loads. Abort early rather than letting login trigger
                # Render's 430 MiB memory guard. Hybrid only; don't change
                # Attendance, Weekly Quiz or Classtime retrieval.
                snapshot = memory_snapshot()
                if snapshot and snapshot['working_set'] + 105 * MIB >= snapshot['limit'] * STOP_PERCENT / 100:
                    return {'status': 'hybrid_insufficient_headroom', 'nigri_writes': False}
                context = browser.new_context()
                # Asset-heavy teacher login UI is not needed to obtain cookies;
                # keep scripts, documents, CSS and API calls untouched.
                context.route('**/*', _hybrid_login_router)
                page = context.new_page()
                stage = 'browser_signin'
                login(page)  # Uses the existing proven browser sign-in.
                log_memory('hybrid: browser sign-in completed')
                browser_cookies = [
                    x for x in context.cookies()
                    if x.get('name') and x.get('value')
                    and (x.get('domain', '').lstrip('.').lower() == HOST
                         or x.get('domain', '').lstrip('.').lower() == 'nigrijewishonlineschool.com')
                ]
                user_agent = page.evaluate('navigator.userAgent')
            finally:
                browser.close()
                log_memory('hybrid: chromium closed before HTTP')
        stage = 'http_marks'
        log_memory('hybrid: playwright driver closed')
        with requests.Session() as session:
            session.headers['User-Agent'] = user_agent or 'Mozilla/5.0'
            for ck in browser_cookies:
                session.cookies.set(
                    ck['name'], ck['value'], domain=ck['domain'],
                    path=ck.get('path') or '/', secure=ck.get('secure', True),
                )
            cookies_would_send = bool(
                session.prepare_request(requests.Request('GET', MARKS_URL)).headers.get('Cookie')
            )
            response = _request(session, 'GET', MARKS_URL)  # Strictly read only.
            marks = _page(response)
            form_names = {i['name'] for f in marks.forms for i in f['inputs']}
            has_marks = bool(
                marks.has_create_text
                or any('create new mark' in x['label'].lower() for x in marks.links)
                or {'testGradeID', 'testName', 'testDate'} & form_names
            )
            has_login_form = any(
                {'login', 'password'}.issubset({i['name'] for i in f['inputs']})
                for f in marks.forms
            )
            result = {
                'status': 'hybrid_marks_found' if has_marks else 'hybrid_marks_unconfirmed',
                'marks_controls_found': has_marks,
                'login_form_on_marks': has_login_form,
                'browser_closed_before_http': True,
                'transferred_cookie_count': len(browser_cookies),
                'cookies_sent_to_marks': cookies_would_send,
                'mark_entry_readonly': _mark_entry_readonly_summary(marks),
                'nigri_writes': False,
            }
            if inspect_blank_form:
                if not has_marks:
                    result['blank_form'] = {'status': 'blank_form_marks_unconfirmed', 'nigri_writes': False}
                else:
                    result['blank_form'] = _probe_blank_mark_form(session, marks)
            return result
    except Exception:
        # Never return exception text (could include sensitive URLs/cookies).
        return {'status': 'hybrid_' + stage + '_stopped', 'nigri_writes': False}
