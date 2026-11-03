"""Run this yourself to SEE which Nigri classes your own login can see. Read only.

    python read_courses_cli.py

It asks for your Nigri username and password (typed here, hidden, never saved or printed), signs in once, reads only the
class picker, and prints course IDs and names. It cannot save, submit or change anything on Nigri, and it never opens a
student list. Nothing is sent anywhere except to Nigri's own sign-in. A copy of what it found (IDs and names only) is
written to nigri-courses-found.json next to this file.
"""
import getpass
import json
import sys
from pathlib import Path

import nigri_course_reader as R


def show(report):
    print('\nCourses Nigri showed this login: %d (shared rooms removed)\n' % report['total'])
    for grade, g in report['grades'].items():
        print(grade)
        for c in g['found']:
            print('  FOUND    %-16s id %s  (Nigri calls it: %s)' % (c['label'], c['courseId'], c['nigriName']))
        for c in g['missing']:
            print('  MISSING  %-16s id %s' % (c['label'], c['courseId']))
    if report['other']:
        print('\nOther courses this login can see (not in our table):')
        for o in report['other']:
            print('  id %s  %s' % (o['id'], o['name']))
    if report['noNumericId']:
        print('\nCourses whose picker value was not a number (cannot be matched by ID):')
        for n in report['noNumericId']:
            print('  ' + n)


def main():
    print('Read-only check of your own Nigri classes. Nothing will be saved or changed.')
    username = input('Nigri username: ').strip()
    password = getpass.getpass('Nigri password (hidden): ')
    try:
        raw = R.login_and_read(username, password, R.playwright_launcher('chrome'))
    except R.ReaderError as exc:
        print('\nStopped: %s' % exc)
        return 1
    except Exception as exc:
        print('\nStopped: could not complete the read (%s).' % type(exc).__name__)
        return 1
    finally:
        password = None
    report = R.summarize(R.parse_options(raw))
    show(report)
    Path(__file__).with_name('nigri-courses-found.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('\nSaved a copy (IDs and names only) to nigri-courses-found.json')
    return 0


if __name__ == '__main__':
    sys.exit(main())
