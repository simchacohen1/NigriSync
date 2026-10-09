"""Read-only session review, explicit class-scoped matching and PDF verification."""
import io
import re
import time
import uuid
import threading
import hashlib
import unicodedata
from nigri_playwright import REWARDS_CHILD_IDS, _MARKS_CLASS_STUDENTS

ALIASES = {
    'Mayer Chaikin': 'Chaikin Mayer Chaim',
    'Moshe Raichman': 'Raichman Moshe Tuvia',
    'Sholom Huebner': 'Huebner Sholom DovBer',
}

def normalize(s):
    return ' '.join(re.findall(r'[a-z0-9]+', unicodedata.normalize('NFKC', s).lower()))

def match_student(name, section):
    roster = _MARKS_CLASS_STUDENTS[section]
    canonical = ALIASES.get(name)
    if canonical is None:
        candidates = [n for n in roster if sorted(normalize(n).split()) == sorted(normalize(name).split())]
        canonical = candidates[0] if len(candidates) == 1 else None
    if canonical not in roster:
        return None
    return {'name': canonical, 'child_id': REWARDS_CHILD_IDS[canonical], 'class_section': section}

def parse_session(rows, text, code, section):
    if not re.search(r'\b' + re.escape(code) + r'\b', text):
        raise ValueError('The page does not identify the requested session')
    title = re.search(r'Session:\s*"([^"]+)"', text)
    if not title or not re.search(r'\b' + re.escape(section) + r'\b', text):
        raise ValueError('Session class does not match the selected Nigri class')
    if not re.search(r'\b' + re.escape(section) + r'\b', title.group(1)):
        raise ValueError('Quiz title does not identify the selected class')
    total = re.search(r'([\d.]+)\s*pts\b', text)
    if not total or float(total.group(1)) <= 0:
        raise ValueError('Could not verify the maximum points')
    maximum = float(total.group(1))
    students = []
    seen = set()
    for row in rows:
        lines = [s.strip() for s in row.splitlines() if s.strip()]
        if not lines or lines[0].startswith('Sort by'): continue
        mapping = match_student(lines[0], section)
        # Only accept the one overall score in a student row. Question values
        # would make the row ambiguous and must not be silently summed.
        scores = [s for s in lines[1:] if re.fullmatch(r'\d+(?:\.\d+)?|–|—', s)]
        if len(scores) != 1: raise ValueError('Student score row is ambiguous')
        points = None if scores[0] in ('–', '—') else float(scores[0])
        if points is not None and not 0 <= points <= maximum: raise ValueError('Student score is out of range')
        if mapping and mapping['child_id'] in seen: raise ValueError('Duplicate student mapping')
        if mapping: seen.add(mapping['child_id'])
        students.append({'classtime_name': lines[0], 'nigri': mapping, 'points': points, 'maximum_points': maximum, 'percentage': None if points is None else round(points / maximum * 100, 2), 'pdf_status': 'pending'})
    if not students: raise ValueError('No student score rows were found')
    return {'session_code': code, 'session_name': title.group(1), 'class_section': section, 'students': students, 'read_only': True, 'sync_enabled': False, 'nigri_writes': False}

def verify_pdf(data, students, session_code=None, class_section=None):
    if len(data) > 8 * 1024 * 1024 or not data.startswith(b'%PDF-'):
        raise ValueError('Invalid or oversized PDF')
    from pypdf import PdfReader
    pdf = PdfReader(io.BytesIO(data))
    if pdf.is_encrypted or not 0 < len(pdf.pages) <= 100:
        raise ValueError('PDF cannot be verified')
    text = '\n'.join(page.extract_text() or '' for page in pdf.pages)
    # Verify the student identity in the PDF contents, never just its filename.
    normalized = ' ' + normalize(text) + ' '
    matches = [s for s in students if ' ' + normalize(s['classtime_name']) + ' ' in normalized]
    if len(matches) != 1:
        raise ValueError('PDF student identity is missing or ambiguous')
    student = matches[0]
    if session_code and not re.search(r'Session:\s*' + re.escape(session_code) + r'\b', text):
        raise ValueError('PDF identifies a different session')
    if class_section and not re.search(r'Class:\s*' + re.escape(class_section) + r'\b', text):
        raise ValueError('PDF identifies a different class')
    if session_code:
        score = re.search(r'(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*points\b', text)
        if not score or student['points'] is None or float(score.group(1)) != student['points'] or float(score.group(2)) != student['maximum_points']:
            raise ValueError('PDF score does not match the reviewed grade')
    return student, text

JOBS = {}
LOCK = threading.Lock()
TTL = 3600

def start_review(owner, code, section, loader):
    now = time.time()
    with LOCK:
        for key in list(JOBS):
            if JOBS[key]['expires'] < now: del JOBS[key]
        # Only the verified website owner and the internal diagnostic can
        # create jobs. Reuse the same retrieved source without another browser.
        for prior in list(JOBS.values()):
            result = prior.get('result', {})
            if prior['status'] == 'ready' and result.get('session_code') == code and result.get('class_section') == section:
                key = uuid.uuid4().hex
                JOBS[key] = dict(prior, owner=owner)
                return key
        # Keep at most two distinct report sets to bound resident memory.
        ready = [key for key, job in JOBS.items() if job['status'] != 'loading']
        while len(ready) >= 2:
            del JOBS[ready.pop(0)]
        active = [j for j in JOBS.values() if j['status'] == 'loading']
        if active: raise ValueError('A review is already loading. Please wait for it to finish.')
        key = uuid.uuid4().hex
        JOBS[key] = {'owner': owner, 'expires': now + TTL, 'status': 'loading', 'detail': 'Signing in and retrieving session reports', 'pdfs': {}}
    def run():
        try:
            result, pdfs = loader(code, section)
            with LOCK: JOBS[key].update(status='ready', result=result, pdfs=pdfs)
        except Exception as e:
            # Export errors must be safe application messages, never raw browser traces.
            with LOCK: JOBS[key].update(status='error', detail=str(e) if isinstance(e, ValueError) else 'Classtime export failed. No Nigri data was changed.')
    threading.Thread(target=run, daemon=True).start()
    return key

def get_review(key, owner):
    with LOCK:
        job = JOBS.get(key)
        if not job or job['owner'] != owner or job['expires'] < time.time(): return None
        return job.copy()
