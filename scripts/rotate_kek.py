#!/usr/bin/env python3
"""Rewrap encrypted DEKs from one AWS KMS key to another without decrypting user keys."""
import argparse,asyncio,base64,boto3
from sqlalchemy import select
from app.db.session import SessionLocal
from app.models.entities import SigningCredential
async def run(new_key,region):
    kms=boto3.client('kms',region_name=region or None)
    async with SessionLocal() as db:
        rows=(await db.execute(select(SigningCredential).where(SigningCredential.key_provider=='aws_kms'))).scalars().all()
        for c in rows:
            out=kms.re_encrypt(CiphertextBlob=base64.b64decode(c.wrapped_dek_b64),DestinationKeyId=new_key)
            c.wrapped_dek_b64=base64.b64encode(out['CiphertextBlob']).decode();c.key_reference=new_key;c.key_version+=1
        await db.commit();print(f'rewrapped={len(rows)}')
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--new-key',required=True);p.add_argument('--region',default='');a=p.parse_args();asyncio.run(run(a.new_key,a.region))
