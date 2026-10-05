"""Integration tests use ONLY a disposable local database named flightwatch_test.

FLIGHTWATCH_TEST_DSN is optional; without it these tests are explicitly skipped.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import uuid

import pytest

from flightwatch.core import snapshot, stable_key
from flightwatch.providers import normalize

DSN = os.environ.get('FLIGHTWATCH_TEST_DSN')
pytestmark = pytest.mark.skipif(not DSN, reason='disposable local PostgreSQL not configured')


@pytest.fixture(scope='module')
def db():
    import psycopg
    conn = psycopg.connect(DSN, autocommit=True)
    assert conn.info.dbname == 'flightwatch_test' and conn.info.host in {'127.0.0.1','localhost','/tmp'}
    # This test is intentionally not rerunnable against a populated database.
    assert conn.execute("select to_regclass('public.flightwatch_searches')").fetchone()[0] is None
    for role in ['anon','authenticated','service_role']:
        if not conn.execute('select 1 from pg_roles where rolname=%s',(role,)).fetchone():
            conn.execute(f'create role {role}')
    conn.execute(Path('migrations/001_flightwatch.sql').read_text())
    yield conn
    conn.close()


def payload(cfg,results=None):
    rows = normalize(results,'VIE','2027-02-18',cfg)[0] if results else []
    return snapshot(cfg,str(uuid.uuid4()),'VIE','2027-02-18',
                    'success' if rows else 'no_offers',offers=rows,complete=True)


def ingest(conn,p,db_budget=450000000,scraper_budget=150000000):
    from psycopg.types.json import Jsonb
    return conn.execute('select flightwatch_ingest(%s,%s,%s,%s)',
                        (Jsonb(p),stable_key(p),scraper_budget,db_budget)).fetchone()[0]


def test_sql_idempotency_history_and_views(db,cfg,results):
    p=payload(cfg,results)
    assert ingest(db,p)['duplicate'] is False
    assert ingest(db,p)['duplicate'] is True
    assert db.execute('select count(*) from flightwatch_observations where snapshot_id=%s',
                      (p['snapshot_id'],)).fetchone()[0] == 1
    changed=payload(cfg,results)
    changed['offers'][0]['price']='88.88'
    ingest(db,changed)
    assert db.execute('select count(*) from flightwatch_price_history where fare_key=%s',
                      (p['offers'][0]['fare_key'],)).fetchone()[0] == 2
    assert db.execute('select count(*) from flightwatch_current_eligible').fetchone()[0] == 1
    absent=payload(cfg)
    ingest(db,absent)
    assert db.execute('select count(*) from flightwatch_current_eligible').fetchone()[0] == 0
    assert db.execute('select count(*) from flightwatch_price_history').fetchone()[0] == 2


def test_sql_partial_failure_rolls_back(db,cfg,results):
    import psycopg
    p=payload(cfg,results)
    bad=dict(p['offers'][0],observation_key='another-observation',arrival_at=None)
    p['offers'].append(bad)
    with pytest.raises(psycopg.Error): ingest(db,p)
    assert db.execute('select count(*) from flightwatch_searches where snapshot_id=%s',
                      (p['snapshot_id'],)).fetchone()[0] == 0
    assert db.execute('select count(*) from flightwatch_observations where snapshot_id=%s',
                      (p['snapshot_id'],)).fetchone()[0] == 0


def test_sql_budget_and_digest_conflict(db,cfg):
    import psycopg
    p=payload(cfg)
    with pytest.raises(psycopg.Error,match='storage_budget_exceeded'):
        ingest(db,p,db_budget=1)
    assert db.execute('select count(*) from flightwatch_searches where snapshot_id=%s',
                      (p['snapshot_id'],)).fetchone()[0] == 0
    ingest(db,p)
    p['metadata']['changed']=True
    with pytest.raises(psycopg.Error,match='snapshot_digest_conflict'): ingest(db,p)


def test_sql_concurrent_retry(db,cfg):
    import psycopg
    p=payload(cfg)
    def deliver(_):
        with psycopg.connect(DSN,autocommit=True) as c: return ingest(c,p)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies=list(pool.map(deliver,range(2)))
    assert sorted(r['duplicate'] for r in replies)==[False,True]


def test_rpc_permissions(db,cfg):
    import psycopg
    db.execute('set role anon')
    try:
        with pytest.raises(psycopg.Error): db.execute('select flightwatch_sizes()')
        with pytest.raises(psycopg.Error): db.execute('select * from flightwatch_current_eligible')
    finally:
        db.execute('reset role')
    db.execute('set role service_role')
    try:
        assert ingest(db,payload(cfg))['accepted']
        sizes=db.execute('select flightwatch_sizes()').fetchone()[0]
        assert sizes['database_bytes'] > sizes['scraper_bytes'] > 0
    finally:
        db.execute('reset role')
