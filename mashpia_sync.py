"""Daily Checklist -> Mashpia "Mark Missions" (Daily tab). Plain HTTPS, no browser.

How Mashpia works (captured from the real site):
  POST /api/auth/login               {"username","password"}            -> cookie session
  POST /api/missions/grid?action=get {"type":"daily","date":<JDN>}      -> {data:{missions:[...],soldiers:[...]}}
  POST /api/missions/mark            {"user_ids":[..],"grid_id":N,"dates":[<JDN>],"mark":"1"|"0"}
Dates are Julian Day Numbers. A soldier row has registered / absent flags (the "greyed out" rows) and `marks`
{grid_id: {done_qty}}.

Safety rules built in:
  * only ever sends mark "1" (never "0": nothing is ever unchecked);
  * cells already checked are not sent again;
  * greyed-out soldiers (not registered, or absent that day) are skipped;
  * only missions Mashpia shows as available (disabled == 0) are matched.
"""
import datetime
import re
import unicodedata

BASE = 'https://mashpia.com'
TIMEOUT = 25


class MashpiaError(Exception):
    """Safe to show a teacher: never contains a password or cookie."""


def julian_day(iso_date):
    try:
        d = datetime.date.fromisoformat(str(iso_date))
    except ValueError:
        raise MashpiaError('Invalid date')
    return d.toordinal() + 1721425


def norm(text):
    """Case, spacing, punctuation and apostrophe-insensitive form for item names."""
    t = unicodedata.normalize('NFKC', str(text or '')).casefold()
    t = re.sub(r"[’‘'`\"]", '', t)
    return re.sub(r'[\W_]+', ' ', t).strip()


def name_key(text):
    """Student names: word order does not matter ("Ari Greenberg" == "Greenberg, Ari")."""
    return ' '.join(sorted(norm(text).split()))


class MashpiaClient:
    def __init__(self, session=None, base=BASE):
        if session is None:
            import requests
            session = requests.Session()
            session.headers['User-Agent'] = 'Mozilla/5.0 (compatible; B3-Mashpia-Sync)'
        self.s = session
        self.base = base

    def _post(self, path, payload):
        try:
            r = self.s.post(self.base + path, json=payload, timeout=TIMEOUT)
        except Exception:
            raise MashpiaError('Could not reach Mashpia right now.')
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code >= 400 or not isinstance(body, dict):
            raise MashpiaError('Mashpia did not answer properly (status %s).' % r.status_code)
        return body

    def login(self, username, password):
        body = self._post('/api/auth/login', {'username': username, 'password': password})
        if body.get('success') is not True:
            raise MashpiaError('Mashpia did not accept that username and password.')

    def grid(self, jdn):
        body = self._post('/api/missions/grid?action=get', {'type': 'daily', 'date': jdn})
        data = body.get('data') if body.get('success') is True else None
        if not isinstance(data, dict) or not isinstance(data.get('missions'), list) or not isinstance(data.get('soldiers'), list):
            raise MashpiaError('Could not read the Mashpia grid. Is this a school account?')
        return data

    def mark(self, user_ids, grid_id, jdn):
        body = self._post('/api/missions/mark', {'user_ids': user_ids, 'grid_id': grid_id, 'dates': [jdn], 'mark': '1'})
        data = body.get('data') if body.get('success') is True else None
        if not isinstance(data, dict):
            return set()
        return {str(k) for k in data}


def plan(grid, students):
    """Pure matching step. students: [{'name': str, 'items': [item names answered Yes]}].
    Returns {'sends': {grid_id: [user_id, ...]}, 'synced': [names], 'not_found': [names], 'greyed': [names],
             'missing_items': [names]}. Nothing here talks to Mashpia."""
    missions = {}
    for m in grid['missions']:
        if isinstance(m, dict) and not m.get('disabled') and isinstance(m.get('grid_id'), int):
            missions.setdefault(norm(m.get('cat')), m['grid_id'])
    by_name = {}
    for sol in grid['soldiers']:
        if isinstance(sol, dict) and isinstance(sol.get('user_id'), int):
            by_name.setdefault(name_key(sol.get('name')), []).append(sol)
    out = {'sends': {}, 'synced': [], 'not_found': [], 'greyed': [], 'missing_items': []}
    missing = set()
    for st in students:
        label = str(st.get('name') or '').strip()
        matches = by_name.get(name_key(label), [])
        if len(matches) != 1:           # none, or ambiguous: never guess
            out['not_found'].append(label)
            continue
        sol = matches[0]
        if sol.get('registered') is False or sol.get('absent'):
            out['greyed'].append(label)
            continue
        marks = sol.get('marks') if isinstance(sol.get('marks'), dict) else {}
        for item in st.get('items') or []:
            gid = missions.get(norm(item))
            if gid is None:
                missing.add(str(item))
                continue
            done = marks.get(str(gid)) or marks.get(gid)
            if isinstance(done, dict) and (done.get('done_qty') or 0) >= 1:
                continue                # already checked in Mashpia
            out['sends'].setdefault(gid, []).append(sol['user_id'])
        out['synced'].append((label, sol['user_id']))
    out['missing_items'] = sorted(missing)
    return out


def sync_day(client, iso_date, students):
    jdn = julian_day(iso_date)
    p = plan(client.grid(jdn), students)
    failed_ids = set()
    for gid, ids in p['sends'].items():
        ok = client.mark(ids, gid, jdn)
        failed_ids.update(i for i in ids if str(i) not in ok)
    synced, failed = [], []
    for label, uid in p['synced']:
        (failed if uid in failed_ids else synced).append(label)
    return {'synced': len(synced), 'not_found': p['not_found'] + failed, 'greyed': p['greyed'],
            'missing_items': p['missing_items']}
