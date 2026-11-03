"""Run this yourself to check your own Nigri sign-in and SEE what the page contains. Read only.

    python read_courses_cli.py

It asks for your Nigri username and password (typed here, hidden, never saved or printed), signs in once, and tells you
plainly whether the sign-in worked. It then prints only: the page title, the page address (with hidden query values),
and the NAMES of dropdowns and form fields. It never prints or saves student names, attendance, marks, dropdown choices,
field values or your password. It cannot save, submit or change anything on Nigri. A copy of what it printed is written
to nigri-structure-found.json next to this file.
"""
import getpass
import json
import sys
from pathlib import Path

import nigri_course_reader as R


def describe_field(f):
    bits = [f['tag'] + (':' + f['type'] if f.get('type') and f['type'] != f['tag'] else '')]
    if f.get('id'): bits.append('id=' + f['id'])
    if f.get('name'): bits.append('name=' + f['name'])
    if f['tag'] == 'select':
        bits.append('%s choices' % f.get('optionCount'))
        bits.append('%s look like "B3 ET/WT" classes' % f.get('optionsMatchingClassNames'))
        bits.append('%s have a number as value' % f.get('optionsWithNumericValue'))
    if f.get('count', 1) > 1: bits.append('(%d of these)' % f['count'])
    return '  - ' + ', '.join(bits)


def show(result):
    print('\n==== SIGN-IN: %s ====' % result['signIn'])
    print(result['signInDetail'])
    for i, page in enumerate(result['pages']):
        print('\n%s %d' % ('Page' if i == 0 else 'Frame', i))
        print('  title:   %s' % (page.get('title') or '(none)'))
        print('  address: %s' % page['address'])
        if page.get('unreadable'):
            print('  (could not be read)')
        fields = page.get('fields', [])
        selects = [f for f in fields if f['tag'] == 'select']
        print('  dropdowns: %d, kinds of fields and buttons: %d' % (len(selects), len(fields)))
        for f in fields:
            print(describe_field(f))
        for sub in page.get('frames', []):
            print('  contains a frame: id=%s name=%s address=%s' % (sub.get('id'), sub.get('name'), sub.get('src')))
    print('\nClass picker (select#selNewCours) found: %s' % ('YES' if result['classPickerFound'] else 'NO'))
    if result['courses'] is not None:
        report = R.summarize(result['courses'])
        for grade, g in report['grades'].items():
            print(grade)
            for c in g['found']:
                print('  FOUND    %-16s id %s' % (c['label'], c['courseId']))
            for c in g['missing']:
                print('  MISSING  %-16s id %s' % (c['label'], c['courseId']))
        result['courseSummary'] = report


def main():
    print('Read-only check of your own Nigri sign-in. Nothing will be saved or changed on Nigri.')
    username = input('Nigri username: ').strip()
    password = getpass.getpass('Nigri password (hidden): ')
    try:
        result = R.login_and_inspect(username, password, R.playwright_launcher('chrome'))
    except Exception as exc:
        print('\nStopped: could not complete the check (%s).' % type(exc).__name__)
        return 1
    finally:
        password = None
    show(result)
    result.pop('courses', None)
    Path(__file__).with_name('nigri-structure-found.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print('\nSaved a copy (names only) to nigri-structure-found.json')
    return 0


if __name__ == '__main__':
    sys.exit(main())
