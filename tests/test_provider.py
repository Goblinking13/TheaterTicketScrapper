import asyncio
import copy
import json
import httpx
import pytest
from flightwatch.providers import PartnerProvider, SearchFailure, WebsiteProvider, merge_page


def page(results,status='RESULT_STATUS_COMPLETE',action='RESULT_ACTION_REPLACED'):
    return {'sessionToken':'synthetic-token','status':status,'action':action,'content':{'results':results}}


def test_poll_until_complete(cfg,results):
    calls=[]
    def handler(request):
        calls.append(request)
        if len(calls)==1:
            assert json.loads(request.content)['query']['queryLegs'][0]['destinationPlaceId']['iata'] == 'ROM'
            return httpx.Response(200,json=page({'itineraries':{}},'RESULT_STATUS_INCOMPLETE'))
        return httpx.Response(200,json=page(results))
    rows,meta=asyncio.run(PartnerProvider('fake',httpx.MockTransport(handler)).search('VIE','2027-02-18',cfg))
    assert len(rows)==1 and meta['polls']==1
    assert calls[1].url.path.endswith('/poll/synthetic-token')


def test_replacement_discards_stale(results):
    cache={}
    merge_page(cache,page(results))
    merge_page(cache,page({'itineraries':{}}))
    assert cache['itineraries']=={}
    merge_page(cache,page({},action='RESULT_ACTION_NOT_MODIFIED'))
    assert cache['itineraries']=={}


def test_missing_contract_and_degradation():
    with pytest.raises(SearchFailure): merge_page({},page({},action='RESULT_ACTION_OMITTED'))
    with pytest.raises(SearchFailure): merge_page({},page({},action='RESULT_ACTION_NOT_MODIFIED'))
    p=page({'itineraries':{}})
    p['degradationReasons']=['synthetic-degradation']
    with pytest.raises(SearchFailure): merge_page({},p)


def test_never_treat_timeout_as_no_offers(cfg):
    cfg['search_timeout_seconds']=0
    transport=httpx.MockTransport(lambda request:httpx.Response(200,json=page({'itineraries':{}},'RESULT_STATUS_INCOMPLETE')))
    with pytest.raises(SearchFailure,match='search_incomplete_timeout'):
        asyncio.run(PartnerProvider('fake',transport).search('VIE','2027-02-18',cfg))


def test_block_stops_polling(cfg):
    calls=[]
    def handler(request):
        calls.append(1)
        return httpx.Response(429)
    with pytest.raises(SearchFailure) as exc:
        asyncio.run(PartnerProvider('fake',httpx.MockTransport(handler)).search('VIE','2027-02-18',cfg))
    assert exc.value.blocked and len(calls)==1


def test_complete_empty_is_explicit(cfg):
    transport=httpx.MockTransport(lambda request:httpx.Response(200,json=page({'itineraries':{}})))
    rows,meta=asyncio.run(PartnerProvider('fake',transport).search('VIE','2027-02-18',cfg))
    assert rows==[] and meta['completion_signal']=='RESULT_STATUS_COMPLETE'
