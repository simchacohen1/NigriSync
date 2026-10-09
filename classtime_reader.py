"""Bounded read-only diagnostic through Classtime's normal teacher UI.
No Nigri imports, private API calls, session edits, or security bypasses.
"""
import os
import re
from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout


def test_session(session_code, inspect_exports=False, review_section=None):
    from browser_runtime import run_browser
    return run_browser(_test_session, session_code, inspect_exports=inspect_exports, review_section=review_section)


def _test_session(session_code, inspect_exports=False, review_section=None):
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
    # Limit the Playwright Node driver independently of the Chromium V8 heap.
    os.environ['NODE_OPTIONS'] = '--max-old-space-size=64'
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--single-process', '--no-zygote', '--js-flags=--max-old-space-size=96'])
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
            if review_section: print('Classtime review: logging in', flush=True)
            page.get_by_role('textbox', name='Email', exact=True).fill(email)
            page.get_by_label('Password', exact=True).fill(password)
            page.get_by_role('button', name='Log in', exact=True).click()
            try:
                page.wait_for_url(lambda url: '/auth/' not in url, timeout=30000)
            except BrowserTimeout:
                return {'status': 'blocked' if blocked(page) else 'login_failed', 'stage': stage, 'detail': 'Login did not reach an authenticated page. No retry or bypass attempted.', 'nigri_writes': False}
            stage = 'reading_session'
            if review_section: print('Classtime review: reading session', flush=True)
            page.goto('https://www.classtime.com/sessions/' + session_code, wait_until='domcontentloaded', timeout=45000)
            # Wait for the SPA to render a session heading or its access error.
            page.wait_for_function("""(code) => { const t = document.body.innerText; return t.includes(code) || t.includes('Shorashim') || /session not found|do not have access|permission denied/i.test(t); }""", arg=session_code, timeout=90000)
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
            if review_section: raise ValueError('Classtime timed out at ' + stage + '. No Nigri grades were changed. Observed page: ' + clean(page.locator('body').inner_text())[:1200])
            return {'status': 'timeout', 'stage': stage, 'detail': 'Classtime page did not finish the required step', 'nigri_writes': False}
        except Exception as exc:
            if review_section:
                if isinstance(exc, ValueError): raise
                raise ValueError('The report export could not be completed. No Nigri grades were changed.') from None
            # Never return raw exceptions, URLs, cookies, traces or credential-bearing HTML.
            return {'status': 'error', 'stage': stage, 'detail': 'Read-only browser test failed; no credentials included in diagnostics', 'nigri_writes': False}
        finally:
            try:
                context.close()
            finally:
                browser.close()


def load_review(session_code, class_section):
    from classtime_review import verify_pdf
    import uuid
    import hashlib
    result = test_session(session_code, review_section=class_section)
    if not isinstance(result, tuple):
        raise ValueError('Classtime access failed at ' + str(result.get('stage', result.get('status', 'login'))) + '. No bypass attempted.')
    review, archive_path = result
    from classtime_review import attach_archive_reports
    from pathlib import Path
    try:
        from browser_runtime import browser_slot
        with browser_slot():
            return attach_archive_reports(review, Path(archive_path))
    finally:
        import shutil
        shutil.rmtree(str(Path(archive_path).parent), ignore_errors=True)


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
    from classtime_review import parse_session
    text = page.locator('body').inner_text()
    rows = page.locator('tr, [role="row"]').all_inner_texts()
    result = parse_session(rows, text, code, section)
    print("Classtime review: opening export", flush=True)
    page.locator('[aria-label="Export"] button').click()
    page.wait_for_timeout(1000)
    student_report = page.get_by_text(re.compile(r'^Student Reports?(?:\s*\(PDF\))?$', re.I))
    if student_report.count() == 1:
        student_report.click()
        page.wait_for_timeout(1000)
    archive = page.get_by_text(re.compile(r'^Export all(?:\s*\(as \.zip\))?$', re.I))
    if archive.count() != 1:
        raise ValueError('Student ZIP export control was not identified. Visible export choices: ' + _export_controls(page))
    dialogs = page.locator('[role="dialog"]')
    if dialogs.count():
        picker_text = dialogs.last.inner_text()
        result['pdf_export_offered_names'] = [student['classtime_name'] for student in result['students'] if student['classtime_name'] in picker_text]
        result['pdf_export_observation'] = picker_text[:5000]
    print("Classtime review: downloading ZIP", flush=True)
    with page.expect_download(timeout=150000) as download_info:
        archive.click()
    download = download_info.value
    folder = tempfile.mkdtemp(prefix='classtime-download-')
    target = Path(folder) / 'reports.zip'
    try:
        download.save_as(str(target))
        if target.stat().st_size > 20 * 1024 * 1024: raise ValueError('Report archive exceeds the size limit')
        result['retrieved_at'] = datetime.now(timezone.utc).isoformat()
        return result, str(target)
    except Exception:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
        raise
