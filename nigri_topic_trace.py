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


def _preform_route(route, stats=None):
    request = route.request
    # Login is finished before this router is installed. Only the known
    # blank form GET and harmless same-host static files may leave Chromium.
    if (request.method == 'GET' and _is_blank_form_get(request.url)) or _safe_static_asset(request):
        route.continue_()
    else:
        if stats is not None:
            kind = request.resource_type
            if kind == 'script':
                stats['blocked_scripts'] = min(100, stats.get('blocked_scripts', 0) + 1)
            elif kind == 'stylesheet':
                stats['blocked_stylesheets'] = min(100, stats.get('blocked_stylesheets', 0) + 1)
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




def _form_snapshot(page, *, instrument=False):
    """Tiny DOM-only snapshot; never export form HTML, options, names or URLs."""
    js = """(instrument) => {
      const grade = document.querySelector('select[name="testGradeID"]');
      const topic = document.querySelector('select[name="testTopicID"]');
      if (!grade || !topic) return null;
      if (instrument) {
        const state = {changeCount:0, oldTopic:topic};
        grade.addEventListener('change', () => {state.changeCount++}, true);
        window.__fttBlockedTraceState = state;
      }
      const state = window.__fttBlockedTraceState;
      const hasShoroshim = Array.from(topic.options).some(o => o.value === '142');
      return {
        selected_b3_et: grade.value === '22590',
        shoroshim_option_present: hasShoroshim,
        topic_option_count_capped: Math.min(topic.options.length, 100),
        change_event_seen: !!(state && state.changeCount > 0),
        topic_element_replaced: !!(state && state.oldTopic !== topic),
        onchange_attribute_present: !!grade.getAttribute('onchange')
      };
    }"""
    observed = page.evaluate(js, instrument)
    if not isinstance(observed, dict):
        return None
    keys = ('selected_b3_et', 'shoroshim_option_present',
            'topic_option_count_capped', 'change_event_seen',
            'topic_element_replaced', 'onchange_attribute_present')
    # Coerce the report to a fixed allowlist, never copy arbitrary page data.
    return {k: (min(100, max(0, int(observed[k])))
                if k == 'topic_option_count_capped' else observed[k] is True)
            for k in keys if k in observed}


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
                preform_stats = {'blocked_scripts': 0, 'blocked_stylesheets': 0}
                preform_router = lambda route: _preform_route(route, preform_stats)
                context.route('**/*', preform_router)
                script_errors = {'count': 0}
                page.on('pageerror', lambda error: script_errors.__setitem__(
                    'count', min(100, script_errors['count'] + 1)
                ))
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
                initial = _form_snapshot(page, instrument=True)
                if initial is None:
                    return {'status': 'form_snapshot_unavailable', 'nigri_writes': False}
                context.unroute('**/*', preform_router)
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
                after = _form_snapshot(page) or {}
                kinds = sorted({x['kind'] for x in events})
                methods = sorted({x['method'] for x in events})
                return {
                    'status': ('requests_blocked_and_class_change_inspected'
                               if events else 'no_class_change_requests_observed'),
                    'class_selection_event_completed': selection_executed,
                    'change_event_observed': after.get('change_event_seen', False),
                    'b3_et_preselected': initial.get('selected_b3_et', False),
                    'b3_et_selected_after': after.get('selected_b3_et', False),
                    'shoroshim_present_initially': initial.get('shoroshim_option_present', False),
                    'shoroshim_present_after_blocked_change': after.get('shoroshim_option_present', False),
                    'topic_options_initial_count': initial.get('topic_option_count_capped', 0),
                    'topic_options_after_count': after.get('topic_option_count_capped', 0),
                    'topic_dom_replaced': after.get('topic_element_replaced', False),
                    'inline_grade_handler_present': initial.get('onchange_attribute_present', False),
                    'blocked_scripts_before_change': preform_stats['blocked_scripts'],
                    'blocked_stylesheets_before_change': preform_stats['blocked_stylesheets'],
                    'script_errors_seen_capped': script_errors['count'],
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
