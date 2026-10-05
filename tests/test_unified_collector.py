import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from flightwatch.core import aware
from flightwatch.local_scheduler import collect_all
from flightwatch.theater import tick

SNAPSHOT = {'id': 'fixture-uuid', 'available_total': 12}


def test_theater_hourly_restart_and_catch_up(tmp_path):
    async def capture(args):
        output = Path(args.capture_root) / 'output' / 'fixture'
        output.mkdir(parents=True)
        return output
    with patch('flightwatch.theater.supabase_settings', return_value=('https://example.supabase.co', 'test')), \
         patch('flightwatch.theater.investigate', side_effect=capture) as browser, \
         patch('flightwatch.theater.extract_snapshot', return_value=SNAPSHOT), \
         patch('flightwatch.theater.upload_snapshot') as upload:
        for moment in ['2026-10-04T10:10:00Z', '2026-10-04T10:59:00Z',
                       '2026-10-04T11:10:00Z', '2026-10-06T11:10:00Z']:
            assert asyncio.run(tick(tmp_path, now=aware(moment))) == 0
        assert browser.call_count == 3 and upload.call_count == 3
        assert all(call.args[0].headless for call in browser.call_args_list)
    assert not list((tmp_path / 'pending').glob('*.json'))
    assert not (tmp_path / 'captures').exists()


def test_theater_failed_upload_is_retried_without_new_capture(tmp_path):
    with patch('flightwatch.theater.supabase_settings', return_value=('https://example.supabase.co', 'test')), \
         patch('flightwatch.theater.investigate', new_callable=AsyncMock, return_value=tmp_path) as browser, \
         patch('flightwatch.theater.extract_snapshot', return_value=SNAPSHOT), \
         patch('flightwatch.theater.upload_snapshot', side_effect=[ValueError('offline'), None]) as upload:
        now = aware('2026-10-04T10:10:00Z')
        assert asyncio.run(tick(tmp_path, now=now)) == 1
        assert len(list((tmp_path / 'pending').glob('*.json'))) == 1
        assert asyncio.run(tick(tmp_path, now=now)) == 0
        assert browser.call_count == 1 and upload.call_count == 2
        assert upload.call_args_list[0].args[0]['id'] == upload.call_args_list[1].args[0]['id']


def test_theater_recovers_enqueue_before_marker_crash(tmp_path):
    now = aware('2026-10-04T10:10:00Z')
    pending = tmp_path / 'pending'
    pending.mkdir()
    (pending / f'{int(now.timestamp() // 3600)}.json').write_text(json.dumps(SNAPSHOT))
    with patch('flightwatch.theater.supabase_settings', return_value=('https://example.supabase.co', 'test')), \
         patch('flightwatch.theater.investigate', new_callable=AsyncMock) as browser, \
         patch('flightwatch.theater.upload_snapshot') as upload:
        assert asyncio.run(tick(tmp_path, now=now)) == 0
        browser.assert_not_called()
        upload.assert_called_once()


def test_source_failure_does_not_prevent_other_collector(cfg, tmp_path):
    with patch('flightwatch.local_scheduler.tick', new_callable=AsyncMock, side_effect=ValueError('offline')), \
         patch('flightwatch.local_scheduler.theater_tick', new_callable=AsyncMock, return_value=0) as theater:
        assert asyncio.run(collect_all(cfg, tmp_path)) == 1
        theater.assert_awaited_once_with(tmp_path / 'theater')


def test_unified_service_prevents_overlapping_runs(cfg, tmp_path):
    import fcntl
    with (tmp_path / 'collector.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with patch('flightwatch.local_scheduler.tick', new_callable=AsyncMock) as flights, \
             patch('flightwatch.local_scheduler.theater_tick', new_callable=AsyncMock) as theater:
            assert asyncio.run(collect_all(cfg, tmp_path)) == 0
            flights.assert_not_called()
            theater.assert_not_called()
