#!/usr/bin/env python3
import base64,secrets
print('SESSION_SECRET='+secrets.token_urlsafe(48))
print('ENCRYPTION_KEY_B64='+base64.b64encode(secrets.token_bytes(32)).decode())
print('METRICS_TOKEN='+secrets.token_urlsafe(32))
