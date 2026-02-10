"""
OIDC Authentication for MeshBOT WebGUI
Generic OpenID Connect client — works with any OIDC provider.
"""

import os
import secrets
from datetime import datetime, timedelta
from typing import Optional, Dict

from authlib.integrations.starlette_client import OAuth
from starlette.requests import Request
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

# OIDC Configuration from environment
OIDC_DISCOVERY_URL = os.environ.get("OIDC_DISCOVERY_URL", "")  # e.g. https://auth.example.com/.well-known/openid-configuration
OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")
OIDC_SCOPES = os.environ.get("OIDC_SCOPES", "openid profile email")
OIDC_SESSION_SECRET = os.environ.get("OIDC_SESSION_SECRET", "") or secrets.token_hex(32)
OIDC_SESSION_MAX_AGE = int(os.environ.get("OIDC_SESSION_MAX_AGE", "28800"))  # 8 hours default
OIDC_REDIRECT_URI = os.environ.get("OIDC_REDIRECT_URI", "")  # e.g. https://meshbot.example.com/auth/callback — auto-detected if empty
OIDC_POST_LOGOUT_REDIRECT = os.environ.get("OIDC_POST_LOGOUT_REDIRECT", "/")

OIDC_ENABLED = bool(OIDC_DISCOVERY_URL and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET)

# Session cookie config
SESSION_COOKIE_NAME = "meshbot_session"
SESSION_COOKIE_SECURE = os.environ.get("OIDC_COOKIE_SECURE", "true").lower() == "true"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "lax"

# Session serializer
_serializer = URLSafeTimedSerializer(OIDC_SESSION_SECRET)

# In-memory session store (maps session_id -> user info)
# For single-instance deployment this is fine. Restarts invalidate sessions (users re-login).
_sessions: Dict[str, Dict] = {}

# OAuth client
oauth = OAuth()

if OIDC_ENABLED:
    oauth.register(
        name="oidc",
        client_id=OIDC_CLIENT_ID,
        client_secret=OIDC_CLIENT_SECRET,
        server_metadata_url=OIDC_DISCOVERY_URL,
        client_kwargs={"scope": OIDC_SCOPES},
    )


def create_session(user_info: dict) -> str:
    """Create a new session and return signed session ID."""
    session_id = secrets.token_hex(32)
    _sessions[session_id] = {
        "user": user_info,
        "created": datetime.now().isoformat(),
    }
    return _serializer.dumps(session_id)


def get_session(signed_session_id: str) -> Optional[Dict]:
    """Validate and return session data, or None if invalid/expired."""
    try:
        session_id = _serializer.loads(signed_session_id, max_age=OIDC_SESSION_MAX_AGE)
        return _sessions.get(session_id)
    except (BadSignature, SignatureExpired):
        return None


def destroy_session(signed_session_id: str) -> None:
    """Remove a session."""
    try:
        session_id = _serializer.loads(signed_session_id, max_age=OIDC_SESSION_MAX_AGE)
        _sessions.pop(session_id, None)
    except (BadSignature, SignatureExpired):
        pass


def cleanup_expired_sessions() -> int:
    """Remove expired sessions. Returns count removed."""
    cutoff = datetime.now() - timedelta(seconds=OIDC_SESSION_MAX_AGE)
    expired = [
        sid for sid, data in _sessions.items()
        if datetime.fromisoformat(data["created"]) < cutoff
    ]
    for sid in expired:
        del _sessions[sid]
    return len(expired)


def get_user_from_request(request: Request) -> Optional[Dict]:
    """Extract user info from session cookie. Returns None if not authenticated."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        return None
    session = get_session(cookie)
    if not session:
        return None
    return session.get("user")
