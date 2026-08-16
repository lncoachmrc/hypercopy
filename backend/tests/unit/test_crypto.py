import base64
import os

import pytest

from app.core.config import settings
from app.core.crypto import EnvelopeCrypto


def _local_crypto():
    settings.KEK_PROVIDER='env'
    settings.ENCRYPTION_KEY_B64=base64.b64encode(os.urandom(32)).decode()
    return EnvelopeCrypto()


def test_envelope_roundtrip_and_randomization():
    c=_local_crypto()
    a=c.encrypt('0x'+'11'*32,user_id='u1',account_id='a1')
    b=c.encrypt('0x'+'11'*32,user_id='u1',account_id='a1')
    assert a.ciphertext_b64 != b.ciphertext_b64
    assert c.decrypt(a,user_id='u1',account_id='a1') == '0x'+'11'*32


def test_aad_binds_ciphertext_to_record():
    c=_local_crypto(); blob=c.encrypt('secret',user_id='u1',account_id='a1')
    with pytest.raises(Exception):
        c.decrypt(blob,user_id='u2',account_id='a1')
