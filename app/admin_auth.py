import hmac

from fastapi import HTTPException, Request
from pwdlib import PasswordHash

from app.config import Settings


class AdminAuthenticator:
    def __init__(self, settings: Settings) -> None:
        self._username = settings.admin_username
        self._password_hash = settings.admin_password_hash
        self._configured = bool(self._username and self._password_hash and settings.admin_session_secret)
        self._hasher = PasswordHash.recommended()

    @property
    def configured(self) -> bool:
        return self._configured

    def verify(self, username: str, password: str) -> bool:
        if not self._configured:
            return False
        return hmac.compare_digest(username, self._username or "") and self._hasher.verify(password, self._password_hash or "")

    def require(self, request: Request) -> str:
        if not self._configured:
            raise HTTPException(status_code=503, detail="admin authentication is not configured")
        username = request.session.get("admin_username")
        if not isinstance(username, str) or not hmac.compare_digest(username, self._username or ""):
            raise HTTPException(status_code=401, detail="admin login required")
        return username
