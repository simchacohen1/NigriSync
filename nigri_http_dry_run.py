"""An entirely OFFLINE Classtime -> Nigri HTTP save simulation.

This module is NOT an HTTP Nigri client. It makes no network requests,
uses no account credentials, opens no browser, stores no data, and has
no method for committing a mark. Every destination is *.invalid.

It validates a fictional mark header and nine fictional score/PDF rows,
then uses requests.Request.prepare() to serialize the two POST bodies
without ever sending them. The live Nigri POST actions and dynamic
class/topic controls remain UNVERIFIED.
"""
from io import BytesIO
from decimal import Decimal
from urllib.parse import urlparse

from pypdf import PdfReader, PdfWriter
from requests import Request

from classtime_sync import percent

MOCK_ORIGIN = 'https://nigri-mock.invalid'
MOCK_MARK_ID = 'SIMULATED-NOT-A-NIGRI-ID'
MAX_REPORT_BYTES = 8 * 1024 * 1024

# Values are fictional and only used in .invalid request preparations.
_FAKE_CLASS_ID = '990001'
_FAKE_TOPIC_ID = '990002'


def synthetic_pdf():
    """A real one-page PDF containing no names or personal information."""
    buffer = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=250, height=250)
    writer.write(buffer)
    return buffer.getvalue()


def fictional_students(count=9):
    """Only made-up records, never the user's live student roster."""
    if type(count) is not int or not 1 <= count <= 20:
        raise ValueError('Fictional count must be 1 to 20')
    pdf = synthetic_pdf()
    return [
        {
            'child_id': str(900001 + i),
            'name': f'Fictional Learner {i + 1:02d}',
            'points': 24 - (i % 7),
            'maximum_points': 24,
            'pdf_bytes': pdf,
        }
        for i in range(count)
    ]


def _check_pdf(blob):
    if not isinstance(blob, bytes) or len(blob) > MAX_REPORT_BYTES or not blob.startswith(b'%PDF-'):
        raise ValueError('A fictional report is not a valid-sized PDF')
    try:
        reader = PdfReader(BytesIO(blob), strict=True)
        if not len(reader.pages):
            raise ValueError('A fictional PDF has no pages')
    except Exception as exc:
        raise ValueError('A fictional report PDF could not be read') from exc


def _validate_students(students):
    if not isinstance(students, list) or not 1 <= len(students) <= 20:
        raise ValueError('Simulation needs 1 to 20 fictional students')
    seen_ids, seen_names = set(), set()
    rows = []
    for item in students:
        if not isinstance(item, dict):
            raise ValueError('Invalid simulated row')
        cid, name = item.get('child_id'), item.get('name')
        if not isinstance(cid, str) or not cid.isascii() or not cid.isdigit():
            raise ValueError('Simulated child ID must be numeric')
        if cid in seen_ids or not isinstance(name, str) or not name.startswith('Fictional Learner ') or name in seen_names:
            raise ValueError('Duplicate or non-fictional simulated student')
        seen_ids.add(cid)
        seen_names.add(name)
        p, maximum = item.get('points'), item.get('maximum_points')
        if isinstance(p, bool) or isinstance(maximum, bool):
            raise ValueError('Invalid points')
        try:
            dp, dm = Decimal(str(p)), Decimal(str(maximum))
        except Exception as exc:
            raise ValueError('Invalid points') from exc
        if not dp.is_finite() or not dm.is_finite() or dm <= 0 or dp < 0 or dp > dm:
            raise ValueError('Invalid score range')
        pdf = item.get('pdf_bytes')
        _check_pdf(pdf)
        rows.append((cid, name, percent(dp, dm), pdf))
    return rows


def _prepare_only(method, path, *, data, files=None):
    if method != 'POST' or path not in ('/mock/new-mark', '/mock/student-marks'):
        raise ValueError('Only fictional POST bodies may be prepared')
    request = Request(method, MOCK_ORIGIN + path, data=data, files=files)
    # prepare() does NOT connect to any server or send an HTTP request.
    prepared = request.prepare()
    destination = urlparse(prepared.url)
    if destination.hostname != 'nigri-mock.invalid':
        raise ValueError('Simulation destination is not fictional')
    return prepared


def run_offline_simulation(students=None):
    """Serialize mocked header and multipart score/PDF forms entirely in RAM.

    No Nigri or network access. Never use real student data for this tool.
    This demonstrates request shapes, not that Nigri will accept them.
    """
    if students is None:
        students = fictional_students()
    rows = _validate_students(students)

    # Confirmed blank form field names. POST endpoint and precise accepted
    # values are only MOCKED, not established for real Nigri submissions.
    header = {
        'testGradeID': _FAKE_CLASS_ID,
        'testTopicID': _FAKE_TOPIC_ID,
        'testType': 'quiz',
        'testName': 'FICTIONAL SIMULATION ONLY',
        'testDate': '2026-10-11',
        'testDesc': 'No external submission',
    }
    header_prepared = _prepare_only('POST', '/mock/new-mark', data=header)
    if not header_prepared.headers['Content-Type'].startswith('application/x-www-form-urlencoded'):
        raise AssertionError('Mock header was not form encoded')

    # The next screen's field names were established in the previous
    # browser-save code, but the browser-free save action is NOT verified.
    # Simulate a mark ID locally, without creating any actual mark.
    save_fields = {'testCompleted': '1', 'submit': 'Save!'}
    file_fields = {}
    scores = []
    for cid, name, score, pdf in rows:
        prefix = f'testChild_{cid}_'
        save_fields[prefix + 'mark'] = str(score)
        save_fields[prefix + 'markSpecialStatus'] = ''
        save_fields[prefix + 'markComment'] = ''
        file_fields[prefix + 'File1'] = (name + ' - MOCK.pdf', pdf, 'application/pdf')
        scores.append(score)

    final_prepared = _prepare_only(
        'POST', '/mock/student-marks', data=save_fields, files=file_fields
    )
    content_type = final_prepared.headers.get('Content-Type', '')
    if not content_type.startswith('multipart/form-data; boundary='):
        raise AssertionError('PDF scores must be multipart form data')
    body = final_prepared.body
    if not isinstance(body, bytes) or body.count(b'%PDF-') != len(rows):
        raise AssertionError('Mock PDF attachments were not serialized correctly')
    for key in file_fields:
        if key.encode('ascii') not in body:
            raise AssertionError('A fictional PDF field was omitted')
    for cid, _, score, _ in rows:
        if f'testChild_{cid}_mark'.encode('ascii') not in body:
            raise AssertionError('A fictional student mark field was omitted')

    return {
        'status': 'simulated_ok',
        'offline_only': True,
        'fictional_data_only': True,
        'nigri_contacted': False,
        'nigri_writes': False,
        'real_save_enabled': False,
        'compatible_with_nigri_proven': False,
        'mock_mark_reference': MOCK_MARK_ID,
        'simulated_students': len(rows),
        'simulated_scores': len(scores),
        'simulated_pdf_attachments': len(file_fields),
        'simulated_scores_percent': scores,
        'mock_header_content_type': 'urlencoded',
        'mock_student_content_type': 'multipart/form-data',
        'mock_request_bodies': 2,
        'requests_sent': 0,
        'automated_retries': 0,
        'simulation_steps': [
            'Validate fictional class, mark header, scores and PDFs',
            'Prepare a mock mark-header POST body (never send)',
            'Assign a fictional local-only mark reference',
            'Prepare a mock multipart score-and-PDF save body (never send)',
            'Verify that all fictional scores and PDFs were serialized',
        ],
        'remaining_real_world_unknowns': [
            'Nigri mark creation POST endpoint, dynamic topic selection, and required hidden fields',
            'Nigri multipart Save! POST endpoint and additional required fields',
            'Nigri response, created mark ID, and saved score/PDF verification',
        ],
    }
