"""Sign-in token checks: clock tolerance, exact refusal reasons in the log (never the token), one owner-email setting."""
import logging
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import firebase_token
import nigri_school_access as A

SECRET = 'SECRET-TOKEN-VALUE-abc123'  # made-up


def claims(**kw):
    base = {'iss': 'https://securetoken.google.com/b3-games', 'email': 'owner@example.test', 'email_verified': True, 'sub': 'uid1'}
    base.update(kw)
    return base


class VerifyTests(unittest.TestCase):
    def test_good_token_passes_and_gets_clock_tolerance(self):
        with patch('google.oauth2.id_token.verify_firebase_token', return_value=claims()) as m:
            result, reason = firebase_token.verify(SECRET, 'b3-games')
        self.assertEqual(result['sub'], 'uid1'); self.assertIsNone(reason)
        self.assertEqual(m.call_args.kwargs['clock_skew_in_seconds'], firebase_token.CLOCK_SKEW_SECONDS)
        self.assertGreaterEqual(firebase_token.CLOCK_SKEW_SECONDS, 5)

    def test_refusal_reasons_are_logged_without_the_token(self):
        cases = [
            (ValueError('Token used too early, 100 < 105. Check that your computer\'s clock is set correctly.'), 'server clock is behind'),
            (ValueError('Token expired, 100 < 200'), 'token expired'),
            (ValueError('Token has wrong audience x, expected one of [b3-games]'), 'different Firebase project'),
            (ValueError('Wrong number of segments in token: ' + SECRET), 'not a sign-in token'),
            (RuntimeError('boom ' + SECRET), 'verification failed (RuntimeError)'),
        ]
        for exc, wanted in cases:
            with self.assertLogs('nigri.auth', level='WARNING') as log, patch('google.oauth2.id_token.verify_firebase_token', side_effect=exc):
                result, reason = firebase_token.verify(SECRET, 'b3-games')
            self.assertIsNone(result); self.assertIn(wanted, reason)
            text = ' '.join(log.output)
            self.assertIn(wanted, text); self.assertNotIn(SECRET, text); self.assertNotIn(SECRET, reason)

    def test_wrong_issuer_and_unverified_email_are_refused_with_a_reason(self):
        for bad, wanted in ((claims(iss='https://securetoken.google.com/other'), 'wrong issuer'), (claims(email_verified=False), 'email not verified')):
            with self.assertLogs('nigri.auth', level='WARNING') as log, patch('google.oauth2.id_token.verify_firebase_token', return_value=bad):
                result, reason = firebase_token.verify(SECRET, 'b3-games')
            self.assertIsNone(result); self.assertEqual(reason, wanted); self.assertIn(wanted, ' '.join(log.output))


class OwnerEmailTests(unittest.TestCase):
    def env(self, **kw):
        keep = {k: v for k, v in os.environ.items() if k not in ('NIGRI_OWNER_EMAIL', 'CLASSTIME_REVIEW_OWNER_EMAIL')}
        keep.update(kw)
        return patch.dict(os.environ, keep, clear=True)

    def test_default_is_the_production_owner(self):
        with self.env(): self.assertEqual(A.owner_email(), 'simcha5770@gmail.com')

    def test_new_setting_wins_and_old_one_still_works_alone(self):
        with self.env(NIGRI_OWNER_EMAIL='A@Example.test'): self.assertEqual(A.owner_email(), 'a@example.test')
        with self.env(CLASSTIME_REVIEW_OWNER_EMAIL='B@example.test'): self.assertEqual(A.owner_email(), 'b@example.test')
        with self.env(NIGRI_OWNER_EMAIL='a@example.test', CLASSTIME_REVIEW_OWNER_EMAIL='a@example.test'): self.assertEqual(A.owner_email(), 'a@example.test')

    def test_a_mismatch_between_the_two_settings_is_logged(self):
        with self.env(NIGRI_OWNER_EMAIL='a@example.test', CLASSTIME_REVIEW_OWNER_EMAIL='b@example.test'):
            with self.assertLogs('nigri.auth', level='WARNING') as log:
                self.assertEqual(A.owner_email(), 'a@example.test')
        self.assertIn('differ', ' '.join(log.output))


if __name__ == '__main__':
    unittest.main()
