#!/usr/bin/env python3
import asyncio
from app.db.redis import redis_client
from app.db.session import SessionLocal
from app.services.queue import ensure_group,repair_stream
async def main():
    r=redis_client();await ensure_group(r)
    total=0
    while True:
        async with SessionLocal() as db: n=await repair_stream(r,db,1000)
        total+=n
        if n==0: break
    print(f'republished={total}')
if __name__=='__main__': asyncio.run(main())
