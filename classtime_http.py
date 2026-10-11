"""Plain-HTTP Classtime sign-in and report export. No browser, so it uses almost no memory.

Mirrors what Classtime's own teacher page sends (same endpoints, same message format).
Credentials come only from CLASSTIME_EMAIL / CLASSTIME_PASSWORD and are never logged or returned.
Every error raised here is a short safe message: no response bodies, cookies, URLs or credentials.
"""
import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests

SERVICE = 'https://www.classtime.com/service/global/'
LANDING = 'https://www.classtime.com/'
SUCCESS_REDIRECT = 'https://www.classtime.com/questions'
EXPORT_HOST = 'export.classtime.com'
# classtime.service.session.api.ReportType: PDF=1, PARTICIPANTS_INSIGHTS_ZIP=2, PARTICIPANTS_INSIGHTS_PDF=3, INSIGHTS_XLSX=4
REPORT_PARTICIPANTS_ZIP = 2
MAX_ZIP_BYTES = 20 * 1024 * 1024
PBKDF2_ROUNDS = 310000
TIMEOUT = (10, 60)


# ---- minimal protobuf encoding/decoding (only what these calls need) ----

def _varint(n):
    out = bytearray()
    while True:
        bits = n & 0x7F
        n >>= 7
        if n:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            return bytes(out)


def _text(no, value):
    data = value.encode('utf-8')
    return _varint(no << 3 | 2) + _varint(len(data)) + data


def _raw(no, data):
    return _varint(no << 3 | 2) + _varint(len(data)) + data


def _number(no, value):
    return _varint(no << 3) + _varint(value)


def _read_fields(buf):
    """Return {field_number: bytes} for length-delimited fields (last one wins)."""
    fields, i = {}, 0
    def varint():
        nonlocal i
        result = shift = 0
        while True:
            if i >= len(buf): raise ValueError('bad message')
            byte = buf[i]; i += 1
            result |= (byte & 0x7F) << shift
            if not byte & 0x80: return result
            shift += 7
    while i < len(buf):
        tag = varint()
        number, wire = tag >> 3, tag & 7
        if wire == 0: varint()
        elif wire == 2:
            length = varint()
            if i + length > len(buf): raise ValueError('bad message')
            fields[number] = bytes(buf[i:i + length]); i += length
        elif wire == 1: i += 8
        elif wire == 5: i += 4
        else: raise ValueError('bad message')
    return fields


def password_hash(email, password):
    """Same scramble as Classtime's page: PBKDF2-SHA256, 310000 rounds, salt 'classtime' + lowercase email."""
    return hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), ('classtime' + email.lower()).encode('utf-8'), PBKDF2_ROUNDS, 32)


def login_request():
    channel = _text(1, 'other') + _text(2, 'organic') + _text(3, LANDING) + _text(8, '/en')
    return _text(5, SUCCESS_REDIRECT) + _raw(10, channel)


def authorize_request(email, hashed, login_state):
    return _raw(2, _text(1, email) + _raw(2, hashed)) + _raw(4, login_state)


def export_request(session_code, time_zone, report_type=REPORT_PARTICIPANTS_ZIP):
    return _text(1, session_code) + _number(3, report_type) + _text(5, time_zone)


class ClasstimeHttp:
    def __init__(self, http=None):
        self.http = http or requests.Session()
        self.http.headers.update({
            'accept': 'application/protobuf,*/*',
            'content-type': 'application/protobuf',
            'origin': 'https://www.classtime.com',
            'referer': LANDING,
            'x-auth-role': '0',
            'x-attempt-count': '1',
            'user-agent': 'Mozilla/5.0 (compatible; NigriSync/1.0)',
        })

    def _call(self, name, body, stage):
        try:
            response = self.http.post(SERVICE + name, data=body, timeout=TIMEOUT)
        except requests.RequestException:
            raise ValueError('Classtime could not be reached at ' + stage + '.') from None
        if response.status_code in (401, 403):
            raise ValueError('Classtime refused the request at ' + stage + ' (status ' + str(response.status_code) + ').')
        if response.status_code != 200:
            raise ValueError('Classtime returned status ' + str(response.status_code) + ' at ' + stage + '.')
        return response.content

    def sign_in(self, email, password):
        state_message = self._call('Account/login', login_request(), 'login step 1')
        login_state = _read_fields(state_message).get(2)
        if not login_state:
            raise ValueError('Classtime login step 1 gave an unexpected reply.')
        reply = self._call('Account/authorize', authorize_request(email, password_hash(email, password), login_state), 'login step 2')
        redirect = _read_fields(reply).get(1, b'').decode('utf-8', 'replace')
        host = urlparse(redirect).hostname or ''
        if not (host == 'classtime.com' or host.endswith('.classtime.com')) or urlparse(redirect).scheme != 'https':
            raise ValueError('Classtime login step 2 gave an unexpected reply.')
        try:
            # The page visits this address next; that is where the browser's session is completed.
            self.http.get(redirect, timeout=TIMEOUT, headers={'accept': 'text/html,*/*'})
        except requests.RequestException:
            raise ValueError('Classtime could not be reached after login.') from None

    def export_zip_url(self, session_code, time_zone='America/New_York'):
        if not re.fullmatch(r'[A-Z0-9]{6}', session_code):
            raise ValueError('A six-character session code is required')
        reply = self._call('Session/exportSession', export_request(session_code, time_zone), 'export')
        url = _read_fields(reply).get(1, b'').decode('utf-8', 'replace')
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname != EXPORT_HOST:
            raise ValueError('Classtime export gave an unexpected reply.')
        return url

    def download_zip(self, url, folder):
        target = Path(folder) / 'reports.zip'
        size = 0
        try:
            # A plain download link: no login or cookies are sent to it.
            with requests.get(url, stream=True, timeout=TIMEOUT) as response:
                if response.status_code != 200:
                    raise ValueError('Classtime report download failed (status ' + str(response.status_code) + ').')
                with target.open('wb') as output:
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > MAX_ZIP_BYTES: raise ValueError('Report archive exceeds the size limit')
                        output.write(chunk)
        except requests.RequestException:
            raise ValueError('Classtime report download failed.') from None
        return target


def fetch_report_zip(session_code, email=None, password=None, client=None):
    """Sign in, request the 'export all' ZIP and save it to a private temp folder. Returns the ZIP path.
    The caller deletes the folder (the ZIP's parent)."""
    email = email or os.environ.get('CLASSTIME_EMAIL')
    password = password or os.environ.get('CLASSTIME_PASSWORD')
    if not email or not password:
        raise ValueError('Classtime credentials are not configured')
    client = client or ClasstimeHttp()
    client.sign_in(email, password)
    url = client.export_zip_url(session_code)
    folder = tempfile.mkdtemp(prefix='classtime-download-')
    try:
        return client.download_zip(url, folder)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
