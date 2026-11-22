"""One place that checks a website sign-in (a Firebase ID token), shared by every Nigri route.

- A few seconds of clock tolerance, so a server clock that is slightly behind does not refuse a fresh sign-in.
- When a token is presented and refused, the exact REASON is logged (e.g. "token expired", "wrong audience").
  Never the token itself, never the exception text (some library messages contain the token).
"""
import logging

log = logging.getLogger('nigri.auth')
CLOCK_SKEW_SECONDS = 10

# Library message prefixes that are safe to turn into a reason. Anything else is logged by exception type only.
_REASONS = (
    ('Token expired', 'token expired (the page must sign in again)'),
    ('Token used too early', 'token not valid yet (server clock is behind)'),
    ('Token used too late', 'token too old'),
    ('Could not verify token signature', 'token signature not valid'),
    ('Token has wrong audience', 'token is for a different Firebase project'),
    ('Wrong number of segments', 'value is not a sign-in token'),
    ('Can\'t parse segment', 'value is not a sign-in token'),
    ('Certificate for key id', 'signing certificate not found'),
    ('Unable to fetch', 'could not download Google signing certificates'),
)


def reason_for(exc):
    message = str(exc)
    for prefix, label in _REASONS:
        if message.startswith(prefix):
            return label
    return 'verification failed (' + type(exc).__name__ + ')'


def verify(token, project_id):
    """Return (claims, None) for a valid, verified-email token of this project, else (None, reason)."""
    try:
        from google.oauth2 import id_token
        from google.auth.transport.requests import Request
        claims = id_token.verify_firebase_token(token, Request(), audience=project_id, clock_skew_in_seconds=CLOCK_SKEW_SECONDS)
    except Exception as exc:
        reason = reason_for(exc)
        log.warning('Sign-in token refused: %s', reason)
        return None, reason
    if not claims:
        log.warning('Sign-in token refused: no claims')
        return None, 'no claims'
    if claims.get('iss') != 'https://securetoken.google.com/' + project_id:
        log.warning('Sign-in token refused: wrong issuer')
        return None, 'wrong issuer'
    if claims.get('email_verified') is not True:
        log.warning('Sign-in token refused: email not verified')
        return None, 'email not verified'
    return claims, None
