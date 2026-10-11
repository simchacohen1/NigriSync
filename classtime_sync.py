"""Dry run for 'Sync to Nigri' from a Classtime review. Builds exactly what a real save WOULD send, without sending it.

Nothing here writes to Nigri. The only Nigri contact is one read of the existing marks list (list_mark_titles),
to see whether a mark for this session is already there. A real save does not exist yet (can_save is always False).
Student PDFs never leave the server; the preview reports only whether each one is attached and its size.
"""
import base64
import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

TYPES = ('homework', 'test', 'quiz', 'classwork')
MAX_REPORT_BYTES = 8 * 1024 * 1024


def _normalize(text):
    return ' '.join(re.findall(r'[a-z0-9]+', str(text).lower()))


def validate_options(body, result, topics):
    """Return clean options or raise ValueError with a plain message."""
    if not isinstance(body, dict): raise ValueError('Invalid request')
    topic = body.get('topic')
    if topic not in topics: raise ValueError('Choose one of the Nigri topics')
    mark_type = body.get('type', 'quiz')
    if mark_type not in TYPES: raise ValueError('Choose homework, test, quiz or classwork')
    date = body.get('date')
    try: datetime.strptime(date, '%Y-%m-%d')
    except (TypeError, ValueError): raise ValueError('Enter the date as YYYY-MM-DD') from None
    default_name = (str(result.get('session_name', 'Classtime')) + ' (' + result['session_code'] + ')')[:120]
    name = body.get('test_name') or default_name
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120 or re.search(r'[\x00-\x1f]', name):
        raise ValueError('The mark name must be 1 to 120 characters')
    description = body.get('description')
    if description is not None and (not isinstance(description, str) or len(description) > 500 or re.search(r'[\x00-\x08\x0b-\x1f]', description)):
        raise ValueError('The description must be under 500 characters')
    return {'topic': topic, 'type': mark_type, 'date': date, 'test_name': name.strip(), 'description': description or None}


def percent(points, maximum):
    """Whole-number percent, rounded half up (same as the Weekly Quiz page)."""
    return int((Decimal(str(points)) / Decimal(str(maximum)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _safe_file_part(text):
    return re.sub(r'[^A-Za-z0-9 _-]', '', text or '').strip() or 'student'


def build_packet(result, pdfs, options, with_reports=False):
    """The /sync-marks message a real save would send. Reports are read from disk and encoded only if with_reports.
    Returns (packet, rows, blockers); rows is the plain-language per-boy list."""
    students, rows, blockers = [], [], []
    for s in result['students']:
        entry = {'classtime_name': s['classtime_name'], 'points': s['points'], 'maximum_points': s['maximum_points']}
        if not s.get('nigri'):
            rows.append({**entry, 'nigri_name': None, 'action': 'skip', 'reason': 'Not matched to a Nigri student; not sent', 'mark_percent': None, 'report': None})
            continue
        entry['nigri_name'] = s['nigri']['name']
        if s['points'] is None:
            # No result: never a 0. The mark stays blank and attendance is left unchanged.
            students.append({'name': s['nigri']['name'], 'mark': None, 'attendance': 'review'})
            rows.append({**entry, 'action': 'blank', 'reason': 'No score; the mark is left blank (never sent as 0)', 'mark_percent': None, 'report': None})
            continue
        report = None
        row = {'name': s['nigri']['name'], 'mark': percent(s['points'], s['maximum_points']), 'attendance': 'present'}
        path = pdfs.get(s.get('pdf_id')) if s.get('pdf_status') == 'verified' else None
        if path:
            size = Path(path).stat().st_size
            if size > MAX_REPORT_BYTES: raise ValueError('A report is too large to send')
            report = {'attached': True, 'kb': round(size / 1024)}
            row['report_filename'] = _safe_file_part(s['nigri']['name']) + ' - ' + _safe_file_part(options['test_name']) + '.pdf'
            if with_reports: row['report_base64'] = base64.b64encode(Path(path).read_bytes()).decode('ascii')
        else:
            blockers.append('A report PDF is not verified for a boy with a score; fix this before saving')
        students.append(row)
        rows.append({**entry, 'action': 'save', 'reason': 'Will save the score' + (' with his report' if report else ' (no verified report)'), 'mark_percent': row['mark'], 'report': report})
    if not any(r['action'] == 'save' for r in rows): blockers.append('No matched boy has a score')
    packet = {'class_section': result['class_section'], 'topic': options['topic'], 'test_name': options['test_name'], 'type': options['type'],
              'date': options['date'], 'description': options['description'], 'mark_all_done': True, 'students': students}
    return packet, rows, sorted(set(blockers))


def find_duplicates(existing_titles, options, code):
    """Existing Nigri marks that are the same session: the same name, or a name carrying this session's code."""
    wanted = _normalize(options['test_name'])
    marker = _normalize(code)
    hits = []
    for title in existing_titles:
        t = _normalize(title)
        if t == wanted or re.search(r'(?<![a-z0-9])' + re.escape(marker) + r'(?![a-z0-9])', t): hits.append(title)
    return hits


def preview(result, pdfs, options, mark_titles, check_error=None, *, duplicate_check_disabled=False):
    """The full dry-run answer. `mark_titles` is the read-only list from Nigri, or None if it could not be read."""
    packet, rows, blockers = build_packet(result, pdfs, options)
    if duplicate_check_disabled:
        check = {'status': 'skipped', 'detail': 'Nigri duplicate lookup is disabled. Check Nigri yourself before saving the same session again.'}
    elif mark_titles is None:
        check = {'status': 'could_not_check', 'detail': check_error or 'Could not read the existing Nigri marks'}
        blockers.append('Could not check whether this session is already saved; a real save would refuse')
    elif not mark_titles.get('titles'):
        check = {'status': 'could_not_check', 'detail': 'The Nigri marks list showed no titled marks, so duplicates cannot be ruled out'}
        blockers.append('Could not check whether this session is already saved; a real save would refuse')
    else:
        hits = find_duplicates(mark_titles['titles'], options, result['session_code'])
        check = {'status': 'duplicate' if hits else 'clear', 'existing_marks_seen': len(mark_titles['titles']), 'matches': hits[:5]}
        if hits: blockers.append('A mark for this session already exists in Nigri')
    counts = {'will_save': sum(r['action'] == 'save' for r in rows), 'left_blank_no_score': sum(r['action'] == 'blank' for r in rows),
              'skipped_unmatched': sum(r['action'] == 'skip' for r in rows)}
    return {'dry_run': True, 'nigri_writes': False, 'real_save_enabled': False, 'can_save': False,
            'session_code': result['session_code'], 'class_section': result['class_section'],
            'topic': options['topic'], 'type': options['type'], 'date': options['date'], 'test_name': options['test_name'],
            'counts': counts, 'students': rows, 'blockers': blockers, 'duplicate_check': check,
            'would_send': {'students': len(packet['students']), 'reports': sum(1 for r in rows if r['report']),
                           'report_kb': sum(r['report']['kb'] for r in rows if r['report'])}}
