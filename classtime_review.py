"""Read-only session review, explicit class-scoped matching and PDF verification."""
import io
from pathlib import Path
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
    is_path = isinstance(data, Path)
    size = data.stat().st_size if is_path else len(data)
    if is_path:
        with data.open('rb') as source: magic = source.read(5)
    else: magic = data[:5]
    if size > 8 * 1024 * 1024 or magic != b'%PDF-':
        raise ValueError('Invalid or oversized PDF')
    from pypdf import PdfReader
    pdf = PdfReader(str(data) if is_path else io.BytesIO(data))
    if pdf.is_encrypted or not 0 < len(pdf.pages) <= 100:
        raise ValueError('PDF cannot be verified')
    try:
        text = '\n'.join(page.extract_text() or '' for page in pdf.pages)
    finally:
        pdf.close()
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
        referenced = {str(Path(p).parent) for job in JOBS.values() for p in job.get('pdfs', {}).values()}
        import shutil
        for folder in Path('/tmp').glob('classtime-reports-*'):
            if str(folder) not in referenced and now - folder.stat().st_mtime > TTL:
                shutil.rmtree(folder, ignore_errors=True)
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


def import_review_archive(data):
    """Restore an owner-supplied review; reverify mappings and original PDF contents."""
    import json
    import zipfile
    import math
    size = data.stat().st_size if isinstance(data, Path) else len(data)
    if size > 20 * 1024 * 1024: raise ValueError('Archive exceeds the size limit')
    try:
        with zipfile.ZipFile(data if isinstance(data, Path) else io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 102 or sum(e.file_size for e in entries) > 40 * 1024 * 1024:
                raise ValueError('Archive exceeds the verification limits')
            metadata = [e for e in entries if e.filename == 'review.json']
            if len(metadata) != 1 or metadata[0].file_size > 200000:
                raise ValueError('Use a saved review ZIP containing review.json')
            source = json.loads(archive.read(metadata[0]))
            code, section = source.get('session_code'), source.get('class_section')
            if not isinstance(code, str) or not re.fullmatch(r'[A-Z0-9]{6}', code) or section not in ('B3 ET', 'B3 WT'):
                raise ValueError('Invalid session or class')
            rows = source.get('students')
            if not isinstance(rows, list) or not 0 < len(rows) <= 100: raise ValueError('Invalid roster')
            students, seen = [], set()
            for row in rows:
                name = row.get('classtime_name')
                if not isinstance(name, str) or not 0 < len(name) <= 120: raise ValueError('Invalid student name')
                mapping = match_student(name, section)
                if not mapping or mapping['child_id'] in seen: raise ValueError('Unknown or duplicate class mapping')
                seen.add(mapping['child_id'])
                maximum, points = row.get('maximum_points'), row.get('points')
                if type(maximum) not in (int, float) or not math.isfinite(maximum) or maximum <= 0: raise ValueError('Invalid maximum points')
                if points is not None and (type(points) not in (int, float) or not math.isfinite(points) or not 0 <= points <= maximum): raise ValueError('Invalid points')
                origin = row.get('source_session', code)
                if not isinstance(origin, str) or not re.fullmatch(r'[A-Z0-9]{6}', origin): raise ValueError('Invalid source session')
                students.append(dict(classtime_name=name, nigri=mapping, points=points, maximum_points=maximum,
                    percentage=None if points is None else round(points / maximum * 100, 2), source_session=origin, pdf_status='pending'))
            result = dict(session_code=code, session_name=str(source.get('session_name', code))[:200], class_section=section,
                students=students, retrieved_at=source.get('retrieved_at'), archive_report_count=0,
                source_sessions=sorted({s['source_session'] for s in students}), restored_from_archive=True,
                read_only=True, sync_enabled=False, nigri_writes=False)
            return attach_archive_reports(result, data)
    except (zipfile.BadZipFile, json.JSONDecodeError, KeyError, TypeError, AttributeError):
        raise ValueError('Invalid saved review archive') from None


def store_review(owner, result, pdfs):
    with LOCK:
        if any(j['status'] == 'loading' for j in JOBS.values()):
            raise ValueError('Wait for the active retrieval to finish')
        old_folders = {str(Path(p).parent) for job in JOBS.values() for p in job.get('pdfs', {}).values()}
        JOBS.clear()
        import shutil
        for folder in old_folders: shutil.rmtree(folder, ignore_errors=True)
        key = uuid.uuid4().hex
        JOBS[key] = dict(owner=owner, expires=time.time() + TTL, status='ready', result=result, pdfs=pdfs)
        return key


def attach_archive_reports(review, archive_path):
    """Keep PDFs on private temporary disk; parse only one bounded report at a time."""
    import gc
    import tempfile
    import zipfile
    import shutil
    from browser_runtime import log_memory
    log_memory('PDF verification starting (no browser running)')
    folder = Path(tempfile.mkdtemp(prefix='classtime-reports-'))
    folder.chmod(0o700)
    pdfs = {}
    try:
        with zipfile.ZipFile(archive_path if isinstance(archive_path, Path) else io.BytesIO(archive_path)) as archive:
            entries = [e for e in archive.infolist() if not e.is_dir() and e.filename.lower().endswith('.pdf')]
            if len(entries) > 100 or sum(e.file_size for e in entries) > 20 * 1024 * 1024:
                raise ValueError('Archive exceeds verification limits')
            for entry in entries:
                if entry.file_size > 8 * 1024 * 1024: raise ValueError('PDF is too large')
                key = uuid.uuid4().hex
                path = folder / (key + '.pdf')
                # Generated filename avoids archive path traversal entirely.
                with archive.open(entry) as source, path.open('wb') as output:
                    shutil.copyfileobj(source, output, length=65536)
                path.chmod(0o600)
                student, _ = verify_pdf(path, review['students'])
                origin = student.get('source_session', review['session_code'])
                verify_pdf(path, [student], origin, review['class_section'])
                if student['pdf_status'] == 'verified': raise ValueError('Duplicate student report')
                digest = hashlib.sha256()
                with path.open('rb') as source:
                    for chunk in iter(lambda: source.read(65536), b''): digest.update(chunk)
                student.update(pdf_status='verified', pdf_id=key, source_session=origin,
                    pdf_filename=entry.filename.rsplit('/', 1)[-1], pdf_sha256=digest.hexdigest(), pdf_identity=student['classtime_name'])
                pdfs[key] = str(path)
                gc.collect()  # release this PDF's parsed pages before reading the next one
                log_memory('PDF ' + str(len(pdfs)) + '/' + str(len(entries)) + ' verified')
        for student in review['students']:
            if student['pdf_status'] != 'verified': student.update(pdf_status='missing', pdf_detail='No verified report in this archive')
        review['archive_report_count'] = len(pdfs)
        return review, pdfs
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise
