from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.hyperliquid import fill_event_id, signed_fill_delta
from app.core.config import settings
from app.core.logging import get_logger
from app.models.entities import CopyJob, MasterEvent, TradingAccount, User, UserState

log = get_logger(__name__)


def _event_time(fill: dict[str, Any]) -> datetime:
    ts = int(fill.get('time') or int(datetime.now(UTC).timestamp() * 1000))
    return datetime.fromtimestamp(ts / 1000, UTC)


async def persist_master_fill_and_jobs(
    db: AsyncSession, *, fill: dict[str, Any], master_equity: Decimal,
    fencing_token: int, correlation_id: str, source_network: str | None = None,
    master_leverage: int | None = None, master_is_cross: bool | None = None,
) -> tuple[MasterEvent | None, list[CopyJob]]:
    lease = (await db.execute(text("SELECT fencing_token, expires_at FROM watcher_lease WHERE name='master-watcher' FOR SHARE"))).first()
    if lease is None or int(lease[0]) != fencing_token or lease[1] <= datetime.now(UTC):
        raise RuntimeError('Watcher fenced out or lease expired before master-event write')

    raw_eid = fill_event_id(fill)
    eid = f'{source_network}:{raw_eid}' if source_network else raw_eid
    existing = (await db.execute(select(MasterEvent).where(MasterEvent.exchange_event_id == eid))).scalar_one_or_none()
    if existing:
        return None, []

    asset = str(fill['coin'])
    size = Decimal(str(fill['sz']))
    price = Decimal(str(fill['px']))
    start = Decimal(str(fill.get('startPosition', '0')))
    position_after = start + signed_fill_delta(fill)
    raw = dict(fill)
    if source_network:
        raw['_hypercopy_network'] = source_network
    if master_leverage is not None:
        raw['_hypercopy_master_leverage'] = master_leverage
        raw['_hypercopy_master_is_cross'] = bool(master_is_cross)
    event = MasterEvent(
        exchange_event_id=eid, asset=asset, side=str(fill.get('side') or fill.get('dir') or ''),
        size=size, price=price, start_position=start, position_after=position_after,
        master_equity=master_equity, event_ts=_event_time(fill), raw=raw,
        fencing_token=fencing_token,
    )
    db.add(event)
    await db.flush()

    eligible = (await db.execute(
        select(User.id).join(TradingAccount, TradingAccount.user_id == User.id)
        .where(User.state == UserState.ACTIVE)
    )).scalars().all()

    jobs: list[CopyJob] = []
    for user_id in eligible:
        job_id = uuid.uuid5(uuid.UUID('8f6f61ae-7239-5e86-a501-8c8d95e94f20'), f'{event.id}:{user_id}')
        context = {
            'master_position': str(position_after),
            'master_equity': str(master_equity),
            'master_mark_price': str(price),
            'mark_price': str(price),
            'master_event_id': str(event.id),
            'master_network': source_network or settings.master_network,
            'follower_network': settings.follower_network,
        }
        if master_leverage is not None:
            context['master_leverage'] = int(master_leverage)
            context['master_is_cross'] = bool(master_is_cross)
        statement = insert(CopyJob).values(
            id=job_id, master_event_id=event.id, user_id=user_id, asset=asset,
            origin='EVENT', state='QUEUED', correlation_id=correlation_id,
            context=context,
        ).on_conflict_do_nothing(constraint='uq_job_master_user').returning(CopyJob.id)
        inserted = (await db.execute(statement)).scalar_one_or_none()
        if inserted:
            jobs.append(await db.get(CopyJob, inserted))
    await db.commit()
    return event, [j for j in jobs if j is not None]
