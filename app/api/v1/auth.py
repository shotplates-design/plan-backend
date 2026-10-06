from fastapi import APIRouter, Request, Response, status

from app.api.deps import DB, CurrentUser
from app.billing.paywall import resolve_access
from app.billing.views import usage_out
from app.config import get_settings
from app.core.errors import ERROR_RESPONSES
from app.schemas.user import AuthResponse, LoginRequest, MeOut, SignupRequest, UserOut
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"], responses=ERROR_RESPONSES)


def _issue(request: Request, response: Response, db: DB, user) -> AuthResponse:
    token, expires = auth_service.start_session(
        db,
        user,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    s = get_settings()
    response.set_cookie(
        s.auth_cookie_name,
        token,
        httponly=True,
        secure=s.auth_cookie_secure,
        samesite=s.auth_cookie_samesite,
        expires=expires,
        path="/",
    )
    return AuthResponse(user=UserOut.model_validate(user), access_token=token, expires_at=expires)


@router.post("/signup", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def signup(body: SignupRequest, request: Request, response: Response, db: DB) -> AuthResponse:
    """Create an account (with a play-money wallet + welcome credits) and log in."""
    user = auth_service.signup(db, body.email, body.password, body.display_name, body.theme)
    return _issue(request, response, db, user)


@router.post("/login", response_model=AuthResponse)
def login(body: LoginRequest, request: Request, response: Response, db: DB) -> AuthResponse:
    user = auth_service.authenticate(db, body.email, body.password)
    return _issue(request, response, db, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, user: CurrentUser, db: DB) -> Response:
    """Revoke the current session and clear the cookie."""
    auth_service.revoke_session(db, request.state.session_id)
    resp = Response(status_code=status.HTTP_204_NO_CONTENT)
    resp.delete_cookie(get_settings().auth_cookie_name, path="/")
    return resp


@router.get("/me", response_model=MeOut)
def me(user: CurrentUser, db: DB) -> MeOut:
    """Current user plus paywall `usage` (UI hints; the server enforces gating)."""
    base = UserOut.model_validate(user).model_dump()
    return MeOut(**base, usage=usage_out(db, resolve_access(db, user)))
