"""Verify website owner sign-in server-side; no frontend shared secret."""
import os
import hmac
from flask import request
import firebase_token

def review_owner(allow_diagnostic=True):
    header = request.headers.get('Authorization', '')
    if not header.startswith('Bearer '): return None
    token = header[7:]
    diagnostic = os.environ.get('CLASSTIME_TEST_TOKEN', '')
    if allow_diagnostic and diagnostic and hmac.compare_digest(token, diagnostic): return 'backend-diagnostic'
    claims, reason = firebase_token.verify(token, 'b3-games')
    if not claims: return None
    from nigri_school_access import owner_email
    if str(claims.get('email', '')).strip().lower() != owner_email():
        firebase_token.log.warning('Sign-in token refused: signed in, but not as the owner email setting')
        return None
    return claims['sub']
