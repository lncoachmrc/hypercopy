#!/usr/bin/env python3
"""Targeted local/CI release-structure preflight.

This is deliberately *not* a general-purpose secret scanner. It verifies a
small set of release-critical files, the registered Alembic migration baseline,
and a narrow set of obvious secret-assignment mistakes. GitHub Actions Gitleaks
with full history is the authoritative repository secret-scanning gate.
"""

from pathlib import Path
import re
import sys

root = Path(__file__).resolve().parents[1]

REQUIRED_FILES = [
    'README.md',
    'DEPLOYMENT.md',
    'SECURITY.md',
    'RUNBOOK.md',
    'ROADMAP.md',
    'DEFINITION_OF_DONE.md',
    'SPEC.md',
    '.github/workflows/ci.yml',
    '.github/workflows/codeql.yml',
    'backend/railway.toml',
    'backend/railway.watcher.toml',
    'backend/railway.worker.toml',
    'backend/railway.ai.toml',
    'frontend/railway.toml',
    'scripts/targeted_release_preflight.py',
]

EXPECTED_MIGRATIONS = {
    '0001_initial.py',
    '0002_seed.py',
    '0003_operational_risk.py',
    '0004_ledger_marks.py',
    '0005_position_leverage.py',
    '0006_pricing_plans.py',
    '0007_trial_limits.py',
    '0008_equity_breakdown.py',
    '0009_user_execution_network.py',
    '0010_user_plan_discounts.py',
}

missing = [path for path in REQUIRED_FILES if not (root / path).exists()]
migration_dir = root / 'backend/alembic/versions'
actual_migrations = {
    path.name
    for path in migration_dir.glob('[0-9][0-9][0-9][0-9]_*.py')
    if path.is_file()
}
missing_migrations = sorted(EXPECTED_MIGRATIONS - actual_migrations)
unregistered_migrations = sorted(actual_migrations - EXPECTED_MIGRATIONS)

# Deliberately line-bound and intentionally narrow. This heuristic catches only
# a few obvious assignment forms; Gitleaks full-history in CI is authoritative.
secret_assignment = re.compile(
    r'(?im)(private[_ -]?key|stripe_secret(?:_key)?|session_secret)'
    r'[ \t]*=[ \t]*["\']?([A-Za-z0-9_/+=-]{20,})'
)
allowed_prefixes = ('development', 'CHANGE', 'ci-only')
hits: list[str] = []
for path in root.rglob('*'):
    if not path.is_file() or any(
        part in {'.git', 'node_modules', 'dist', '.pytest_cache', '__pycache__'}
        for part in path.parts
    ):
        continue
    if path.suffix.lower() not in {
        '.py', '.md', '.toml', '.yml', '.yaml', '.env', '.example',
        '.json', '.ts', '.tsx', '.js',
    }:
        continue
    try:
        text = path.read_text(errors='ignore')
    except Exception:
        continue
    for match in secret_assignment.finditer(text):
        value = match.group(2)
        if not value.startswith(allowed_prefixes):
            hits.append(str(path.relative_to(root)))
            break

if missing or missing_migrations or unregistered_migrations or hits:
    print(
        'missing=', missing,
        'missing_migrations=', missing_migrations,
        'unregistered_migrations=', unregistered_migrations,
        'targeted_secret_like=', hits,
    )
    sys.exit(1)

print('targeted release preflight: OK (authoritative secret scan: CI Gitleaks full history)')
