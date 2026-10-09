"""Bounded read-only diagnostic through Classtime's normal teacher UI.
No Nigri imports, private API calls, session edits, or security bypasses.
"""
import os
import re
from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout


def test_session(session_code, inspect_exports=False, review_section=None):
    if not re.fullmatch(r'[A-Z0-9]{6}', session_code):
        raise ValueError('A six-character session code is required')
    email = os.environ.get('CLASSTIME_EMAIL')
    password = os.environ.get('CLASSTIME_PASSWORD')
    if not email or not password:
        return {'status': 'not_configured', 'detail': 'Classtime credentials are missing', 'nigri_writes': False}
    stage = 'opening_login'
    def clean(value):
        return value.replace(email, '[redacted]').replace(password, '[redacted]')
    def blocked(page):
        text = page.locator('body').inner_text().lower()
        return any(s in text for s in ('verify you are human', 'checking your browser', 'unusual traffic', 'automated traffic', 'access denied')) or page.locator('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare"]').count() > 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        context.route('**/*', lambda route: route.abort() if route.request.resource_type in ('image', 'media', 'font') else route.continue_())
        page = context.new_page()
        page.set_default_timeout(15000)
        try:
            page.goto('https://www.classtime.com/auth/login', wait_until='domcontentloaded', timeout=45000)
            page.get_by_role('textbox', name='Email', exact=True).wait_for()
            if blocked(page):
                return {'status': 'blocked', 'stage': stage, 'detail': 'Human verification or access restriction; no bypass attempted', 'nigri_writes': False}
            stage = 'logging_in'
            page.get_by_role('textbox', name='Email', exact=True).fill(email)
            page.get_by_label('Password', exact=True).fill(password)
            page.get_by_role('button', name='Log in', exact=True).click()
            try:
                page.wait_for_url(lambda url: '/auth/' not in url, timeout=30000)
            except BrowserTimeout:
                return {'status': 'blocked' if blocked(page) else 'login_failed', 'stage': stage, 'detail': 'Login did not reach an authenticated page. No retry or bypass attempted.', 'nigri_writes': False}
            stage = 'reading_session'
            page.goto('https://www.classtime.com/sessions/' + session_code, wait_until='domcontentloaded', timeout=45000)
            # Wait for the SPA to render a session heading or its access error.
            page.wait_for_function("""(code) => { const t = document.body.innerText; return t.includes(code) || t.includes('Shorashim') || /session not found|do not have access|permission denied/i.test(t); }""", arg=session_code, timeout=45000)
            page.wait_for_timeout(3000)
            if blocked(page):
                return {'status': 'blocked', 'stage': stage, 'nigri_writes': False}
            if review_section:
                return _collect_review(page, session_code, review_section)
            if inspect_exports:
                return {'status': 'export_inspection', 'visible_text': clean(page.locator('body').inner_text())[:24000], 'controls': page.locator('button, a, [role=button], [title], [aria-label]').evaluate_all('(els) => els.map(e => ({tag:e.tagName,text:e.innerText, label:e.getAttribute("aria-label"), title:e.getAttribute("title"), href:e.getAttribute("href")}))'), 'nigri_writes': False}
            text = clean(page.locator('body').inner_text())
            rows = page.locator('tr, [role="row"]').all_inner_texts()
            return {'status': 'login_succeeded' if '/auth/' not in page.url else 'session_access_denied', 'session_code': session_code, 'stage': stage, 'page_title': clean(page.title()), 'visible_text': text[:24000], 'visible_rows': [clean(row) for row in rows][:200], 'nigri_writes': False, 'note': 'Diagnostic observations only; names and grades require verification before integration.'}
        except BrowserTimeout:
            if review_section: raise ValueError('Classtime did not finish loading the session or report export. No Nigri grades were changed.')
            return {'status': 'timeout', 'stage': stage, 'detail': 'Classtime page did not finish the required step', 'nigri_writes': False}
        except Exception as exc:
            if review_section:
                if isinstance(exc, ValueError): raise
                raise ValueError('The report export could not be completed. No Nigri grades were changed.') from None
            # Never return raw exceptions, URLs, cookies, traces or credential-bearing HTML.
            return {'status': 'error', 'stage': stage, 'detail': 'Read-only browser test failed; no credentials included in diagnostics', 'nigri_writes': False}
        finally:
            context.close()
            browser.close()


def load_review(session_code, class_section):
    result = test_session(session_code, review_section=class_section)
    if not isinstance(result, tuple):
        raise ValueError('Classtime access failed at ' + str(result.get('stage', result.get('status', 'login'))) + '. No bypass attempted.')
    return result


def _export_controls(page):
    # Only rendered export choices; never HTML, cookies, network payloads, or credentials.
    dialog = page.locator('[role="dialog"]')
    text = dialog.last.inner_text() if dialog.count() else page.locator('body').inner_text()
    return text[-4000:]


def _collect_review(page, code, section):
    import io
    import zipfile
    import tempfile
    import uuid
    import hashlib
    from datetime import datetime, timezone
    from pathlib import Path
    from classtime_review import parse_session, verify_pdf
    text = page.locator('body').inner_text()
    rows = page.locator('tr, [role="row"]').all_inner_texts()
    result = parse_session(rows, text, code, section)
    page.locator('[aria-label="Export"] button').click()
    page.wait_for_timeout(1000)
    student_report = page.get_by_text(re.compile(r'^Student Reports?(?:\s*\(PDF\))?$', re.I))
    if student_report.count() == 1:
        student_report.click()
        page.wait_for_timeout(1000)
    archive = page.get_by_text(re.compile(r'^Export all(?:\s*\(as \.zip\))?$', re.I))
    if archive.count() != 1:
        raise ValueError('Student ZIP export control was not identified. Visible export choices: ' + _export_controls(page))
    with page.expect_download(timeout=150000) as download_info:
        archive.click()
    download = download_info.value
    with tempfile.TemporaryDirectory() as folder:
        target = Path(folder) / 'reports.zip'
        download.save_as(str(target))
        if target.stat().st_size > 40 * 1024 * 1024: raise ValueError('Report archive exceeds the size limit')
        pdfs = {}
        with zipfile.ZipFile(target) as archive_file:
            entries = [entry for entry in archive_file.infolist() if not entry.is_dir() and entry.filename.lower().endswith('.pdf')]
            if len(entries) > 100 or sum(entry.file_size for entry in entries) > 40 * 1024 * 1024:
                raise ValueError('Report archive exceeds the verification limits')
            for entry in entries:
                if entry.file_size > 8 * 1024 * 1024: raise ValueError('Individual report is too large')
                data = archive_file.read(entry)
                try:
                    student, pdf_text = verify_pdf(data, result['students'])
                except ValueError:
                    raise ValueError('A PDF could not be uniquely matched by its contents. No PDF was assigned by filename.') from None
                if student['pdf_status'] == 'verified': raise ValueError('Two reports claim the same student identity')
                pdf_id = uuid.uuid4().hex
                student.update(pdf_status='verified', pdf_id=pdf_id, pdf_filename=Path(entry.filename).name, pdf_sha256=hashlib.sha256(data).hexdigest(), pdf_identity=student['classtime_name'])
                pdfs[pdf_id] = data
        for student in result['students']:
            if student['pdf_status'] != 'verified':
                student.update(pdf_status='missing', pdf_detail='Classtime did not include a report for this student')
        result['retrieved_at'] = datetime.now(timezone.utc).isoformat()
        result['archive_report_count'] = len(entries)
        return result, pdfs
