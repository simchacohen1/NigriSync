"""Build the review from Classtime's 'export all' ZIP alone (no browser, no page scraping).

Each student's PDF carries the name, the score line and the session. The result has the same shape
as the browser-based review, so the existing PDF verification and matching run on it unchanged.
"""
import gc
import io
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

NUMBER = r'\d+(?:\.\d+)?'


def _first_page_text(data):
    from pypdf import PdfReader
    if data[:5] != b'%PDF-': raise ValueError('Invalid report in the Classtime ZIP')
    pdf = PdfReader(io.BytesIO(data))
    try:
        if pdf.is_encrypted or not 0 < len(pdf.pages) <= 100: raise ValueError('Invalid report in the Classtime ZIP')
        return pdf.pages[0].extract_text() or ''
    finally:
        pdf.close()


def parse_report_header(text, code):
    """Return (student name, points or None, maximum points, session title) from a report's first page."""
    name = re.search(r'^Name:[ \t]*(.+)$', text, re.M)
    # The header line spells the session code with spaces between characters ("A B C 1 2 3"), then the title and score.
    header = re.search(r'^' + r'\s*'.join(re.escape(c) for c in code) + r'[ \t]+(.+?)[ \t]+(' + NUMBER + r'|-|–|—)[ \t]*/[ \t]*(' + NUMBER + r')[ \t]*points?\b', text, re.M)
    footer = re.search(r'Session:\s*' + re.escape(code) + r'\b', text)
    if not name or not header or not footer: raise ValueError('A report in the Classtime ZIP is not in the expected format')
    score = header.group(2)
    return name.group(1).strip(), None if score in ('-', '–', '—') else float(score), float(header.group(3)), header.group(1).strip()


def review_from_zip(zip_path, code, section):
    from classtime_review import match_student, class_in_text, class_label, class_field_text
    students, titles, seen, fields = [], set(), set(), []
    with zipfile.ZipFile(zip_path) as archive:
        entries = [e for e in archive.infolist() if not e.is_dir() and e.filename.lower().endswith('.pdf')]
        if not 0 < len(entries) <= 100 or sum(e.file_size for e in entries) > 20 * 1024 * 1024:
            raise ValueError('The Classtime ZIP has no reports or exceeds the limits')
        for entry in entries:
            if entry.file_size > 8 * 1024 * 1024: raise ValueError('PDF is too large')
            page = _first_page_text(archive.read(entry))
            name, points, maximum, title = parse_report_header(page, code)
            fields.append(class_field_text(page))
            gc.collect()
            titles.add(title)
            if maximum <= 0 or (points is not None and not 0 <= points <= maximum): raise ValueError('A report score is out of range')
            mapping = match_student(name, section)
            if mapping and mapping['child_id'] in seen: raise ValueError('Duplicate student mapping')
            if mapping: seen.add(mapping['child_id'])
            students.append({'classtime_name': name, 'nigri': mapping, 'points': points, 'maximum_points': maximum,
                             'percentage': None if points is None else round(points / maximum * 100, 2), 'pdf_status': 'pending'})
    if len(titles) != 1: raise ValueError('The reports in the ZIP are from different sessions')
    title = titles.pop()
    # Same class safety as the browser review: the session title itself must name the selected class.
    from classtime_review import title_class_labels
    print('Classtime class check: picked=' + section + ' reports=' + str(len(students)) + ' title_class_labels=' + str(title_class_labels(title))
          + ' pdf_class_fields=' + str(sorted({class_label(f) for f in fields})) + ' all_same_class_field=' + str(len({f.strip().upper() for f in fields}) == 1), flush=True)
    if not class_in_text(title, section): raise ValueError('Quiz title does not identify the selected class')
    if len({s['maximum_points'] for s in students}) != 1: raise ValueError('Reports disagree on the maximum points')
    return {'session_code': code, 'session_name': title, 'class_section': section, 'students': students,
            'read_only': True, 'sync_enabled': False, 'nigri_writes': False}


def load_review_http(code, section):
    """Sign in, export, read and verify. Returns (review, pdfs) like the browser version. Run it inside run_browser."""
    from classtime_http import fetch_report_zip
    from classtime_review import attach_archive_reports
    path = fetch_report_zip(code)
    try:
        review = review_from_zip(path, code, section)
        review['retrieved_at'] = datetime.now(timezone.utc).isoformat()
        return attach_archive_reports(review, path)
    finally:
        shutil.rmtree(str(Path(path).parent), ignore_errors=True)
