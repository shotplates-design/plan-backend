from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from app.api.deps import DB
from app.config import get_settings


class HealthOut(BaseModel):
    status: str
    database: str
    environment: str
    version: str


router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
def health(db: DB) -> HealthOut:
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:  # pragma: no cover - surfaced to the caller
        db_status = "error"
    return HealthOut(
        status="ok" if db_status == "ok" else "degraded",
        database=db_status,
        environment=get_settings().environment,
        version="0.1.0",
    )
