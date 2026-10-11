"""Owner-only, network-BLOCKED class-change trace for Nigri.

The browser signs in normally (login POST is permitted). On an already
validated BLANK New Mark URL, it then selects B3 ET while *aborting every*
request triggered by that change. It never contacts the grade/topic AJAX
endpoint and cannot create or save marks. No URLs, HTML, student names,
payloads, cookies or credential values are returned or logged.

This is discovery only; the refreshed topic list remains UNVERIFIED.
"""
import os
from urllib.parse import urlsplit, parse_qsl
from urllib.parse import urljoin

from nigri_http_probe import BASE, HOST, _hybrid_login_router

# Exactly the owner-inspected blank-form URL shape (testID=0 and all
# three selector values empty). This is a GET, not a Create Mark POST.
BLANK_FORM_PATH = (
    '/main/default_os_prog.asp?section=teachers&spec=tests'
    '&testCoursTypeSelect=&testGradeSelect=&testID=0&testTopicSelect='
)
BLANK_FORM_URL = BASE + BLANK_FORM_PATH


def _is_blank_form_get(url):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname != HOST:
        return False
    if parts.username or parts.password or parts.port not in (None, 443):
        return False
    if parts.path.lower() != '/main/default_os_prog.asp' or parts.fragment:
        return False
    items = parse_qsl(parts.query, keep_blank_values=True)
    return len(items) == 6 and len(set(k for k, _ in items)) == 6 and dict(items) == {
        'section': 'teachers', 'spec': 'tests',
        'testCoursTypeSelect': '', 'testGradeSelect': '',
        'testID': '0', 'testTopicSelect': '',
    }


def _safe_static_asset(request):
    """Only allow same-host JS/CSS GETs required for a functional form."""
    if request.method != 'GET':
        return False
    parts = urlsplit(request.url)
    if parts.scheme != 'https' or parts.hostname != HOST or parts.username or parts.password:
        return False
    if parts.port not in (None, 443) or parts.fragment:
        return False
    if request.resource_type == 'script':
        return parts.path.startswith(('/js/', '/main/js/', '/profile/js/')) and parts.path.endswith('.js')
    if request.resource_type == 'stylesheet':
        return parts.path.startswith(('/css/', '/main/css/', '/profile/css/')) and parts.path.endswith('.css')
    return False


def _preform_route(route):
    request = route.request
    # Login is finished before this router is installed. Only the known
    # blank form GET and harmless same-host static files may leave Chromium.
    if (request.method == 'GET' and _is_blank_form_get(request.url)) or _safe_static_asset(request):
        route.continue_()
    else:
        route.abort()


def _capture_and_abort(route, events):
    """Collect only network METHOD and broad type; cancel ALL requests."""
    req = route.request
    if len(events) < 20:
        events.append({
            'method': req.method if req.method in ('GET', 'POST') else 'OTHER',
            'kind': req.resource_type if req.resource_type in (
                'xhr', 'fetch', 'document', 'script', 'stylesheet', 'image',
            ) else 'other',
            'same_host': urlsplit(req.url).hostname == HOST,
        })
    # Deliberately NEVER call continue_() or fulfill() here.
    route.abort()


def _headroom_or_stop():
    from browser_runtime import memory_snapshot, MIB, STOP_PERCENT
    s = memory_snapshot()
    if s and s['working_set'] + 85 * MIB >= s['limit'] * STOP_PERCENT / 100:
        return False
    return True


def blocked_topic_refresh_trace():
    """Login, open only a known blank form, block all change-triggered traffic.

    This function MUST run under browser_runtime.run_browser's 430MiB guard.
    Does not call the normal HTTP save route or choose a topic.
    """
    from classtime_reader import _chromium_args
    from nigri_playwright import login
    from playwright.sync_api import sync_playwright
    from browser_runtime import log_memory

    if not os.environ.get('NIGRI_USERNAME') or not os.environ.get('NIGRI_PASSWORD'):
        return {'status': 'credentials_missing', 'nigri_writes': False}

    stage = 'starting_browser'
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=_chromium_args() + [
                '--disk-cache-size=0', '--media-cache-size=0',
            ])
            try:
                log_memory('blocked-topic-trace: chromium launched')
                if not _headroom_or_stop():
                    return {'status': 'insufficient_memory', 'nigri_writes': False}
                context = browser.new_context(service_workers='block')
                context.route('**/*', _hybrid_login_router)
                page = context.new_page()
                stage = 'login'
                login(page)  # Already-proven authentication only.
                log_memory('blocked-topic-trace: login complete')
                if not _headroom_or_stop():
                    return {'status': 'insufficient_memory_after_login', 'nigri_writes': False}
                # Only one approved empty form GET. No navigation POST permitted.
                context.unroute('**/*', _hybrid_login_router)
                context.route('**/*', _preform_route)
                stage = 'blank_form'
                page.goto(BLANK_FORM_URL, wait_until='domcontentloaded', timeout=15000)
                grade = page.locator('select[name="testGradeID"]')
                topic = page.locator('select[name="testTopicID"]')
                if grade.count() != 1 or topic.count() != 1:
                    return {'status': 'form_controls_unconfirmed',
                            'nigri_writes': False}
                if page.locator('select[name="testGradeID"] option[value="22590"]').count() != 1:
                    return {'status': 'b3_et_not_confirmed',
                            'nigri_writes': False}
                log_memory('blocked-topic-trace: blank form loaded')
                if not _headroom_or_stop():
                    return {'status': 'insufficient_memory_after_form', 'nigri_writes': False}

                # Wait for initial scripts to settle, then replace routing with
                # unconditional abort before a class-change event can execute.
                page.wait_for_timeout(250)
                context.unroute('**/*', _preform_route)
                events = []
                context.route('**/*', lambda route: _capture_and_abort(route, events))
                page.on('dialog', lambda dialog: dialog.dismiss())
                stage = 'select_b3_et'
                selection_executed = False
                try:
                    grade.select_option('22590', timeout=3500)
                    selection_executed = True
                except Exception:
                    # A synchronous AJAX request can throw when blocked.
                    # Still return only captured request metadata.
                    pass
                page.wait_for_timeout(700)
                kinds = sorted({x['kind'] for x in events})
                methods = sorted({x['method'] for x in events})
                return {
                    'status': 'requests_blocked_and_class_change_inspected',
                    'class_selection_event_completed': selection_executed,
                    'attempted_request_count_capped': len(events),
                    'attempted_methods': methods,
                    'attempted_types': kinds,
                    'attempted_xhr_or_fetch': any(x['kind'] in ('xhr', 'fetch') for x in events),
                    'post_blocked': any(x['method'] == 'POST' for x in events),
                    'request_to_other_host_blocked': any(not x['same_host'] for x in events),
                    'all_change_requests_blocked': True,
                    'post_refresh_topic_verified': False,
                    'no_topic_selected': True,
                    'no_mark_created': True,
                    'no_grade_saved': True,
                    'nigri_writes': False,
                }
            finally:
                browser.close()
                log_memory('blocked-topic-trace: browser closed')
    except Exception:
        # Never leak URL/body/browser exception with credentials or data.
        return {'status': 'topic_trace_' + stage + '_stopped_safely',
                'nigri_writes': False}
