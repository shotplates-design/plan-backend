import uuid
from datetime import datetime

from pydantic import EmailStr, Field, field_validator

from app.models.enums import Theme
from app.schemas.common import APIModel


class UserOut(APIModel):
    id: uuid.UUID
    email: EmailStr
    display_name: str | None
    theme: Theme | None = Field(description="fanduel | draftkings | null (show skin picker when null)")
    created_at: datetime


class SignupRequest(APIModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=80)
    theme: Theme | None = Field(default=None, description="Optional initial sportsbook theme")

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()


class LoginRequest(APIModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()


class AuthResponse(APIModel):
    user: UserOut
    access_token: str = Field(description="JWT. Also set as an httpOnly cookie.")
    token_type: str = "bearer"
    expires_at: datetime


class MeOut(UserOut):
    """`/auth/me`: user + paywall usage (additive to UserOut)."""

    usage: "UsageOut"


from app.schemas.billing import UsageOut  # noqa: E402

MeOut.model_rebuild()
