"""Ryanair's actual website availability response, verified with ordinary Chromium."""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from .core import aware, event_fields, stable_key
from .providers import SearchFailure

SOURCE = 'ryanair.com'
AIRPORTS_URL = 'https://www.ryanair.com/api/views/locate/5/airports/en/active'
ROUTES_BASE = 'https://www.ryanair.com/api/views/locate/searchWidget/routes/en/airport'


def airport_codes(payload):
    if not isinstance(payload, list) or not payload:
        raise SearchFailure('ryanair_airport_inventory_invalid')
    codes = {a.get('code') for a in payload if isinstance(a, dict)}
    if len(codes) != len(payload) or any(not isinstance(c, str) or not re.fullmatch('[A-Z]{3}', c) for c in codes):
        raise SearchFailure('ryanair_airport_inventory_invalid')
    return codes


def route_destination_codes(payload):
    if not isinstance(payload, list):
        raise SearchFailure('ryanair_route_search_invalid')
    codes = set()
    for route in payload:
        code = route.get('arrivalAirport', {}).get('code') if isinstance(route, dict) else None
        if not isinstance(code, str) or not re.fullmatch('[A-Z]{3}', code):
            raise SearchFailure('ryanair_route_search_invalid')
        codes.add(code)
    return codes


def booking_url(origin, day, destination='FCO'):
    return 'https://www.ryanair.com/gb/en/trip/flights/select?' + urlencode({
        'adults': 1, 'teens': 0, 'children': 0, 'infants': 0, 'dateOut': day, 'dateIn': '',
        'isConnectedFlight': 'false', 'discount': 0, 'promoCode': '', 'isReturn': 'false',
        'originIata': origin, 'destinationIata': destination})


def availability_params(origin, destination, day):
    return {'ADT': '1', 'TEEN': '0', 'CHD': '0', 'INF': '0', 'Origin': origin,
            'Destination': destination, 'promoCode': '', 'IncludeConnectingFlights': 'false',
            'DateOut': day, 'DateIn': '', 'FlexDaysBeforeOut': '0', 'FlexDaysOut': '0',
            'FlexDaysBeforeIn': '0', 'FlexDaysIn': '0', 'RoundTrip': 'false',
            'IncludePrimeFares': 'false', 'ToUs': 'AGREED'}


def normalize_availability(payload, origin, destination, day, cfg):
    """Validate the exact route/day envelope; missing dates never mean zero flights."""
    if not isinstance(payload, dict) or not isinstance(payload.get('trips'), list):
        raise SearchFailure('ryanair_missing_trips')
    if not isinstance(payload.get('currency'), str) or not payload.get('serverTimeUTC'):
        raise SearchFailure('ryanair_incomplete_envelope')
    matches = [t for t in payload['trips'] if t.get('origin') == origin and t.get('destination') == destination]
    if len(matches) != 1:
        raise SearchFailure('ryanair_route_mismatch')
    dates = matches[0].get('dates')
    if not isinstance(dates, list):
        raise SearchFailure('ryanair_missing_dates')
    dates = [d for d in dates if d.get('dateOut', '').split('T')[0] == day]
    if len(dates) != 1 or not isinstance(dates[0].get('flights'), list):
        raise SearchFailure('ryanair_missing_requested_date')
    rows, rejected, unavailable = [], 0, 0
    for flight in dates[0]['flights']:
        if flight.get('errors'):
            raise SearchFailure('ryanair_flight_error')
        segments = flight.get('segments')
        if not isinstance(segments, list):
            raise SearchFailure('ryanair_missing_segments')
        if len(segments) != 1:
            rejected += 1
            continue
        seg = segments[0]
        if seg.get('origin') != origin or seg.get('destination') != destination:
            raise SearchFailure('ryanair_segment_route_mismatch')
        times = seg.get('timeUTC')
        local = seg.get('time')
        if not isinstance(times, list) or len(times) != 2 or not isinstance(local, list) or len(local) != 2:
            raise SearchFailure('ryanair_missing_flight_times')
        departure = aware(times[0]).astimezone(ZoneInfo(cfg['airports'][origin]))
        arrival = aware(times[1]).astimezone(ZoneInfo(cfg['event_timezone']))
        # Validate the source's local and UTC clocks against the configured zones.
        from datetime import datetime
        if (datetime.fromisoformat(local[0]) != departure.replace(tzinfo=None) or
                datetime.fromisoformat(local[1]) != arrival.replace(tzinfo=None)):
            raise SearchFailure('ryanair_timezone_mismatch')
        if departure.date().isoformat() != day or arrival <= departure:
            raise SearchFailure('ryanair_inconsistent_flight_times')
        if len(flight.get('timeUTC', [])) != 2 or flight['timeUTC'] != times:
            raise SearchFailure('ryanair_leg_segment_time_mismatch')
        regular = flight.get('regularFare')
        if not regular:
            # Website JS explicitly maps missing regularFare to isSoldout.
            unavailable += 1
            continue
        adults = [f for f in regular.get('fares', []) if f.get('type') == 'ADT']
        if len(adults) != 1 or adults[0].get('count') != 1:
            raise SearchFailure('ryanair_adult_fare_mismatch')
        amount = Decimal(str(adults[0]['amount']))
        if not amount.is_finite() or amount < 0:
            raise SearchFailure('ryanair_invalid_price')
        number = flight.get('flightNumber') or seg.get('flightNumber')
        marketing = {'iata': number[:2] if number else None, 'icao': None, 'name': None}
        operating = {'iata': None, 'icao': None, 'name': flight.get('operatedBy') or None}
        flight_key = stable_key(SOURCE, origin, destination, departure.isoformat(), arrival.isoformat(),
                                number, marketing, operating)
        fare_key = stable_key(SOURCE, flight_key, 'economy', 'regularFare')
        seats = flight.get('faresLeft')
        if not isinstance(seats, int) or isinstance(seats, bool) or seats < 0:
            seats = None
        row = {'flight_key': flight_key, 'fare_key': fare_key, 'origin': origin, 'destination': destination,
               'departure_at': departure.isoformat(), 'arrival_at': arrival.isoformat(),
               'departure_timezone': cfg['airports'][origin], 'arrival_timezone': cfg['event_timezone'],
               'marketing_carrier': marketing, 'operating_carrier': operating, 'flight_number': number,
               'duration_minutes': (arrival-departure).total_seconds()/60, 'aircraft_type': None,
               'price': str(amount), 'currency': payload['currency'], 'cabin_class': 'economy',
               'fare_name': 'Basic Fare', 'fare_identity_known': True,
               'seller_names': ['Ryanair website'], 'availability': 'offered',
               'available_seats': seats, 'available_seats_scope': 'current_price' if seats is not None else None,
               'cabin_baggage': None, 'checked_baggage': None,
               'search_url': booking_url(origin, day, destination), **event_fields(arrival.isoformat(), cfg)}
        row['observation_key'] = stable_key(SOURCE, fare_key, row)
        rows.append(row)
    return rows, {'destination': destination, 'flight_count': len(dates[0]['flights']),
                  'rejected_connections': rejected, 'unavailable_flights': unavailable,
                  'currency_returned': payload['currency'], 'server_time_utc': payload['serverTimeUTC'],
                  'completion_signal': 'full_requested_date_array', 'fare_coverage': 'public_basic_fare'}


class RyanairProvider:
    source = SOURCE

    def __init__(self):
        self.pw = self.browser = self.page = None
        self.version = None
        self.airports = None
        self.routes = {}

    def search_link(self, origin, day, cfg):
        destination = next((d for d in cfg['destination_airports'] if d in self.routes.get(origin, set())),
                           cfg['destination_airports'][0])
        return booking_url(origin, day, destination)

    async def aclose(self):
        if self.browser:
            await self.browser.close()
            self.browser = self.page = None
        if self.pw:
            await self.pw.stop()
            self.pw = None
        self.version = None

    async def _prepare(self, origin, day, cfg):
        if self.page and self.version:
            return
        if self.pw:
            await self.aclose()
        from playwright.async_api import async_playwright
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        response = await self.page.goto(self.search_link(origin, day, cfg), wait_until='domcontentloaded',
                                        timeout=cfg['search_timeout_seconds']*1000)
        if not response or response.status != 200:
            raise SearchFailure('ryanair_browser_bootstrap_failed', blocked=response is not None and response.status in {403,429})
        # Version is published in the actual app HTML, not a hardcoded session/header.
        html = await self.page.content()
        version = re.search(r'Desktop version:\s*([0-9]+\.[0-9]+\.[0-9]+)', html)
        if not version:
            raise SearchFailure('ryanair_app_version_missing')
        self.version = version.group(1)

    async def _load_inventory(self):
        if self.airports is not None:
            return
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(AIRPORTS_URL)
        if response.status_code in {401,403,409,429}:
            raise SearchFailure('ryanair_airport_inventory_blocked', True)
        if response.status_code != 200:
            raise SearchFailure('ryanair_airport_inventory_unavailable')
        self.airports = airport_codes(response.json())

    async def _load_routes(self, origin):
        if origin in self.routes:
            return
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(f'{ROUTES_BASE}/{origin}')
        if response.status_code in {401,403,409,429}:
            raise SearchFailure('ryanair_route_search_blocked', True)
        if response.status_code != 200:
            raise SearchFailure('ryanair_route_search_unavailable')
        self.routes[origin] = route_destination_codes(response.json())

    async def search(self, origin, day, cfg):
        from playwright.async_api import Error as BrowserError
        try:
            return await self._search(origin, day, cfg)
        except BrowserError:
            await self.aclose()
            raise SearchFailure('ryanair_browser_or_network_error') from None

    async def _search(self, origin, day, cfg):
        await self._load_inventory()
        if origin not in self.airports:
            raise SearchFailure('origin_not_in_ryanair_airport_inventory', metadata={
                'attempted': False, 'source_coverage': 'Ryanair website only',
                'inventory_checked': True, 'inventory_source': AIRPORTS_URL})
        if any(d not in self.airports for d in cfg['destination_airports']):
            raise SearchFailure('destination_not_in_ryanair_airport_inventory')
        await self._load_routes(origin)
        supported = self.routes[origin].intersection(cfg['destination_airports'])
        if supported:
            await self._prepare(origin, day, cfg)
        rows, checked = [], []
        for destination in cfg['destination_airports']:
            if destination not in supported:
                checked.append({'destination': destination, 'route_supported_by_source': False,
                                'availability_checked': False, 'completion_signal': 'complete_route_widget_search'})
                continue
            if checked:
                await asyncio.sleep(cfg['between_searches_seconds'])
            result = await self.page.evaluate('''async ({params, version}) => {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), 20000);
                try {
                    const response = await fetch('/api/booking/v4/en-gb/availability?' + new URLSearchParams(params), {
                        headers: {client: 'desktop', 'client-version': version}, signal: controller.signal});
                    const text = await response.text();
                    let data; try { data = JSON.parse(text); } catch { data = null; }
                    return {status: response.status, data};
                } finally { clearTimeout(timer); }
            }''', {'params': availability_params(origin, destination, day), 'version': self.version})
            if result['status'] in {401,403,409,429}:
                raise SearchFailure(f'ryanair_http_{result["status"]}', True, {'attempted': True, 'destination': destination})
            if result['status'] != 200:
                raise SearchFailure(f'ryanair_http_{result["status"]}', metadata={'destination':destination})
            offers, meta = normalize_availability(result['data'], origin, destination, day, cfg)
            rows.extend(offers)
            checked.append({**meta, 'route_supported_by_source': True, 'availability_checked': True})
        return rows, {'transport': 'chromium_fetch', 'adapter_verified': True,
                      'source_coverage': 'Ryanair website only', 'checked_destinations': checked,
                      'preferred_currency': cfg['currency'], 'currency_request_supported': None,
                      'currency_policy': 'source_currency_no_conversion',
                      'completion_signal': 'both_airport_route_search_and_available_date_arrays',
                      'route_search_url': f'{ROUTES_BASE}/{origin}',
                      'http_searches': sum(c['availability_checked'] for c in checked),
                      'pagination': 'none_in_verified_route_and_date_array_contract'}
