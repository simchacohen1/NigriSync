"""Bounded read-only diagnostic through Classtime's normal teacher UI.
No Nigri imports, private API calls, session edits, or security bypasses.
"""
import os
import re
from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout


def _chromium_args():
    """Memory-saving Chromium flags for the 512 MiB Render Starter service."""
    args = [
        '--disable-dev-shm-usage',        # use /tmp, not the tiny /dev/shm, for shared memory
        '--disable-gpu', '--disable-software-rasterizer',
        '--disable-extensions', '--disable-background-networking', '--disable-component-update',
        '--disable-sync', '--disable-default-apps', '--mute-audio', '--no-first-run',
        '--disable-breakpad', '--disable-domain-reliability', '--disable-client-side-phishing-detection',
        '--disable-features=Translate,OptimizationHints,MediaRouter',
        '--js-flags=--max-old-space-size=96',
    ]
    # Single-process Chromium saves a lot of RAM but is not officially supported.
    # It stays ON (as before); set CLASSTIME_CHROMIUM_SINGLE_PROCESS=0 in Render to
    # switch to normal multi-process mode if the browser ever crashes.
    if os.environ.get('CLASSTIME_CHROMIUM_SINGLE_PROCESS', '1') != '0':
        args = ['--single-process', '--no-zygote'] + args
    return args


# Never needed to log in, read grades or export reports. Types the page never has to load:
_BLOCKED_TYPES = ('image', 'media', 'font', 'texttrack', 'manifest', 'ping', 'cspviolationreport')
# Analytics, ads, session-replay and chat widgets only. Classtime's own domains and
# Google reCAPTCHA are deliberately NOT listed. Turn off with CLASSTIME_BLOCK_TRACKERS=0.
_TRACKER_DOMAINS = (
    'google-analytics.com', 'googletagmanager.com', 'doubleclick.net', 'googlesyndication.com', 'googleadservices.com',
    'facebook.net', 'hotjar.com', 'hotjar.io', 'intercom.io', 'intercomcdn.com', 'segment.io', 'segment.com',
    'sentry.io', 'fullstory.com', 'clarity.ms', 'hubspot.com', 'hs-scripts.com', 'hs-analytics.net', 'mixpanel.com',
    'amplitude.com', 'heapanalytics.com', 'crisp.chat', 'drift.com', 'zendesk.com', 'zdassets.com', 'linkedin.com',
    'licdn.com', 'ads-twitter.com', 'tiktok.com', 'posthog.com', 'newrelic.com', 'nr-data.net', 'smartlook.com',
    'logrocket.com', 'lr-ingest.io', 'youtube.com', 'vimeo.com', 'wistia.com',
)


def _host_matches(host, domains):
    return any(host == d or host.endswith('.' + d) for d in domains)


def _make_router(stats):
    """Request filter that also records host NAMES only (never URLs, paths or tokens)."""
    block_trackers = os.environ.get('CLASSTIME_BLOCK_TRACKERS', '1') != '0'
    def handle(route):
        request = route.request
        if request.resource_type in _BLOCKED_TYPES:
            stats['blocked_types'] += 1
            return route.abort()
        try:
            from urllib.parse import urlparse
            host = (urlparse(request.url).hostname or '').lower()
        except ValueError:
            host = ''
        if block_trackers and host and _host_matches(host, _TRACKER_DOMAINS):
            stats['blocked_trackers'] += 1
            if len(stats['blocked_hosts']) < 40: stats['blocked_hosts'].add(host)
            return route.abort()
        if host and not _host_matches(host, ('classtime.com',)) and len(stats['hosts']) < 40:
            stats['hosts'].add(host)
        return route.continue_()
    return handle


def _log_network(stats):
    print('Classtime network: blocked ' + str(stats['blocked_types']) + ' image/font/media requests and '
          + str(stats['blocked_trackers']) + ' tracker requests (' + ', '.join(sorted(stats['blocked_hosts'])) + '); '
          'other outside hosts contacted: ' + ', '.join(sorted(stats['hosts'])), flush=True)


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
    from browser_runtime import log_memory
    # Limit the Playwright Node driver independently of the Chromium V8 heap.
    os.environ['NODE_OPTIONS'] = '--max-old-space-size=64'
    with sync_playwright() as p:
        log_memory('playwright driver started')
        browser = p.chromium.launch(headless=True, args=_chromium_args())
        log_memory('chromium launched')
        context = browser.new_context()
        stats = dict(blocked_types=0, blocked_trackers=0, blocked_hosts=set(), hosts=set())
        context.route('**/*', _make_router(stats))
        page = context.new_page()
        page.set_default_timeout(15000)
        log_memory('page ready')
        try:
            page.goto('https://www.classtime.com/auth/login', wait_until='domcontentloaded', timeout=45000)
            page.get_by_role('textbox', name='Email', exact=True).wait_for()
            if blocked(page):
                return {'status': 'blocked', 'stage': stage, 'detail': 'Human verification or access restriction; no bypass attempted', 'nigri_writes': False}
            log_memory('login page loaded')
            stage = 'logging_in'
            if review_section: print('Classtime review: logging in', flush=True)
            page.get_by_role('textbox', name='Email', exact=True).fill(email)
            page.get_by_label('Password', exact=True).fill(password)
            page.get_by_role('button', name='Log in', exact=True).click()
            try:
                page.wait_for_url(lambda url: '/auth/' not in url, timeout=30000)
            except BrowserTimeout:
                return {'status': 'blocked' if blocked(page) else 'login_failed', 'stage': stage, 'detail': 'Login did not reach an authenticated page. No retry or bypass attempted.', 'nigri_writes': False}
            log_memory('logged in')
            stage = 'reading_session'
            if review_section: print('Classtime review: reading session', flush=True)
            page.goto('https://www.classtime.com/sessions/' + session_code, wait_until='domcontentloaded', timeout=45000)
            # Wait for the SPA to render a session heading or its access error.
            page.wait_for_function("""(code) => { const t = document.body.innerText; return t.includes(code) || t.includes('Shorashim') || /session not found|do not have access|permission denied/i.test(t); }""", arg=session_code, timeout=90000)
            page.wait_for_timeout(3000)
            log_memory('session page rendered')
            _log_network(stats)
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
                log_memory('browser closed')


def load_review(session_code, class_section):
    result = test_session(session_code, review_section=class_section)
    if not isinstance(result, tuple):
        raise ValueError('Classtime access failed at ' + str(result.get('stage', result.get('status', 'login'))) + '. No bypass attempted.')
    review, archive_path = result
    from classtime_review import attach_archive_reports
    from pathlib import Path
    try:
        from browser_runtime import run_browser
        return run_browser(attach_archive_reports, review, Path(archive_path))
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
    from browser_runtime import log_memory
    text = page.locator('body').inner_text()
    rows = page.locator('tr, [role="row"]').all_inner_texts()
    result = parse_session(rows, text, code, section)
    log_memory('grades parsed')
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
    log_memory('export menu open')
    print("Classtime review: downloading ZIP", flush=True)
    with page.expect_download(timeout=150000) as download_info:
        archive.click()
    download = download_info.value
    folder = tempfile.mkdtemp(prefix='classtime-download-')
    target = Path(folder) / 'reports.zip'
    try:
        download.save_as(str(target))
        log_memory('report ZIP saved to disk')
        if target.stat().st_size > 20 * 1024 * 1024: raise ValueError('Report archive exceeds the size limit')
        result['retrieved_at'] = datetime.now(timezone.utc).isoformat()
        return result, str(target)
    except Exception:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
        raise
