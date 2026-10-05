import asyncio
import fcntl
import json
import plistlib
from unittest.mock import patch

from flightwatch.core import aware, stable_key
from flightwatch.local_scheduler import tick, write_plist


def test_restart_and_missed_slots_only_collect_current_slot(cfg, tmp_path):
    cfg['campaign_start'] = None
    calls = []
    async def collect(args, config):
        slot = len(calls) * 4
        calls.append((args.command, config['campaign_start']))
        key = 'campaign-' + stable_key(aware(config['campaign_start']).isoformat(), config,
                                      args.provider, slot)
        path = tmp_path / 'ledger.json'
        ledger = json.loads(path.read_text()) if path.exists() else {}
        ledger[key] = [f'{o}:{d}' for o in config['airports'] for d in config['departure_dates']]
        path.write_text(json.dumps(ledger))
        return 0
    with patch.dict('os.environ', {'CAMPAIGN_START': '', 'PROVIDER': ''}), \
         patch('flightwatch.local_scheduler.run', collect):
        with patch('flightwatch.local_scheduler.utcnow', return_value=aware('2026-10-04T10:00:00Z')):
            assert asyncio.run(tick(cfg, tmp_path)) == 0
            assert asyncio.run(tick(cfg, tmp_path)) == 0
        # Machine was off for two days: collect slot 4 once, without replaying 1,2,3.
        with patch('flightwatch.local_scheduler.utcnow', return_value=aware('2026-10-06T10:01:00Z')):
            assert asyncio.run(tick(cfg, tmp_path)) == 0
            assert asyncio.run(tick(cfg, tmp_path)) == 0
    assert calls == [('run', '2026-10-04T10:00:00+00:00')] * 2
    assert cfg['campaign_start'] is None


def test_lock_prevents_parallel_collection(cfg, tmp_path):
    with (tmp_path / 'local-scheduler.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with patch('flightwatch.local_scheduler.run') as run:
            assert asyncio.run(tick(cfg, tmp_path)) == 0
            run.assert_not_called()


def test_completed_campaign_still_retries_pending(cfg, tmp_path):
    cfg['campaign_start'] = '2026-09-01T00:00:00Z'
    (tmp_path / 'pending').mkdir()
    (tmp_path / 'pending' / 'queued.json').write_text('{}')
    calls = []
    async def deliver(args, cfg):
        calls.append(args.command)
        return 1
    with patch.dict('os.environ', {'CAMPAIGN_START': ''}), \
         patch('flightwatch.local_scheduler.utcnow', return_value=aware('2026-10-04T10:00:00Z')), \
         patch('flightwatch.local_scheduler.run', deliver):
        assert asyncio.run(tick(cfg, tmp_path)) == 1
    assert calls == ['deliver']


def test_plist_uses_project_path_and_contains_no_credentials(tmp_path):
    output = tmp_path / 'service.plist'
    write_plist(tmp_path / 'project with spaces', output)
    data = plistlib.loads(output.read_bytes())
    assert data['RunAtLoad'] is True and data['StartInterval'] == 300
    assert data['ProgramArguments'] == ['/bin/bash', str(tmp_path / 'project with spaces/scripts/local-run.sh')]
    assert 'SUPABASE_SERVICE_ROLE_KEY' not in output.read_text()
