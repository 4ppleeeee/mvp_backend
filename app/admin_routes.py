from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.admin_auth import AdminAuthenticator
from app.config import Settings


def create_admin_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/admin")
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates" / "admin"))
    authenticator = AdminAuthenticator(settings)

    def ensure_configured() -> None:
        if not authenticator.configured:
            raise HTTPException(status_code=503, detail="admin authentication is not configured")

    def is_logged_in(request: Request) -> bool:
        ensure_configured()
        try:
            authenticator.require(request)
        except HTTPException as exc:
            if exc.status_code == 401:
                return False
            raise
        return True

    @router.get("/login")
    def login_form(request: Request):
        ensure_configured()
        return templates.TemplateResponse(request, "login.html", {"error": request.query_params.get("error") == "1"})

    @router.post("/login")
    def login(request: Request, username: str = Form(), password: str = Form()):
        ensure_configured()
        if not authenticator.verify(username, password):
            return RedirectResponse("/admin/login?error=1", status_code=status.HTTP_303_SEE_OTHER)
        request.session["admin_username"] = username
        return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)

    @router.get("")
    @router.get("/")
    def dashboard(request: Request):
        if not is_logged_in(request):
            return RedirectResponse("/admin/login", status_code=status.HTTP_303_SEE_OTHER)
        return templates.TemplateResponse(request, "dashboard.html", {})

    return router
