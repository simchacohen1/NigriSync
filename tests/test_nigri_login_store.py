"""Encrypted per-teacher Nigri login (issue 550, step 1). Made-up names and passwords only; no real Nigri or Firebase."""
import base64
import json
import os
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nigri_login_store as S

KEY_A = base64.b64encode(bytes(range(32))).decode()
KEY_B = base64.b64encode(bytes(range(100, 132))).decode()
UID1, UID2 = 'teacherUidOne123', 'teacherUidTwo456'
USER, PW = 'faketeacher@example.test', 'Fake-Pa55word!-never-real'


def ring(raw='k1:' + KEY_A):
    return S.load_keyring(raw)


def store(raw='k1:' + KEY_A, backend=None):
    return S.NigriLoginStore(backend or S.MemoryBackend(), ring(raw))


class KeyringTests(unittest.TestCase):
    def test_parses_current_and_old(self):
        cur, keys = ring('new:%s,old:%s' % (KEY_B, KEY_A))
        self.assertEqual(cur, 'new')
        self.assertEqual(set(keys), {'new', 'old'})

    def test_fails_closed(self):
        for bad in ('', 'k1', 'k1:not base64!!', 'k1:' + base64.b64encode(b'short').decode(),
                    'k1:%s,k1:%s' % (KEY_A, KEY_B), 'bad id:' + KEY_A):
            with self.assertRaises(S.LoginStoreError, msg=bad):
                S.load_keyring(bad)

    def test_reads_environment(self):
        old = os.environ.get('NIGRI_LOGIN_KEYS')
        os.environ['NIGRI_LOGIN_KEYS'] = 'k1:' + KEY_A
        try:
            self.assertEqual(S.load_keyring()[0], 'k1')
        finally:
            os.environ.pop('NIGRI_LOGIN_KEYS') if old is None else os.environ.update(NIGRI_LOGIN_KEYS=old)

    def test_error_text_never_contains_key(self):
        try:
            S.load_keyring('k1:' + KEY_A + 'x')
        except S.LoginStoreError as e:
            self.assertNotIn(KEY_A, str(e))


class StoredFormTests(unittest.TestCase):
    def test_stored_record_is_scrambled(self):
        b = S.MemoryBackend()
        store(backend=b).save(UID1, USER, PW)
        stored = json.dumps(b.data)
        for secret in (USER, PW, USER[-4:], KEY_A, 'faketeacher', 'Pa55word'):
            self.assertNotIn(secret, stored)
        self.assertEqual(set(b.data[UID1]), {'v', 'kid', 'n', 'ct', 'updatedAt'})

    def test_same_login_encrypts_differently_each_time(self):
        s = store()
        s.save(UID1, USER, PW)
        first = dict(s.backend.data[UID1])
        s.save(UID1, USER, PW)
        self.assertNotEqual(first['n'], s.backend.data[UID1]['n'])
        self.assertNotEqual(first['ct'], s.backend.data[UID1]['ct'])

    def test_round_trip_and_status_has_no_password(self):
        s = store()
        saved = s.save(UID1, USER, PW, now=1700000000)
        self.assertEqual(saved, {'saved': True, 'hint': 'test', 'updatedAt': 1700000000000})
        self.assertNotIn(PW, json.dumps(saved))
        self.assertEqual(s.status(UID1), saved)
        creds = s.get_credentials(UID1)
        self.assertEqual((creds.username, creds.password), (USER, PW))

    def test_credentials_repr_is_redacted(self):
        c = store()
        c.save(UID1, USER, PW)
        text = repr(c.get_credentials(UID1)) + str(c.get_credentials(UID1))
        self.assertNotIn(PW, text)
        self.assertNotIn(USER, text)

    def test_nothing_saved(self):
        s = store()
        self.assertEqual(s.status(UID1), {'saved': False})
        self.assertIsNone(s.get_credentials(UID1))


class RefusalTests(unittest.TestCase):
    def test_wrong_key_cannot_open(self):
        b = S.MemoryBackend()
        store(backend=b).save(UID1, USER, PW)
        with self.assertRaises(S.LoginStoreError):
            store('k1:' + KEY_B, backend=b).get_credentials(UID1)

    def test_record_moved_to_another_teacher_will_not_open(self):
        b = S.MemoryBackend()
        s = store(backend=b)
        s.save(UID1, USER, PW)
        b.data[UID2] = b.data[UID1]
        with self.assertRaises(S.LoginStoreError):
            s.get_credentials(UID2)
        self.assertEqual(s.get_credentials(UID1).password, PW)

    def test_tampering_and_junk_fail_generically(self):
        b = S.MemoryBackend()
        s = store(backend=b)
        s.save(UID1, USER, PW)
        good = dict(b.data[UID1])
        raw = bytearray(base64.b64decode(good['ct']))
        raw[0] ^= 1
        cases = [dict(good, ct=base64.b64encode(bytes(raw)).decode()), dict(good, kid='gone'), dict(good, v=2),
                 dict(good, n='!!!'), {'junk': 1}, 'text', 5]
        for bad in cases:
            b.data[UID1] = bad
            with self.assertRaises(S.LoginStoreError) as cm:
                s.get_credentials(UID1)
            self.assertNotIn(PW, str(cm.exception))

    def test_bad_input(self):
        s = store()
        for u, p in (('', PW), (USER, ''), ('   ', PW), (USER, 'x' * 300), (None, PW), (USER, 123)):
            with self.assertRaises(S.LoginStoreError):
                s.save(UID1, u, p)
        self.assertEqual(s.backend.data, {})

    def test_bad_teacher_ids(self):
        s = store()
        for uid in ('', '../x', 'a/b', 'short', None, 'ok' * 100, 'uid with space'):
            for call in (lambda: s.save(uid, USER, PW), lambda: s.status(uid), lambda: s.remove(uid),
                         lambda: s.get_credentials(uid)):
                with self.assertRaises(S.LoginStoreError):
                    call()

    def test_replace_and_remove(self):
        s = store()
        s.save(UID1, USER, PW)
        s.save(UID1, 'other.user@example.test', 'Another-Fake-1')
        self.assertEqual(s.get_credentials(UID1).password, 'Another-Fake-1')
        self.assertEqual(s.remove(UID1), {'saved': False})
        self.assertEqual(s.backend.data, {})
        self.assertIsNone(s.get_credentials(UID1))

    def test_teachers_are_separate(self):
        s = store()
        s.save(UID1, USER, PW)
        s.save(UID2, 'second@example.test', 'Second-Fake-2')
        self.assertEqual(s.get_credentials(UID1).password, PW)
        self.assertEqual(s.get_credentials(UID2).password, 'Second-Fake-2')
        s.remove(UID1)
        self.assertEqual(s.get_credentials(UID2).password, 'Second-Fake-2')


class KeyReplacementTests(unittest.TestCase):
    def test_old_record_opens_then_reencrypts_with_new_key(self):
        b = S.MemoryBackend()
        store('old:' + KEY_A, backend=b).save(UID1, USER, PW, now=1700000000)
        both = 'new:%s,old:%s' % (KEY_B, KEY_A)
        s = store(both, backend=b)
        self.assertEqual(s.get_credentials(UID1).password, PW)
        self.assertEqual(b.data[UID1]['kid'], 'old')
        self.assertTrue(s.reencrypt(UID1))
        self.assertEqual(b.data[UID1]['kid'], 'new')
        self.assertEqual(b.data[UID1]['updatedAt'], 1700000000000)
        with self.assertRaises(S.LoginStoreError):   # old key alone can no longer open it
            store('old:' + KEY_A, backend=b).get_credentials(UID1)
        self.assertEqual(store('new:' + KEY_B, backend=b).get_credentials(UID1).password, PW)
        self.assertFalse(s.reencrypt(UID2))


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeSession:
    """Stands in for the Firebase connection: remembers what was sent and keeps data in a dict."""

    def __init__(self, status=200):
        self.calls, self.db, self.status = [], {}, status

    def get(self, url, timeout=None):
        self.calls.append(('GET', url, None))
        return FakeResponse(self.status, self.db.get(url))

    def put(self, url, data=None, timeout=None):
        self.calls.append(('PUT', url, data))
        self.db[url] = json.loads(data)
        return FakeResponse(self.status, self.db[url])

    def delete(self, url, timeout=None):
        self.calls.append(('DELETE', url, None))
        self.db.pop(url, None)
        return FakeResponse(self.status)


class FirebaseBackendTests(unittest.TestCase):
    BASE = 'https://fake-project-default-rtdb.example.test'

    def make(self, status=200):
        session = FakeSession(status)
        return session, S.NigriLoginStore(S.FirebaseRestBackend(self.BASE + '/', session=session), ring())

    def test_goes_only_to_the_private_login_path(self):
        session, s = self.make()
        s.save(UID1, USER, PW)
        s.status(UID1)
        s.remove(UID1)
        expected = '%s/b3Private/nigriLogins/%s.json' % (self.BASE, UID1)
        self.assertEqual([c[:2] for c in session.calls],
                         [('PUT', expected), ('GET', expected), ('DELETE', expected)])
        self.assertEqual(session.db, {})

    def test_firebase_only_receives_scrambled_text(self):
        session, s = self.make()
        s.save(UID1, USER, PW)
        sent = ' '.join(str(c) for c in session.calls)
        for secret in (USER, PW, 'faketeacher', KEY_A):
            self.assertNotIn(secret, sent)
        self.assertEqual(s.get_credentials(UID1).password, PW)

    def test_firebase_failure_is_a_safe_error(self):
        _, s = self.make(status=401)
        with self.assertRaises(S.LoginStoreError) as cm:
            s.save(UID1, USER, PW)
        self.assertNotIn(PW, str(cm.exception))
        self.assertNotIn(USER, str(cm.exception))

    def test_database_url_must_be_https(self):
        with self.assertRaises(S.LoginStoreError):
            S.FirebaseRestBackend('http://insecure.example.test', session=FakeSession())
        with self.assertRaises(S.LoginStoreError):
            S.FirebaseRestBackend('', session=FakeSession())


class NoPlaintextInSourceTests(unittest.TestCase):
    def test_module_never_logs_or_prints(self):
        text = (Path(S.__file__)).read_text(encoding='utf-8')
        for word in ('print(', 'logging', 'logger', 'app.logger'):
            self.assertNotIn(word, text)

    def test_no_route_uses_the_store_yet(self):
        """Step 1 has no teacher-facing change: nothing but this module and its test may use it."""
        root = Path(S.__file__).parent
        users = [p.name for p in root.glob('*.py') if p.name != 'nigri_login_store.py'
                 and 'nigri_login_store' in p.read_text(encoding='utf-8')]
        self.assertEqual(users, [])


if __name__ == '__main__':
    unittest.main()
