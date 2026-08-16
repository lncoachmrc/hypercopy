#!/usr/bin/env python3
import asyncio,json
from sqlalchemy import func,select
from app.db.session import SessionLocal
from app.models.entities import CopyJob,JobState
async def main():
    async with SessionLocal() as db:
        rows=(await db.execute(select(CopyJob.state,func.count()).group_by(CopyJob.state))).all()
        print(json.dumps({str(s.value if hasattr(s,'value') else s):n for s,n in rows},indent=2))
if __name__=='__main__': asyncio.run(main())
