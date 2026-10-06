from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.deps import DB, CurrentUser
from app.core.errors import ERROR_RESPONSES
from app.models import WalletTransaction
from app.schemas.common import Page
from app.schemas.wallet import WalletOut, WalletTransactionOut
from app.services.wallet import get_wallet

router = APIRouter(prefix="/wallet", tags=["wallet"], responses=ERROR_RESPONSES)


@router.get("", response_model=WalletOut)
def wallet_balance(user: CurrentUser, db: DB) -> WalletOut:
    """Play-money balance (integer cents). No real-money deposits/withdrawals exist."""
    return WalletOut.model_validate(get_wallet(db, user.id))


@router.get("/transactions", response_model=Page[WalletTransactionOut])
def wallet_transactions(
    user: CurrentUser,
    db: DB,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[WalletTransactionOut]:
    wallet = get_wallet(db, user.id)
    base = select(WalletTransaction).where(WalletTransaction.wallet_id == wallet.id)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.scalars(
        base.order_by(WalletTransaction.created_at.desc(), WalletTransaction.id).limit(limit).offset(offset)
    ).all()
    return Page[WalletTransactionOut](
        items=[WalletTransactionOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
