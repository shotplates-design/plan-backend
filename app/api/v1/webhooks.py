from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool

from app.billing.webhooks import process_webhook
from app.core.errors import ErrorResponse
from app.schemas.billing import WebhookAck

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post(
    "/stripe",
    response_model=WebhookAck,
    responses={
        400: {"model": ErrorResponse, "description": "INVALID_SIGNATURE"},
        500: {"model": ErrorResponse, "description": "WEBHOOK_FAILED (Stripe retries)"},
        503: {"model": ErrorResponse, "description": "STRIPE_NOT_CONFIGURED"},
    },
)
async def stripe_webhook(request: Request) -> WebhookAck:
    """Stripe webhook receiver. No JWT; the `Stripe-Signature` header is mandatory.

    Idempotent by event id; always re-fetches the subscription so delivery order doesn't matter.
    """
    payload = await request.body()  # raw bytes — verify before any JSON parsing
    result = await run_in_threadpool(process_webhook, payload, request.headers.get("stripe-signature"))
    return WebhookAck(**result)
