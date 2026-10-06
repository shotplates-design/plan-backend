import uuid
from datetime import datetime

from app.models.enums import WalletTxType
from app.schemas.common import APIModel


class WalletOut(APIModel):
    id: uuid.UUID
    currency: str
    balance_cents: int
    is_play_money: bool = True
    updated_at: datetime


class WalletTransactionOut(APIModel):
    id: uuid.UUID
    type: WalletTxType
    amount_cents: int
    balance_after_cents: int
    bet_id: uuid.UUID | None
    description: str | None
    created_at: datetime
