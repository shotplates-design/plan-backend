from fastapi import APIRouter

from app.api.v1 import auth, bets, billing, games, health, users, wallet, webhooks

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(games.router)
api_router.include_router(bets.router)
api_router.include_router(wallet.router)
api_router.include_router(billing.router)
api_router.include_router(webhooks.router)
