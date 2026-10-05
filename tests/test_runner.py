import argparse
import asyncio
import json
from unittest.mock import patch
from flightwatch.cli import run
from flightwatch.providers import SearchFailure


def args(tmp_path):
    return argparse.Namespace(state=str(tmp_path),no_upload=True,command='run',test=False,
                              origin='VIE',date='2027-02-18',provider='website')


def test_circuit_breaker_and_slot_idempotency(cfg,tmp_path):
    from flightwatch.core import aware
    cfg['campaign_start']='2027-01-20T00:00:00Z'
    calls=[]
    async def blocked(self,origin,day,cfg):
        calls.append(1)
        raise SearchFailure('website_access_blocked',True)
    with patch('flightwatch.cli.utcnow',return_value=aware('2027-01-20T00:00:00Z')), \
         patch('flightwatch.cli.WebsiteProvider.search',blocked), \
         patch.dict('os.environ',{'CAMPAIGN_START':''}):
        assert asyncio.run(run(args(tmp_path),cfg))==1
        assert len(calls)==1
        files=list((tmp_path/'pending').glob('*.json'))
        assert len(files)==90
        skipped=[json.loads(p.read_text()) for p in files
                 if json.loads(p.read_text())['error_code']=='skipped_after_access_block']
        assert len(skipped)==89 and all(p['metadata']['attempted'] is False for p in skipped)
        asyncio.run(run(args(tmp_path),cfg))
        assert len(calls)==1 and len(list((tmp_path/'pending').glob('*.json')))==90


def test_crash_recovery_between_enqueue_and_ledger(cfg,tmp_path):
    from flightwatch.core import aware
    cfg['campaign_start']='2027-01-20T00:00:00Z'
    async def blocked(self,origin,day,cfg): raise SearchFailure('website_access_blocked',True)
    with patch('flightwatch.cli.utcnow',return_value=aware('2027-01-20T00:00:00Z')), \
         patch('flightwatch.cli.WebsiteProvider.search',blocked), \
         patch.dict('os.environ',{'CAMPAIGN_START':''}):
        asyncio.run(run(args(tmp_path),cfg))
        (tmp_path/'ledger.json').write_text('{}')
        asyncio.run(run(args(tmp_path),cfg))
        assert len(list((tmp_path/'pending').glob('*.json')))==90


def test_known_source_coverage_does_not_fail_job_when_delivered(cfg,tmp_path):
    from flightwatch.core import aware
    cfg['campaign_start']='2027-01-20T00:00:00Z'
    cfg['airports']={'LHR':'Europe/London','VIE':'Europe/Vienna'}
    cfg['departure_dates']=['2027-02-18']
    received=[]
    closed=[]
    class Provider:
        source='ryanair.com'
        async def search(self,origin,day,cfg):
            if origin=='LHR':
                raise SearchFailure('origin_not_in_ryanair_airport_inventory',metadata={'attempted':False})
            return [],{'completion_signal':'complete_fixture'}
        async def aclose(self): closed.append(True)
    class Sink:
        def __init__(self,cfg): pass
        async def deliver(self,payload): received.append(payload)
    a=args(tmp_path)
    a.provider='ryanair'
    a.no_upload=False
    with patch('flightwatch.cli.utcnow',return_value=aware('2027-01-20T00:00:00Z')), \
         patch('flightwatch.cli.RyanairProvider',Provider), \
         patch('flightwatch.cli.SupabaseSink',Sink), \
         patch.dict('os.environ',{'CAMPAIGN_START':''}):
        assert asyncio.run(run(a,cfg))==0
    assert len(received)==2 and received[0]['status']=='error' and received[1]['status']=='no_offers'
    assert all(s['source']=='ryanair.com' for s in received)
    assert closed==[True]


def test_manual_all_collects_every_route_and_delivers_without_campaign(cfg,tmp_path):
    cfg['campaign_start']=None
    calls=[]
    received=[]
    closed=[]
    class Provider:
        source='ryanair.com'
        async def search(self,origin,day,cfg):
            calls.append((origin,day))
            return [],{'completion_signal':'complete_fixture'}
        async def aclose(self): closed.append(True)
    class Sink:
        def __init__(self,cfg): pass
        async def deliver(self,payload): received.append(payload)
    a=args(tmp_path)
    a.all=True
    a.provider='ryanair'
    a.no_upload=False
    with patch('flightwatch.cli.RyanairProvider',Provider), \
         patch('flightwatch.cli.SupabaseSink',Sink), \
         patch.dict('os.environ',{'CAMPAIGN_START':''}):
        assert asyncio.run(run(a,cfg))==0
        assert asyncio.run(run(a,cfg))==0
    expected=[(o,d) for o in cfg['airports'] for d in cfg['departure_dates']]
    assert len(expected)==90
    assert calls==expected+expected
    assert len(received)==180
    assert all(s['source']=='ryanair.com' for s in received)
    ledger=json.loads((tmp_path/'ledger.json').read_text())
    assert len(ledger)==2 and all(k.startswith('manual-') for k in ledger)
    assert not list((tmp_path/'pending').glob('*.json'))
    assert cfg['campaign_start'] is None
    assert closed==[True,True]


def test_manual_scopes_are_mutually_exclusive():
    import pytest
    from flightwatch.cli import main
    with patch('sys.argv',['flightwatch','run','--all','--test']):
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code==2
