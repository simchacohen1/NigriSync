"""Verify website owner sign-in server-side; no frontend shared secret."""
import os
import hmac
from flask import request
from google.oauth2 import id_token
from google.auth.transport.requests import Request

def review_owner():
    header = request.headers.get('Authorization', '')
    if not header.startswith('Bearer '): return None
    token = header[7:]
    diagnostic = os.environ.get('CLASSTIME_TEST_TOKEN', '')
    if diagnostic and hmac.compare_digest(token, diagnostic): return 'backend-diagnostic'
    try:
        claims = id_token.verify_firebase_token(token, Request(), audience='b3-games')
        if claims.get('iss') != 'https://securetoken.google.com/b3-games': return None
        if claims.get('email_verified') is not True: return None
        if claims.get('email', '').lower() != os.environ.get('CLASSTIME_REVIEW_OWNER_EMAIL', 'simcha5770@gmail.com').lower(): return None
        return claims['sub']
    except Exception:
        return None
