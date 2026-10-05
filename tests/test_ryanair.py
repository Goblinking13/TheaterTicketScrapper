import asyncio
import copy
import json
from pathlib import Path

import pytest

from flightwatch.core import snapshot
from flightwatch.providers import SearchFailure
from flightwatch.ryanair import RyanairProvider, airport_codes, availability_params, normalize_availability


@pytest.fixture
def observed():
    # Minimal observed-field fixture from a real search; transient booking keys removed.
    return json.loads(Path('tests/fixtures/ryanair-observed-fields.json').read_text())


def test_real_observed_values_and_event_filter(cfg,observed):
    rows,meta=normalize_availability(observed,'VIE','FCO','2027-02-18',cfg)
    assert len(rows)==2
    assert rows[0]['price']=='27.99' and rows[0]['eligible_for_event']
    assert rows[1]['price']=='43.89' and not rows[1]['eligible_for_event']
    assert rows[0]['available_seats'] is None
    assert rows[1]['available_seats']==5 and rows[1]['available_seats_scope']=='current_price'
    assert all(r['cabin_baggage'] is None and r['checked_baggage'] is None for r in rows)
    assert rows[0]['operating_carrier']['name']=='Lauda Europe'
    assert meta['completion_signal']=='full_requested_date_array'


def test_missing_date_not_no_offers(cfg,observed):
    observed['trips'][0]['dates']=[]
    with pytest.raises(SearchFailure,match='missing_requested_date'):
        normalize_availability(observed,'VIE','FCO','2027-02-18',cfg)


def test_complete_empty_array(cfg,observed):
    observed['trips'][0]['dates'][0]['flights']=[]
    assert normalize_availability(observed,'VIE','FCO','2027-02-18',cfg)[0]==[]


def test_clock_mismatch_fails(cfg,observed):
    observed['trips'][0]['dates'][0]['flights'][0]['segments'][0]['timeUTC'][0]='2027-02-18T04:10:00Z'
    with pytest.raises(SearchFailure,match='timezone_mismatch'):
        normalize_availability(observed,'VIE','FCO','2027-02-18',cfg)


def test_connection_excluded(cfg,observed):
    flights=observed['trips'][0]['dates'][0]['flights']
    flights[0]['segments'].append(copy.deepcopy(flights[0]['segments'][0]))
    rows,meta=normalize_availability(observed,'VIE','FCO','2027-02-18',cfg)
    assert len(rows)==1 and meta['rejected_connections']==1


def test_price_and_booking_ids_do_not_change_fare_key(cfg,observed):
    before=normalize_availability(observed,'VIE','FCO','2027-02-18',cfg)[0][0]
    flight=observed['trips'][0]['dates'][0]['flights'][0]
    flight['flightKey']='synthetic-new-session-flight-key'
    flight['regularFare']['fareKey']='synthetic-new-session-fare-key'
    flight['regularFare']['fares'][0]['amount']=88.88
    after=normalize_availability(observed,'VIE','FCO','2027-02-18',cfg)[0][0]
    assert before['flight_key']==after['flight_key'] and before['fare_key']==after['fare_key']


def test_source_separation_in_snapshot_keys(cfg):
    a=snapshot(cfg,'same-run','VIE','2027-02-18','no_offers',complete=True,source='ryanair.com')
    b=snapshot(cfg,'same-run','VIE','2027-02-18','no_offers',complete=True,source='skyscanner.com')
    assert a['snapshot_id']!=b['snapshot_id'] and a['source']=='ryanair.com'


def test_second_airport_failure_does_not_publish_partial_success(cfg,observed):
    provider=RyanairProvider()
    class Page:
        async def evaluate(self,script,args):
            if args['params']['Destination']=='FCO':
                return {'status':200,'data':observed}
            return {'status':409,'data':{'message':'Availability declined'}}
    provider.page=Page()
    provider.version='test-only-version'
    provider.airports={'VIE','FCO','CIA'}
    provider.routes={'VIE':{'FCO','CIA'}}
    with pytest.raises(SearchFailure) as exc:
        asyncio.run(provider.search('VIE','2027-02-18',cfg))
    assert exc.value.blocked


def test_only_requested_date_no_connections():
    params=availability_params('VIE','FCO','2027-02-18')
    assert params['FlexDaysOut']=='0' and params['IncludeConnectingFlights']=='false'
    assert params['ADT']=='1' and params['IncludePrimeFares']=='false'


def test_unsupported_origin_is_not_a_global_access_block(cfg):
    provider=RyanairProvider()
    provider.airports={'VIE','FCO','CIA'}
    with pytest.raises(SearchFailure) as exc:
        asyncio.run(provider.search('LHR','2027-02-18',cfg))
    assert not exc.value.blocked
    assert exc.value.code=='origin_not_in_ryanair_airport_inventory'
    assert exc.value.metadata['attempted'] is False
    assert provider.browser is None


def test_inventory_cannot_be_missing_or_malformed():
    assert airport_codes([{'code':'VIE'},{'code':'FCO'}])=={'VIE','FCO'}
    for payload in [[],{},[{'name':'Vienna'}],[{'code':'VIE'},{'code':'VIE'}]]:
        with pytest.raises(SearchFailure): airport_codes(payload)


def test_route_lookup_without_rome_is_complete_scoped_no_offers(cfg):
    provider=RyanairProvider()
    provider.airports={'AMS','FCO','CIA'}
    provider.routes={'AMS':{'AGP','DUB'}}  # Actual observed route-widget response for AMS.
    rows,meta=asyncio.run(provider.search('AMS','2027-02-18',cfg))
    assert rows==[] and provider.browser is None
    assert meta['http_searches']==0
    assert all(c['route_supported_by_source'] is False for c in meta['checked_destinations'])


def test_route_schema_must_be_present():
    from flightwatch.ryanair import route_destination_codes
    assert route_destination_codes([])==set()
    assert route_destination_codes([{'arrivalAirport':{'code':'CIA'}}])=={'CIA'}
    with pytest.raises(SearchFailure): route_destination_codes({'message':'error'})
