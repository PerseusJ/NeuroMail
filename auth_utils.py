import os
import msal
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import base64
import requests

# --- CONFIG ---
# Ensure these are set in your environment variables (e.g. on Render)
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8501") 

MICROSOFT_CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID")
MICROSOFT_CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET")
MICROSOFT_TENANT_ID = os.getenv("MICROSOFT_TENANT_ID", "common")
MICROSOFT_REDIRECT_URI = os.getenv("MICROSOFT_REDIRECT_URI", "http://localhost:8501")

# Scopes
GOOGLE_SCOPES = [
    'https://mail.google.com/',
    'https://www.googleapis.com/auth/userinfo.email',
    'openid'
]
MICROSOFT_SCOPES = ['https://outlook.office.com/IMAP.AccessAsUser.All', 'User.Read', 'email', 'offline_access']

# --- GOOGLE AUTH ---
def get_google_auth_url():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return None
    
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=GOOGLE_SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI
    )
    auth_url, _ = flow.authorization_url(prompt='consent', state='google', access_type='offline')
    return auth_url

def get_google_token_from_code(code):
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=GOOGLE_SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    
    # Get User Info
    session = flow.authorized_session()
    user_info = session.get('https://www.googleapis.com/userinfo/v2/me').json()
    email = user_info.get('email')

    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes,
        "email": email,
        "provider": "google"
    }

def refresh_google_token(token_info):
    creds = Credentials(
        token_info['token'],
        refresh_token=token_info.get('refresh_token'),
        token_uri=token_info.get('token_uri'),
        client_id=token_info.get('client_id'),
        client_secret=token_info.get('client_secret'),
        scopes=token_info.get('scopes')
    )
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_info['token'] = creds.token
        except Exception as e:
            print(f"Error refreshing Google token: {e}")
            return None
    return token_info

# --- MICROSOFT AUTH ---
def _get_msal_app():
    if not MICROSOFT_CLIENT_ID or not MICROSOFT_CLIENT_SECRET:
        return None
    return msal.ConfidentialClientApplication(
        MICROSOFT_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{MICROSOFT_TENANT_ID}",
        client_credential=MICROSOFT_CLIENT_SECRET,
    )

def get_microsoft_auth_url():
    app = _get_msal_app()
    if not app: return None
    return app.get_authorization_request_url(
        MICROSOFT_SCOPES,
        redirect_uri=MICROSOFT_REDIRECT_URI,
        state='microsoft'
    )

def get_microsoft_token_from_code(code):
    app = _get_msal_app()
    result = app.acquire_token_by_authorization_code(
        code,
        scopes=MICROSOFT_SCOPES,
        redirect_uri=MICROSOFT_REDIRECT_URI
    )
    if "error" in result:
        raise Exception(result.get("error_description", str(result)))
    
    # Extract Email
    claims = result.get("id_token_claims", {})
    email = claims.get("email") or claims.get("preferred_username")
    
    result['email'] = email
    result['provider'] = 'microsoft'
    return result

def refresh_microsoft_token(token_info):
    app = _get_msal_app()
    if 'refresh_token' in token_info:
            result = app.acquire_token_by_refresh_token(
                token_info['refresh_token'],
                scopes=MICROSOFT_SCOPES
            )
            if "error" in result:
                print(f"Error refreshing Microsoft token: {result}")
                return None
            # Update access token
            token_info['access_token'] = result['access_token']
            # Update refresh token if a new one is returned
            if 'refresh_token' in result:
                token_info['refresh_token'] = result['refresh_token']
            return token_info
    return None

# --- XOAUTH2 GENERATOR ---
def generate_oauth2_string(user, token, base64_encode=True):
    auth_string = f"user={user}\x01auth=Bearer {token}\x01\x01"
    if base64_encode:
        # Standard base64 encoding without newlines
        return base64.b64encode(auth_string.encode("utf-8")).decode("utf-8")
    return auth_string
