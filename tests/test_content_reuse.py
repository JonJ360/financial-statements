"""Immutable reuse contract and isolated real PostgreSQL (PGlite) acceptance."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / 'supabase/migrations/202610310001_financial_period_immutable_content_reuse.sql'


def test_content_reuse_preserves_existing_api_and_least_privilege():
    sql = MIGRATION.read_text()
    assert 'create table public.financial_period_content' in sql
    assert 'create table public.financial_period_refs' in sql
    assert 'union all' in sql and 'select * from public.financial_periods' in sql
    assert sql.count('force row level security') == 2
    assert sql.count('revoke all on public.financial_period') == 3
    assert 'grant ' not in sql.lower()
    assert 'delete from' not in sql.lower() and 'truncate ' not in sql.lower()
    assert 'content hash collision or payload mismatch' in sql
    assert 'payload_canonical' in sql and 'extensions.digest' in sql
    assert 'financial_promote_run(' not in sql and 'financial_rollback_current(' not in sql
    assert 'join public.financial_current c on p.run_id=c.current_run_id' in sql


def test_immutable_reuse_postgres_acceptance():
    package = os.environ.get('PGLITE_PACKAGE')
    if not package:
        known = Path('C:/Users/jonj/OneDrive/Hermes/Documents/AR Storage Growth Review/historyreuse/node_modules/@electric-sql/pglite')
        package = str(known) if known.is_dir() else None
    if not package or not shutil.which('node'):
        pytest.skip('Requires existing PGLITE_PACKAGE and Node; no installation is performed')
    env = {**os.environ, 'PGLITE_PACKAGE': package, 'FINANCIAL_REPO': str(ROOT)}
    result = subprocess.run(['node', str(ROOT / 'tests/financial_content_reuse.mjs')], env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr[-5000:]
    receipt = json.loads(result.stdout.strip().splitlines()[-1])
    assert receipt == {'passed': True, 'periods': 1836, 'reused': 1831, 'new_payloads': 5, 'retry': True, 'full_payload_equality': True, 'rollback': True, 'incomplete_rejected': True, 'role_denials': True, 'immutable': True, 'coordinate_integrity': True, 'atomic_failure': True}
