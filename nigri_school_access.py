"""Schools: may this signed-in person use Nigri at all? (website PR B3-games#631, issue 550)

The website stores each teacher's school on the owner-written invitation:
    b3Games/workspaces/<ws>/schools/<schoolId>              {name, syncsToNigri}
    b3Games/workspaces/<ws>/teacherInvites/<id>/schoolId
    b3Games/workspaces/<ws>/teacherInvites/<id>/nigriLoginResetAt   (ms; set when the owner moves him to another school)

The website only hides buttons. THIS is the real rule, checked on every request:
  - the owner (verified email) is always allowed;
  - otherwise he must be an active teacher, his invitation must be his own (same email), he must not be a principal,
    and his school must exist with syncsToNigri exactly true.
A teacher whose school has sync turned off keeps his saved login, but it cannot be used until the owner turns it on.
"""
import re

OWNER_EMAIL = 'simcha5770@gmail.com'
_ID = re.compile(r'^[a-z0-9_]{1,60}$')
_INVITE = re.compile(r'^[a-z0-9_]{1,200}$')

MESSAGES = {
    'school_off': 'Nigri sync is not turned on for your school.',
    'no_school': 'Nigri sync is not turned on for your school.',
    'principal': 'Nigri sync is for classroom teachers.',
    'not_teacher': 'unauthorized',
}


def invite_key(email):
    return re.sub(r'^_+|_+$', '', re.sub(r'[^a-z0-9]+', '_', str(email or '').strip().lower()))


def _result(allowed, reason, school_id='', reset_at=None):
    return {'allowed': allowed, 'reason': reason, 'school_id': school_id, 'reset_at': reset_at}


def is_owner(claims, owner_email=OWNER_EMAIL):
    return bool(claims) and claims.get('email_verified') is True and \
        str(claims.get('email', '')).strip().lower() == owner_email.lower()


def nigri_access(read_path, workspace, uid, claims, owner_email=OWNER_EMAIL):
    """read_path(path) -> value or None (server reads as its service account). Never trusts the browser."""
    if is_owner(claims, owner_email):
        return _result(True, 'owner')
    if not uid or not claims or claims.get('email_verified') is not True:
        return _result(False, 'not_teacher')
    ws = 'b3Games/workspaces/' + workspace
    teacher = read_path(ws + '/teachers/' + uid)
    if not isinstance(teacher, dict) or teacher.get('active') is not True:
        return _result(False, 'not_teacher')
    email = str(claims.get('email', '')).strip().lower()
    invite_id = teacher.get('inviteId') if isinstance(teacher.get('inviteId'), str) else invite_key(email)
    if not invite_id or not _INVITE.match(invite_id):
        return _result(False, 'not_teacher')
    invite = read_path(ws + '/teacherInvites/' + invite_id)
    # The invitation must really be his, and still active.
    if not isinstance(invite, dict) or str(invite.get('email', '')).strip().lower() != email or invite.get('active') is False:
        return _result(False, 'not_teacher')
    school_id = invite.get('schoolId') if isinstance(invite.get('schoolId'), str) else ''
    reset_at = invite.get('nigriLoginResetAt') if isinstance(invite.get('nigriLoginResetAt'), (int, float)) else None
    if invite.get('accountType') == 'principal' or invite.get('role') == 'principal':
        return _result(False, 'principal', school_id, reset_at)
    if not school_id or not _ID.match(school_id):
        return _result(False, 'no_school', '', reset_at)
    school = read_path(ws + '/schools/' + school_id)
    if not isinstance(school, dict) or school.get('syncsToNigri') is not True:
        return _result(False, 'school_off', school_id, reset_at)
    return _result(True, 'ok', school_id, reset_at)


def login_was_reset(record, reset_at):
    """True when a saved login predates the owner moving him to another school, so it must be deleted."""
    if not reset_at or not isinstance(record, dict):
        return False
    saved = record.get('updatedAt')
    return not isinstance(saved, (int, float)) or saved < reset_at
