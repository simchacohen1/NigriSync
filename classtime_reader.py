"""Bounded read-only diagnostic through Classtime's normal teacher UI.
No Nigri imports, private API calls, session edits, or security bypasses.
"""
import os
import re
from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout


def test_session(session_code, inspect_exports=False):
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
            if inspect_exports:
                page.goto('https://www.classtime.com/sessions', wait_until='domcontentloaded', timeout=45000)
                page.get_by_text(session_code, exact=False).first.wait_for(timeout=45000)
                page.wait_for_timeout(2000)
                return {'status': 'export_inspection', 'visible_text': clean(page.locator('body').inner_text())[:24000], 'controls': page.locator('button, a, [role=button]').evaluate_all('(els) => els.map(e => ({text:e.innerText, label:e.getAttribute("aria-label"), title:e.getAttribute("title")}))'), 'nigri_writes': False}
            text = clean(page.locator('body').inner_text())
            rows = page.locator('tr, [role="row"]').all_inner_texts()
            return {'status': 'login_succeeded' if '/auth/' not in page.url else 'session_access_denied', 'session_code': session_code, 'stage': stage, 'page_title': clean(page.title()), 'visible_text': text[:24000], 'visible_rows': [clean(row) for row in rows][:200], 'nigri_writes': False, 'note': 'Diagnostic observations only; names and grades require verification before integration.'}
        except BrowserTimeout:
            return {'status': 'timeout', 'stage': stage, 'detail': 'Classtime page did not finish the required step', 'nigri_writes': False}
        except Exception:
            # Never return raw exceptions, URLs, cookies, traces or credential-bearing HTML.
            return {'status': 'error', 'stage': stage, 'detail': 'Read-only browser test failed; no credentials included in diagnostics', 'nigri_writes': False}
        finally:
            context.close()
            browser.close()
