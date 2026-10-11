"""The REAL save from a Classtime review into Nigri, through the existing run_marks_sync() code.

Safety rules, all enforced here on the server (the browser only sends choices, never rows or reports):
- rows are rebuilt from the loaded review; PDFs are read from the server's own disk;
- a review can be saved once; a second attempt is refused, and an unknown outcome is never retried automatically;
- a duplicate or an unreadable "already saved?" check needs an explicit override that repeats the exact session code and mark name;
- a PDF problem or no scored boys can never be overridden;
- one-boy test saves are named "TEST DELETE ME ..." and never click "Marking done".
"""
import os
import re

import classtime_review as review_store
import classtime_sync as sync

TEST_PREFIX = 'TEST DELETE ME'
OVERRIDABLE = ('duplicate', 'could_not_check')
# run_browser refused before starting anything, so nothing can have been written.
NOT_STARTED = ('Another browser operation is running', 'Classtime retrieval is disabled because')
LEDGER = {}   # (session_code, class_section) -> {'state', 'test_id'}; lives only as long as this server process


def save_time_limit():
    try: return max(120, min(int(os.environ.get('CLASSTIME_SAVE_TIME_LIMIT', 420)), 900))
    except (TypeError, ValueError): return 420


def effective_name(name, one_boy):
    """One-boy test saves are always visibly named so the test mark is easy to find and delete in Nigri."""
    if not one_boy or name.upper().startswith(TEST_PREFIX): return name[:120]
    return (TEST_PREFIX + ' - ' + name)[:120]


# ---- per-review save state (guards against a second click and a second save) ----

def claim(review_id, owner):
    """Atomically move a review from 'not saved' to 'saving'. Returns (job, None) or (None, (status_code, message))."""
    import time
    with review_store.LOCK:
        job = review_store.JOBS.get(review_id)
        if not job or job['owner'] != owner or job['expires'] < time.time(): return None, (404, 'Review expired or unavailable. Load it again.')
        if job['status'] != 'ready': return None, (409, 'The review is not ready yet')
        state = (job.get('save') or {}).get('state')
        if state == 'saving': return None, (409, 'A save is already running for this review. Wait for it to finish; do not click again.')
        if state == 'saved': return None, (409, 'This review was already saved to Nigri (mark #' + str(job['save'].get('test_id', '?')) + '). It will not be saved twice.')
        if state in ('partial', 'unknown'):
            return None, (409, 'An earlier save of this review did not finish cleanly and may have left a partial mark in Nigri'
                          + ((' (mark #' + str(job['save']['test_id']) + ')') if job['save'].get('test_id') else '') + '. Check Nigri first. It will not be retried automatically.')
        job['save'] = {'state': 'saving'}
        return job, None


def settle(review_id, state, test_id=None, key=None):
    """Record how the save ended. state: 'none' (nothing was written), 'saved', 'partial' or 'unknown'."""
    with review_store.LOCK:
        job = review_store.JOBS.get(review_id)
        if job is not None:
            if state == 'none': job.pop('save', None)
            else: job['save'] = {'state': state, 'test_id': test_id}
        if key and state != 'none': LEDGER[key] = {'state': state, 'test_id': test_id}


# ---- planning (pure; nothing is sent anywhere) ----

def plan(result, pdfs, body, topics):
    """Validate the request and rebuild what a save would send. Returns a dict, or raises ValueError (HTTP 400).
    Hard problems come back in 'hard_blockers' and can never be overridden."""
    if not isinstance(body, dict): raise ValueError('Invalid request')
    options = sync.validate_options(body, result, topics)
    only = body.get('only_student')
    if only is not None and (not isinstance(only, str) or not 0 < len(only) <= 120): raise ValueError('Choose one boy from the list')
    options['test_name'] = effective_name(options['test_name'], only is not None)
    source = result
    if only is not None:
        chosen = [s for s in result['students'] if s.get('nigri') and s['nigri']['name'] == only]
        if len(chosen) != 1: raise ValueError('That boy is not matched in this review')
        if chosen[0]['points'] is None: raise ValueError('That boy has no score, so there is nothing to save for him')
        source = dict(result, students=chosen)   # the one-boy rule applies to him alone, not to the rest of the class
    packet, rows, hard = sync.build_packet(source, pdfs, options)
    packet['mark_all_done'] = only is None     # a one-boy test never clicks "Marking done"
    return {'options': options, 'only': only, 'packet': packet, 'rows': rows, 'hard_blockers': hard, 'source': source}


def check_override(body, options, status, detail, code):
    """None if the save may go ahead, otherwise (http_status, message)."""
    if status == 'clear': return None
    if status not in OVERRIDABLE: return 409, 'The "already saved?" check gave an unexpected answer, so nothing was saved.'
    what = ('Nigri already has a mark for this session' if status == 'duplicate' else 'Could not check whether this session is already saved') + (' (' + detail + ')' if detail else '')
    if body.get('allow_duplicate') is not True:
        return 409, what + '. Nothing was saved. To save anyway, send allow_duplicate with the exact session code and mark name.'
    if body.get('confirm_session_code') != code or body.get('confirm_test_name') != options['test_name']:
        return 400, 'To save anyway, repeat the exact session code and mark name (' + options['test_name'] + ').'
    return None


# ---- the save itself (runs in the memory-guarded child process) ----

def save_job(result, pdfs, body, topics):
    """Rebuilds the packet WITH the PDFs and calls the existing run_marks_sync(). Never raises for a Nigri problem: it returns
    a plain dict so the reason survives the process boundary. Returns picklable data only."""
    from nigri_playwright import run_marks_sync, SyncError
    planned = plan(result, pdfs, body, topics)
    packet, _, _ = sync.build_packet(planned['source'], pdfs, planned['options'], with_reports=True)
    try:
        done = run_marks_sync(class_section=result['class_section'], topic=planned['options']['topic'], test_name=planned['options']['test_name'],
                              students=packet['students'], mark_type=planned['options']['type'], test_date=planned['options']['date'],
                              description=planned['options']['description'], mark_all_done=planned['packet']['mark_all_done'],
                               classtime_memory_saver=True)
        return {'ok': True, 'test_id': done.get('test_id'), 'results': list(done.get('results') or [])}
    except SyncError as error:
        return {'ok': False, 'detail': str(error)[:300], 'results': list(getattr(error, 'partial_results', None) or [])}
    except Exception:
        return {'ok': False, 'detail': 'The save stopped with an error', 'results': []}


def interpret(outcome, rows, one_boy):
    """Turn run_marks_sync's result lines into a plain per-boy report. Returns (per_boy, warnings, saved_ok)."""
    lines = outcome.get('results') or []
    per_boy, failed = [], 0
    for row in rows:
        if row['action'] == 'skip':
            per_boy.append({'classtime_name': row['classtime_name'], 'nigri_name': None, 'outcome': 'skipped', 'detail': 'Not matched to a Nigri student'}); continue
        name = row['nigri_name']
        line = next((l for l in lines if l.startswith(name + ': ')), None)
        if line is None: outcome_text, detail = 'not reached', 'The save stopped before this boy'
        elif 'FAILED' in line: outcome_text, detail = 'failed', line.split('FAILED', 1)[1].strip(' -:')[:160]; failed += 1
        else: outcome_text, detail = ('left blank' if row['action'] == 'blank' else 'saved'), ''
        per_boy.append({'classtime_name': row['classtime_name'], 'nigri_name': name, 'outcome': outcome_text, 'detail': detail})
    warnings = [l[:200] for l in lines if l.startswith('Could not')]
    final = any('Final Save! submitted' in l for l in lines)
    return per_boy, warnings, bool(outcome.get('ok')) and final and failed == 0 and not any(b['outcome'] == 'not reached' for b in per_boy)


def classify_stop(message):
    """Reason text for a save that was stopped from outside (not by Nigri)."""
    text = (message or '').lower()
    if 'memory' in text: return 'the server stopped it to protect its memory'
    if 'timed out' in text or 'time limit' in text: return 'it took longer than the time limit'
    return 'the save process ended unexpectedly'
