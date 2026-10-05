from datetime import datetime, timezone
import pytest
from flightwatch.core import aware, campaign_slot, event_fields, snapshot, stable_key
from flightwatch.providers import SearchFailure, local_datetime, normalize, price_amount


def test_deadline_inclusive(cfg):
    assert event_fields('2027-02-18T17:00:00+01:00', cfg)['eligible_for_event']
    assert not event_fields('2027-02-18T17:00:01+01:00', cfg)['eligible_for_event']
    assert event_fields('2027-02-17T23:59:00+01:00', cfg)['eligible_for_event']
    assert event_fields('2027-02-18T16:00:00Z', cfg)['minutes_to_event'] == 180
    cfg['buffer_minutes'] = 240
    assert not event_fields('2027-02-18T17:00:00+01:00', cfg)['eligible_for_event']


def test_timezones(cfg):
    assert len(cfg['airports']) == 30
    for airport, expected in [('LHR', 0), ('IST', 3), ('SVO', 3), ('VIE', 1), ('ATH', 2)]:
        dt = local_datetime(dict(year=2027, month=2, day=18, hour=0, minute=30), cfg['airports'][airport])
        assert dt.utcoffset().total_seconds()/3600 == expected
    ist = local_datetime(dict(year=2027, month=2, day=18, hour=0, minute=30), 'Europe/Istanbul')
    assert ist.date().isoformat() == '2027-02-18'
    assert ist.astimezone(timezone.utc).date().isoformat() == '2027-02-17'
    with pytest.raises(SearchFailure):
        local_datetime(dict(year=2027, month=10, day=31, hour=2, minute=30), 'Europe/Rome')
    with pytest.raises(ValueError):
        aware('2027-02-18T17:00:00')


def test_campaign_fixed_start(cfg):
    start = '2027-01-20T00:00:00Z'
    assert campaign_slot(start, aware(start), cfg) == 0
    assert campaign_slot(start, aware('2027-02-09T23:59:59Z'), cfg) == 41
    assert campaign_slot(start, aware('2027-02-10T00:00:00Z'), cfg) is None
    assert campaign_slot(start, aware('2027-01-19T23:59:59Z'), cfg) is None
    assert len(cfg['airports'])*len(cfg['departure_dates'])*42 == 3780


@pytest.mark.parametrize('unit,amount', [('PRICE_UNIT_WHOLE','12.34'),('PRICE_UNIT_CENTI','1234'),
                                        ('PRICE_UNIT_MILLI','12340'),('PRICE_UNIT_MICRO','12340000')])
def test_prices(unit, amount):
    assert price_amount({'unit':unit,'amount':amount}) == '12.34'


def test_nonstop_and_unknowns(cfg, results):
    rows, _ = normalize(results, 'VIE', '2027-02-18', cfg)
    assert len(rows) == 1
    assert rows[0]['available_seats'] is None
    assert rows[0]['cabin_baggage'] is None
    assert rows[0]['checked_baggage'] is None
    assert rows[0]['fare_name'] is None
    assert rows[0]['aircraft_type'] is None
    assert rows[0]['eligible_for_event']


@pytest.mark.parametrize('mutation', ['connection','technical','segments','unknown','other_airport','late'])
def test_filter(cfg, results, mutation):
    leg = results['legs']['leg']
    seg = results['segments']['segment']
    if mutation == 'connection': leg['stopCount'] = 1
    if mutation == 'technical': seg['technicalStops'] = [{'iata':'ABC'}]
    if mutation == 'segments': leg['segmentIds'].append('another')
    if mutation == 'unknown': del leg['stopCount']
    if mutation == 'other_airport': results['places']['rome']['iata'] = 'NAP'
    if mutation == 'late': seg['arrivalDateTime']['minute'] = 1
    rows, _ = normalize(results, 'VIE', '2027-02-18', cfg)
    if mutation == 'late':
        assert len(rows) == 1 and not rows[0]['eligible_for_event']
    else:
        assert rows == []


def test_stable_keys_exclude_session_and_price(cfg, results):
    before, _ = normalize(results, 'VIE', '2027-02-18', cfg)
    itinerary = results['itineraries'].pop('ephemeral-itinerary')
    results['itineraries']['another-session-id'] = itinerary
    itinerary['pricingOptions'][0]['id'] = 'another-fare-id'
    itinerary['pricingOptions'][0]['price']['amount'] = '99900000'
    after, _ = normalize(results, 'VIE', '2027-02-18', cfg)
    assert before[0]['flight_key'] == after[0]['flight_key']
    assert before[0]['fare_key'] == after[0]['fare_key']
    assert stable_key('skyscanner.com', 'flight') != stable_key('other.com', 'flight')


def test_snapshot_cannot_claim_complete(cfg):
    with pytest.raises(ValueError):
        snapshot(cfg, 'run', 'VIE', '2027-02-18', 'no_offers', complete=False)
