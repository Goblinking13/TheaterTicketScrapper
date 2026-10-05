import asyncio
import json
import pytest
import httpx
from flightwatch.core import snapshot
from flightwatch.storage import DeliveryFailure, Outbox, SupabaseSink


def test_retry_after_server_commit_before_ack(tmp_path, cfg):
    box = Outbox(tmp_path)
    snap = snapshot(cfg,'test','VIE','2027-02-18','blocked',error_code='fixture')
    box.enqueue(snap)
    stored = set()
    attempts = []
    def handler(request):
        payload = json.loads(request.content)['p_snapshot']
        stored.add(payload['snapshot_id'])
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ReadTimeout('lost acknowledgement')
        return httpx.Response(200,json={'accepted':True,'duplicate':True})
    sink = SupabaseSink(cfg,url='https://example.supabase.co',key='fake-test-key',
                        transport=httpx.MockTransport(handler))
    report = asyncio.run(box.flush(sink))
    assert report['pending'] == 1
    # Brand new process-like outbox recovers the unchanged payload.
    report = asyncio.run(Outbox(tmp_path).flush(sink))
    assert report['delivered'] == 1 and report['pending'] == 0
    assert len(stored) == 1


def test_budget_failure_preserves_all(tmp_path,cfg):
    box = Outbox(tmp_path,budget=10)
    box.enqueue(snapshot(cfg,'run','VIE','2027-02-18','blocked'))
    assert box.full()
    def handler(request):
        return httpx.Response(400,json={'message':'storage_budget_exceeded'})
    sink = SupabaseSink(cfg,url='https://example.supabase.co',key='fake',transport=httpx.MockTransport(handler))
    asyncio.run(box.flush(sink))
    assert len(box.files()) == 1


def test_local_idempotency_conflict(tmp_path,cfg):
    box = Outbox(tmp_path)
    snap = snapshot(cfg,'run','VIE','2027-02-18','blocked')
    box.enqueue(snap)
    box.enqueue(snap)
    snap['status'] = 'error'
    with pytest.raises(ValueError): box.enqueue(snap)


def test_delivery_budget_keeps_unsent_snapshots(tmp_path,cfg):
    box=Outbox(tmp_path)
    box.enqueue(snapshot(cfg,'run','VIE','2027-02-18','blocked'))
    class NeverCalled:
        async def deliver(self,payload): raise AssertionError('Budget already exhausted')
    report=asyncio.run(box.flush(NeverCalled(),deadline=0))
    assert report['pending']==1 and report['delivered']==0
