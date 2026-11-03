"""Match a Fun Torah Tools class to its Nigri grade and course ID (issue 550, step 3).

Rules (owner decision, issue 550):
- A class belongs to a Nigri GRADE ("B3 ET" or "B3 WT"). ET and WT default to their own grade; any other class needs
  the owner to set classes/<id>/nigriGrade. No grade -> no sync.
- Inside a grade the sync picks a COURSE ID for what is being synced (Davening, Morning 1-3, Friday 1-3). Match by
  course ID, never by name. Only IDs in COURSES can ever be targets, so shared rooms (Conference Room, Rally Room)
  can never be chosen, and any room that shows up in Nigri's own list is dropped by name as well.
- A course is usable only if the teacher's OWN Nigri login can see it (read-only list, via `lister`).
Nothing here talks to Nigri by itself: `lister` is passed in (a mock in tests; "not connected" until a read-only
Nigri course reader is built and approved), and until it exists every course stays switched off.
"""
import re

SLOTS = [('davening', 'Davening'), ('morning1', 'Morning Class 1'), ('morning2', 'Morning Class 2'),
         ('morning3', 'Morning Class 3'), ('friday1', 'Friday Class 1'), ('friday2', 'Friday Class 2'),
         ('friday3', 'Friday Class 3')]
LABELS = dict(SLOTS)

# Course IDs from the owner's Nigri "TeacherChilds" export (issue 550). WT has no Friday classes.
COURSES = {
    'B3 ET': {'davening': 28734, 'morning1': 28762, 'morning2': 28818, 'morning3': 28819,
              'friday1': 28910, 'friday2': 28948, 'friday3': 28947},
    'B3 WT': {'davening': 28733, 'morning1': 28722, 'morning2': 28814, 'morning3': 28815},
}
DEFAULT_GRADE = {'et': 'B3 ET', 'wt': 'B3 WT'}
SHARED_ROOMS = ('conference room', 'rally room')   # never sync targets
_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')


class MatchError(Exception):
    """Plain-words refusal. `code` is stable for the page; `message` is safe to show."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class CoursesUnavailable(Exception):
    """The read-only Nigri course reader is not connected, or Nigri could not be asked."""


def not_connected_lister(_credentials):
    raise CoursesUnavailable('Reading your Nigri classes is not connected yet')


def is_shared_room(name):
    text = str(name or '').lower()
    return any(room in text for room in SHARED_ROOMS)


def class_grade(read, workspace, class_id):
    """The Nigri grade for this class, or None. A bad stored value counts as no grade."""
    stored = read('b3Games/workspaces/%s/classes/%s/nigriGrade' % (workspace, class_id))
    if isinstance(stored, str) and stored in COURSES:
        return stored
    return DEFAULT_GRADE.get(class_id.lower()) if stored is None else None


def teacher_has_class(read, workspace, uid, class_id):
    """Is this class really his: an active teacher, the class on his list, and the class itself not switched off."""
    base = 'b3Games/workspaces/%s' % workspace
    return (read('%s/teachers/%s/active' % (base, uid)) is True
            and read('%s/teachers/%s/classIds/%s' % (base, uid, class_id)) is True
            and read('%s/classes/%s/active' % (base, class_id)) is not False)


def _visible_ids(lister, credentials):
    """IDs of courses the teacher's own Nigri login can see, minus shared rooms."""
    seen = set()
    for course in lister(credentials) or []:
        try:
            cid = int(course.get('id'))
        except (TypeError, ValueError, AttributeError):
            continue
        if not is_shared_room(course.get('name')):
            seen.add(cid)
    return seen


def course_status(read, workspace, uid, class_id, credentials, lister):
    """What the page may show: this class's grade and, per course, whether syncing to it is allowed and why not."""
    if not isinstance(class_id, str) or not _ID.fullmatch(class_id):
        raise MatchError('bad_class', 'Choose one of your classes.')
    if not teacher_has_class(read, workspace, uid, class_id):
        raise MatchError('not_your_class', 'That is not one of your classes.')
    grade = class_grade(read, workspace, class_id)
    if grade is None:
        raise MatchError('no_grade', 'The owner has not set which Nigri grade this class belongs to.')
    reason, visible = None, set()
    if credentials is None:
        reason = 'Save your Nigri login first.'
    else:
        try:
            visible = _visible_ids(lister, credentials)
        except CoursesUnavailable as exc:
            reason = str(exc) + '.'
        except Exception:
            reason = 'Could not check your Nigri classes right now.'
    courses = []
    for slot, label in SLOTS:
        cid = COURSES[grade].get(slot)
        if cid is None:
            continue
        ok = reason is None and cid in visible
        courses.append({'slot': slot, 'label': label, 'courseId': cid, 'available': ok,
                        'reason': None if ok else (reason or 'Your Nigri login cannot see this class.')})
    return {'class': class_id, 'grade': grade, 'courses': courses}


def resolve_course(read, workspace, uid, class_id, slot, credentials, lister):
    """Used by the sync (step 4): the ONE course ID to save into, or a refusal. Checked on the server every time."""
    status = course_status(read, workspace, uid, class_id, credentials, lister)
    if slot not in LABELS:
        raise MatchError('bad_slot', 'Choose what is being synced.')
    for course in status['courses']:
        if course['slot'] == slot:
            if not course['available']:
                raise MatchError('not_visible', course['reason'])
            return {'grade': status['grade'], 'slot': slot, 'courseId': course['courseId']}
    raise MatchError('no_such_course', '%s has no %s.' % (status['grade'], LABELS[slot]))
