import copy
import pytest
from flightwatch.core import load_config


@pytest.fixture
def cfg():
    c = load_config()
    c['poll_interval_seconds'] = 0
    c['between_searches_seconds'] = 0
    return c


@pytest.fixture
def results():
    # Explicitly synthetic schema fixture. No real flight or market price claim.
    return {'itineraries': {'ephemeral-itinerary': {
        'legIds': ['leg'], 'pricingOptions': [
            {'id': 'session-fare', 'agentIds': ['seller'],
             'price': {'amount': '12340000', 'unit': 'PRICE_UNIT_MICRO'}}]}},
        'legs': {'leg': {'stopCount': 0, 'segmentIds': ['segment']}},
        'segments': {'segment': {
            'originPlaceId': 'vienna', 'destinationPlaceId': 'rome',
            'departureDateTime': dict(year=2027, month=2, day=18, hour=15, minute=30),
            'arrivalDateTime': dict(year=2027, month=2, day=18, hour=17, minute=0),
            'durationInMinutes': 90, 'marketingCarrierId': 'carrier',
            'operatingCarrierId': 'carrier', 'marketingFlightNumber': 'TEST000'}},
        'places': {'vienna': {'iata': 'VIE'}, 'rome': {'iata': 'FCO'}},
        'carriers': {'carrier': {'name': 'Synthetic Carrier', 'iata': 'XX'}},
        'agents': {'seller': {'name': 'Synthetic Seller'}}}
