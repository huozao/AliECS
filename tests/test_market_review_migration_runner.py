"""Exercise the deployment runner with an isolated Docker boundary.

SQL transaction effects are separately verified against real local PostgreSQL.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('migration_exit', [0, 3, 124])
def test_atomic_migration_uses_server_deadline_and_never_retries_sql(tmp_path, migration_exit):
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    docker = binaries / 'docker'
    docker.write_text(f'#!{sys.executable}\n' + '''
import json, os, sys
args = sys.argv[1:]
if 'psql' not in args:
    sys.exit(0)
if '-Atc' in args:
    query = args[args.index('-Atc')+1]
    with open(os.environ['CALL_LOG'], 'a') as out:
        out.write(json.dumps({'query':query})+'\\n')
    if 'COUNT(*)' in query: print('1')
    elif 'SELECT CASE' in query: print('f')
    sys.exit(0)
body = sys.stdin.read()
with open(os.environ['CALL_LOG'], 'a') as out:
    out.write(json.dumps({'migration':body,'options':os.environ.get('PGOPTIONS'),
                         'args':args})+'\\n')
sys.exit(int(os.environ['MIGRATION_EXIT']))
''')
    docker.chmod(0o755)
    # Avoid delays in the old runner's retry loop while observing its behavior.
    sleep = binaries / 'sleep'
    sleep.write_text('#!/bin/sh\nexit 0\n')
    sleep.chmod(0o755)
    migrations = tmp_path / 'migrations'
    migrations.mkdir()
    (migrations / '0059_test.sql').write_text('-- migration: atomic\nBEGIN; SELECT 1; COMMIT;\n')
    runtime = tmp_path / 'runtime.env'
    runtime.touch()
    metadata = tmp_path / 'release-meta.env'
    metadata.write_text(f"COMPOSE_FILE=unused\nRUNTIME_ENV_FILE='{runtime}'\n"
                        "POSTGRES_USER=app\nPOSTGRES_PASSWORD=test-only\nPOSTGRES_DB=app\n"
                        "DATABASE_URL=postgresql://app:test-only@localhost/app\n")
    log = tmp_path / 'calls.jsonl'
    env = {**os.environ, 'PATH':str(binaries)+os.pathsep+os.environ['PATH'],
           'RELEASE_META_FILE':str(metadata),'ROLE_MIGRATIONS_DIR':str(migrations),
           'CALL_LOG':str(log),'MIGRATION_EXIT':str(migration_exit),'PSQL_TIMEOUT_SECONDS':'2'}
    result = subprocess.run(['bash', str(ROOT / 'deploy/ecs/migrate.sh')],
                            env=env, capture_output=True, text=True, timeout=15)
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    attempts = [call for call in calls if 'migration' in call]
    assert len(attempts) == 1
    assert 'statement_timeout=2000' in (attempts[0]['options'] or '')
    assert 'lock_timeout=2000' in attempts[0]['options']
    assert 'PGOPTIONS' in attempts[0]['args']
    assert '-X' in attempts[0]['args']
    registered = [call for call in calls if call.get('query','').startswith('INSERT INTO schema_migrations')]
    assert len(registered) == (1 if migration_exit == 0 else 0)
    assert (result.returncode == 0) == (migration_exit == 0)
