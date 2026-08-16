from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.config import settings
from app.core.security import decode_session_token
from app.db.redis import redis_client
from app.db.session import SessionLocal
from app.models.entities import Role, User

router=APIRouter(tags=['realtime'])


@router.websocket('/ws/events')
async def events(ws: WebSocket):
    token=ws.cookies.get(settings.SESSION_COOKIE_NAME)
    if not token:
        await ws.close(code=4401); return
    try: claims=decode_session_token(token)
    except Exception: await ws.close(code=4401); return
    async with SessionLocal() as db:
        user=await db.get(User,uuid.UUID(claims['sub']))
        if not user: await ws.close(code=4401); return
        role=user.role
    await ws.accept()
    pubsub=redis_client().pubsub()
    channels=[f'{settings.REALTIME_CHANNEL_PREFIX}:user:{claims["sub"]}']
    if role in {Role.ADMIN,Role.SUPERADMIN}: channels.append(f'{settings.REALTIME_CHANNEL_PREFIX}:system')
    await pubsub.subscribe(*channels)
    try:
        while True:
            message=await pubsub.get_message(ignore_subscribe_messages=True,timeout=15)
            if message: await ws.send_text(message['data'])
            else: await ws.send_json({'type':'heartbeat'})
            await asyncio.sleep(0.05)
    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.unsubscribe(*channels); await pubsub.close()
