from fastapi import APIRouter, Depends

from app.api import activation, admin, admin_health, ai, analytics, auth, billing, leverage, public, user, ws
from app.api.deps import api_rate_limit

api_router = APIRouter(prefix='/api/v1')
http_router = APIRouter(dependencies=[Depends(api_rate_limit)])
http_router.include_router(auth.router)
# The activation router precedes user.router because it is the hardened
# implementation of /copy/resume: it preflights the selected user network,
# reconciles before execution and preserves the independent MAINNET gates.
http_router.include_router(activation.router)
http_router.include_router(user.router)
http_router.include_router(leverage.router)
http_router.include_router(analytics.router)
http_router.include_router(ai.router)
http_router.include_router(billing.router)
http_router.include_router(admin.router)
http_router.include_router(admin_health.router)
http_router.include_router(public.router)
api_router.include_router(http_router)
# WebSocket dependencies use the WebSocket object rather than Request, so keep
# the HTTP fixed-window dependency off this router. WS authentication/rate
# control is handled in api/ws.py.
api_router.include_router(ws.router)
