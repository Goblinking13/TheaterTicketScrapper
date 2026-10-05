import asyncio
import io
import zipfile
import httpx
import pytest
from flightwatch.checkpoints import checkpoint


@pytest.fixture
def github_env(monkeypatch):
    monkeypatch.setenv('GITHUB_REPOSITORY','fixture/repository')
    monkeypatch.setenv('GH_TOKEN','test-only-token')
    monkeypatch.setenv('GITHUB_RUN_ID','55')
    monkeypatch.setenv('GITHUB_RUN_ATTEMPT','1')


def artifact(i,name=None):
    return {'id':i,'name':name or f'flightwatch-state-{i}-1','expired':False}


def test_restore_whole_pending_queue(tmp_path,github_env):
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w') as z:
        z.writestr('ledger.json','{"slot": ["VIE:2027-02-18"]}')
        z.writestr('pending/snapshot.json','{"fixture":true}')
    def handler(request):
        if request.url.host=='storage.example':
            assert 'authorization' not in request.headers
            return httpx.Response(200,content=buffer.getvalue())
        if request.url.path.endswith('/zip'):
            return httpx.Response(302,headers={'location':'https://storage.example/checkpoint.zip'})
        return httpx.Response(200,json={'artifacts':[artifact(54)]})
    asyncio.run(checkpoint('restore',tmp_path,httpx.MockTransport(handler)))
    assert (tmp_path/'pending/snapshot.json').exists()
    assert (tmp_path/'ledger.json').exists()


def test_missing_checkpoint_never_resets_campaign(tmp_path,github_env):
    def handler(request):
        if '/workflows/' in request.url.path:
            return httpx.Response(200,json={'workflow_runs':[{'id':54,'status':'completed'}]})
        return httpx.Response(200,json={'artifacts':[]})
    with pytest.raises(RuntimeError,match='Checkpoint missing'):
        asyncio.run(checkpoint('restore',tmp_path,httpx.MockTransport(handler)))


def test_prune_only_after_new_checkpoint(tmp_path,github_env):
    deleted=[]
    def handler(request):
        if request.method=='DELETE':
            deleted.append(request.url.path)
            return httpx.Response(204)
        return httpx.Response(200,json={'artifacts':[artifact(55),artifact(54),artifact(53)]})
    asyncio.run(checkpoint('prune',tmp_path,httpx.MockTransport(handler)))
    assert len(deleted)==1 and deleted[0].endswith('/53')
    def stale(request): return httpx.Response(200,json={'artifacts':[artifact(54),artifact(53)]})
    with pytest.raises(RuntimeError,match='not confirmed'):
        asyncio.run(checkpoint('prune',tmp_path,httpx.MockTransport(stale)))


def test_prune_orders_by_creation_time_instead_of_artifact_id(tmp_path, github_env):
    deleted = []
    artifacts = [
        dict(artifact(100, 'flightwatch-state-54-1'), created_at='2026-10-05T10:00:00Z'),
        dict(artifact(90, 'flightwatch-state-55-1'), created_at='2026-10-05T11:00:00Z'),
        dict(artifact(110, 'flightwatch-state-53-1'), created_at='2026-10-05T09:00:00Z'),
    ]
    def handler(request):
        if request.method == 'DELETE':
            deleted.append(request.url.path)
            return httpx.Response(204)
        return httpx.Response(200, json={'artifacts': artifacts})
    asyncio.run(checkpoint('prune', tmp_path, httpx.MockTransport(handler)))
    assert len(deleted) == 1 and deleted[0].endswith('/110')


def test_first_run_checkpoint_survives_setup_failure(tmp_path,github_env):
    def handler(request):
        if '/workflows/' in request.url.path:
            return httpx.Response(200,json={'workflow_runs':[{'id':55,'status':'in_progress'}]})
        return httpx.Response(200,json={'artifacts':[]})
    asyncio.run(checkpoint('restore',tmp_path,httpx.MockTransport(handler)))
    assert (tmp_path/'ledger.json').read_text()=='{}'


def test_first_cloud_run_restores_migration_state(tmp_path, github_env, monkeypatch):
    import json
    seed = {'ledger.json': {'campaign-existing': ['VIE:2027-02-18']},
            'local-start.json': {'start': '2026-10-04T20:54:58+00:00'},
            'theater/latest.json': {'slot': 497555}}
    monkeypatch.setenv('INITIAL_COLLECTOR_STATE', json.dumps(seed))
    def handler(request):
        if '/workflows/' in request.url.path:
            return httpx.Response(200, json={'workflow_runs': []})
        return httpx.Response(200, json={'artifacts': []})
    asyncio.run(checkpoint('restore', tmp_path, httpx.MockTransport(handler)))
    for name, data in seed.items():
        assert json.loads((tmp_path / name).read_text()) == data


def test_migration_cannot_write_outside_state(tmp_path, github_env, monkeypatch):
    monkeypatch.setenv('INITIAL_COLLECTOR_STATE', '{"../escape.json": {}}')
    def handler(request):
        if '/workflows/' in request.url.path:
            return httpx.Response(200, json={'workflow_runs': []})
        return httpx.Response(200, json={'artifacts': []})
    with pytest.raises(RuntimeError, match='Invalid initial'):
        asyncio.run(checkpoint('restore', tmp_path, httpx.MockTransport(handler)))
