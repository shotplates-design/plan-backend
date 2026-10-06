from fastapi import APIRouter

from app.api.deps import DB, CurrentUser
from app.core.errors import ERROR_RESPONSES
from app.schemas.sports import PreferencesOut, PreferencesUpdate

router = APIRouter(prefix="/users/me", tags=["users"], responses=ERROR_RESPONSES)


@router.get("/preferences", response_model=PreferencesOut)
def get_preferences(user: CurrentUser) -> PreferencesOut:
    return PreferencesOut(theme=user.theme)


@router.patch("/preferences", response_model=PreferencesOut)
def update_preferences(body: PreferencesUpdate, user: CurrentUser, db: DB) -> PreferencesOut:
    """Set sportsbook look-and-feel: `fanduel` | `draftkings`."""
    user.theme = body.theme
    db.commit()
    return PreferencesOut(theme=user.theme)
