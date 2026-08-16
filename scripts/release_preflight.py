#!/usr/bin/env python3
from pathlib import Path
import re
import sys

root = Path(__file__).resolve().parents[1]
required = [
    'README.md', 'DEPLOYMENT.md', 'SECURITY.md', 'RUNBOOK.md', 'ROADMAP.md',
    'DEFINITION_OF_DONE.md', 'SPEC.md', '.github/workflows/ci.yml',
    'backend/railway.toml', 'backend/railway.watcher.toml',
    'backend/railway.worker.toml', 'frontend/railway.toml',
    'backend/alembic/versions/0001_initial.py',
]
missing = [x for x in required if not (root / x).exists()]

# Deliberately line-bound: empty values in .env.example must not consume text
# from the next line and become false positives.
secret_assignment = re.compile(
    r'(?im)(private[_ -]?key|stripe_secret(?:_key)?|session_secret)'
    r'[ \t]*=[ \t]*["\']?([A-Za-z0-9_/+=-]{20,})'
)
allowed_prefixes = ('development', 'CHANGE', 'ci-only')
hits: list[str] = []
for p in root.rglob('*'):
    if not p.is_file() or any(x in p.parts for x in {'.git', 'node_modules', 'dist', '.pytest_cache', '__pycache__'}):
        continue
    if p.suffix.lower() not in {'.py', '.md', '.toml', '.yml', '.yaml', '.env', '.example', '.json', '.ts', '.tsx', '.js'}:
        continue
    try:
        text = p.read_text(errors='ignore')
    except Exception:
        continue
    for match in secret_assignment.finditer(text):
        value = match.group(2)
        if not value.startswith(allowed_prefixes):
            hits.append(str(p.relative_to(root)))
            break

if missing or hits:
    print('missing=', missing, 'secret-like=', hits)
    sys.exit(1)
print('preflight: OK')
