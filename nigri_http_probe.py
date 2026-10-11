"""Browser-free, read-only Nigri connectivity probe for the owner's Classtime page.

Permitted network operations: GET login page, POST *only* the sign-in form,
GET the existing marks listing. NEVER submit Create Mark!, Save!, delete or
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

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'form':
            self._form = {'action': a.get('action', ''), 'method': (a.get('method') or 'get').lower(), 'inputs': []}
            self.forms.append(self._form)
        elif tag == 'input' and self._form is not None:
            self._form['inputs'].append({'name': a.get('name', ''), 'type': (a.get('type') or 'text').lower(), 'value': a.get('value', '')})
        elif tag == 'button' and self._form is not None and a.get('name'):
            self._form['inputs'].append({'name': a['name'], 'type': a.get('type', 'submit').lower(), 'value': a.get('value', '')})
        elif tag == 'a':
            self._a = {'href': a.get('href', ''), 'label': ''}
            self.links.append(self._a)

    def handle_data(self, data):
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


def _request(session, method, url, *, data=None):
    """Follow redirects only as same-host GETs; never replay a sign-in POST."""
    url = _safe_url(url)
    for _ in range(6):
        response = session.request(method, url, data=data, timeout=(8, 20), allow_redirects=False)
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
        login_response = _request(session, 'POST', action, data=fields)  # SIGN-IN ONLY
        login_after_post = _page(login_response)
        login_field_names = {'login', 'password'}
        post_still_login = any(login_field_names.issubset({i['name'] for i in f['inputs']}) for f in login_after_post.forms)
        marks = _page(_request(session, 'GET', MARKS_URL))
        if any(login_field_names.issubset({i['name'] for i in f['inputs']}) for f in marks.forms):
            return {'status': 'login_not_accepted',
                    'login_form_returned_after_post': post_still_login,
                    'image_button_submitted': len(image_buttons) == 1,
                    'nigri_writes': False}
        create = [a for a in marks.links if 'create new mark' in a['label'].lower()]
        field_names = {i['name'] for f in marks.forms for i in f['inputs']}
        recognizable = bool(create or {'testGradeID', 'testName', 'testDate'} & field_names)
        kind = 'none'
        keys = []
        if create:
            href = create[0]['href']
            kind = 'javascript' if href.lower().startswith('javascript:') else 'http_link'
            if kind == 'http_link':
                target = _safe_url(urljoin(MARKS_URL, href))
                keys = sorted(set(k for k, _ in parse_qsl(urlsplit(target).query)))[:16]
        return {'status': 'marks_listing_found' if recognizable else 'marks_page_unconfirmed',
                'http_login_succeeded': recognizable, 'create_link_found': bool(create),
                'create_link_kind': kind, 'create_link_query_keys': keys,
                'form_count': len(marks.forms), 'nigri_writes': False}
