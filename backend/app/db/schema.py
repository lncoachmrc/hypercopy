from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

EXPECTED_REVISION='0010_user_plan_discounts'


async def assert_schema(db:AsyncSession)->None:
    current=(await db.execute(text('SELECT version_num FROM alembic_version LIMIT 1'))).scalar_one_or_none()
    if current!=EXPECTED_REVISION:
        raise RuntimeError(f'Database schema {current!r} != expected {EXPECTED_REVISION!r}')
