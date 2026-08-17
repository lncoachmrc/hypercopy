from fastapi import APIRouter, Depends

from app.api import activation, admin, analytics, auth, billing, intelligence, leverage, user, ws
from app.api.deps import api_rate_limit

api_router = APIRouter(prefix='/api/v1')
http_router = APIRouter(dependencies=[Depends(api_rate_limit)])
http_router.include_router(auth.router)
# The activation router must precede user.router because it intentionally
# overrides the legacy /copy/resume handler during TESTNET validation.
http_router.include_router(activation.router)
http_router.include_router(user.router)
http_router.include_router(leverage.router)
http_router.include_router(intelligence.router)
http_router.include_router(analytics.router)
http_router.include_router(billing.router)
http_router.include_router(admin.router)
api_router.include_router(http_router)
# WebSocket dependencies use the WebSocket object rather than Request, so keep
# the HTTP fixed-window dependency off this router. WS authentication/rate
# control is handled in api/ws.py.
api_router.include_router(ws.router)
