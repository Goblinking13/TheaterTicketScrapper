import json
from unittest.mock import AsyncMock, patch

import pytest

from flightwatch.cloud_scheduler import main


def test_cloud_requires_fixed_campaign_start(monkeypatch):
    monkeypatch.setattr('sys.argv', ['cloud_scheduler'])
    monkeypatch.delenv('CAMPAIGN_START', raising=False)
    with pytest.raises(SystemExit, match='CAMPAIGN_START'):
        main()


def test_cloud_runs_both_collectors(monkeypatch):
    monkeypatch.setattr('sys.argv', ['cloud_scheduler'])
    monkeypatch.setenv('CAMPAIGN_START', '2026-10-04T20:54:58+00:00')
    with patch('flightwatch.cloud_scheduler.collect_all', new_callable=AsyncMock, return_value=0) as collect:
        with pytest.raises(SystemExit) as result:
            main()
    assert result.value.code == 0
    assert collect.await_args.args[1] == 'state'
