"""Website probe and documented partner API. No invented internal endpoints/selectors."""
from __future__ import annotations

import asyncio
import os
import time
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx

from .core import SOURCE, event_fields, search_url, stable_key


class SearchFailure(Exception):
    def __init__(self, code, blocked=False, metadata=None):
        super().__init__(code)
        self.code, self.blocked, self.metadata = code, blocked, metadata or {}


class WebsiteProvider:
    """Fail closed until a real accessible response contract has been verified."""
    async def search(self, origin, day, cfg):
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(search_url(origin, day, cfg))
        meta = {"http_status": response.status_code, "transport": "html_probe",
                "adapter_verified": False}
        if (response.status_code in {401, 403, 429} or
                any(marker in response.url.path.lower() for marker in ("captcha", "challenge")) or
                any(marker in response.text.lower() for marker in
                    ("verify you are human", "access denied", "px-captcha"))):
            raise SearchFailure("website_access_blocked", True, meta)
        if response.status_code != 200:
            raise SearchFailure("website_http_error", metadata=meta)
        raise SearchFailure("website_response_contract_unverified", metadata=meta)


async def browser_probe(origin, day, cfg):
    """Inspect one real page using ordinary Chromium, stopping at access restrictions.

    Returns only safe structural evidence: no cookies, authorization, or session URLs.
    Capturing internal request payloads is intentionally deferred until access exists.
    """
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        json_responses = []
        def record(response):
            if "json" in response.headers.get("content-type", ""):
                json_responses.append({"host": httpx.URL(response.url).host,
                                       "status": response.status})
        page.on("response", record)
        try:
            response = await page.goto(search_url(origin, day, cfg), wait_until="domcontentloaded",
                                       timeout=cfg["search_timeout_seconds"] * 1000)
            blocked = any(x in page.url.lower() for x in ("captcha", "challenge"))
            if not blocked:
                await page.wait_for_timeout(5000)
                blocked = any(x in page.url.lower() for x in ("captcha", "challenge"))
                text = (await page.locator("body").inner_text()).lower()
                blocked |= any(x in text for x in ("verify you are human", "access denied", "press and hold"))
            return {"status": "blocked" if blocked else "error",
                    "error_code": "website_access_blocked" if blocked else "website_response_contract_unverified",
                    "http_status": response.status if response else None,
                    "transport": "playwright", "json_response_count": len(json_responses),
                    "adapter_verified": False}
        finally:
            await browser.close()


def merge_page(results, page):
    """Honor documented replacement/unchanged actions; reject omitted results."""
    action = page.get("action")
    if action not in {"RESULT_ACTION_REPLACED", "RESULT_ACTION_NOT_MODIFIED"}:
        raise SearchFailure("unknown_result_action")
    if page.get("degradationReasons"):
        raise SearchFailure("degraded_search")
    if action == "RESULT_ACTION_NOT_MODIFIED":
        if not results:
            raise SearchFailure("unchanged_without_prior_results")
        return results
    incoming = page.get("content", {}).get("results")
    if not isinstance(incoming, dict):
        raise SearchFailure("missing_result_maps")
    if not isinstance(incoming.get("itineraries"), dict):
        raise SearchFailure("missing_itinerary_map")
    if action == "RESULT_ACTION_REPLACED":
        results.clear()
    for name, values in incoming.items():
        if isinstance(values, dict):
            results.setdefault(name, {}).update(values)
    return results


def local_datetime(value, zone):
    dt = datetime(**{key: value.get(key, 0) for key in
                     ("year", "month", "day", "hour", "minute", "second")})
    tz = ZoneInfo(zone)
    # Ambiguous or nonexistent times need explicit source offsets, never a guess.
    a, b = dt.replace(tzinfo=tz, fold=0), dt.replace(tzinfo=tz, fold=1)
    if a.utcoffset() != b.utcoffset():
        raise SearchFailure("ambiguous_local_time")
    return a


def price_amount(price):
    divisor = {"PRICE_UNIT_WHOLE": 1, "PRICE_UNIT_CENTI": 100, "PRICE_UNIT_MILLI": 1000,
               "PRICE_UNIT_MICRO": 1000000}.get(price.get("unit"))
    if divisor is None:
        raise SearchFailure("unknown_price_unit")
    amount = Decimal(price["amount"]) / divisor
    if not amount.is_finite() or amount < 0:
        raise SearchFailure("invalid_price")
    return str(amount)


def known_baggage(value):
    if not value or value.get("assessment") in {None, "ASSESSMENT_UNKNOWN", "ASSESSMENT_UNSPECIFIED"}:
        return None
    return {k: v for k, v in value.items() if k in {"assessment", "pieces", "weight", "fee"}}


def normalize(results, origin, day, cfg):
    rows = {}
    rejected = 0
    itineraries = results.get("itineraries", {})
    legs, segments = results.get("legs", {}), results.get("segments", {})
    places, carriers, agents = (results.get(k, {}) for k in ("places", "carriers", "agents"))
    def carrier(carrier_id):
        c = carriers.get(carrier_id, {})
        return {k: c.get(k) or None for k in ("iata", "icao", "name")}
    for itinerary in itineraries.values():
        if len(itinerary.get("legIds", [])) != 1:
            rejected += 1
            continue
        leg = legs[itinerary["legIds"][0]]
        # Missing stopCount is unknown, not zero. A technical-stop indication rejects the leg.
        if (leg.get("stopCount") != 0 or len(leg.get("segmentIds", [])) != 1 or
                leg.get("technicalStops") or leg.get("stops")):
            rejected += 1
            continue
        segment = segments[leg["segmentIds"][0]]
        if segment.get("technicalStops") or segment.get("stops") or segment.get("stopCount", 0) != 0:
            rejected += 1
            continue
        dep_code = places[segment["originPlaceId"]].get("iata")
        arr_code = places[segment["destinationPlaceId"]].get("iata")
        if dep_code != origin or arr_code not in cfg["destination_airports"]:
            rejected += 1
            continue
        departure = local_datetime(segment["departureDateTime"], cfg["airports"][origin])
        arrival = local_datetime(segment["arrivalDateTime"], cfg["event_timezone"])
        if departure.date().isoformat() != day or arrival <= departure:
            raise SearchFailure("inconsistent_flight_times")
        marketing = carrier(segment.get("marketingCarrierId"))
        operating = carrier(segment.get("operatingCarrierId"))
        number = segment.get("marketingFlightNumber") or None
        flight_key = stable_key(SOURCE, origin, arr_code, departure.isoformat(), arrival.isoformat(),
                                marketing, operating, number)
        for option in itinerary.get("pricingOptions", []):
            fare = option.get("pricingOptionFare") or {}
            brand = fare.get("brandNames") or None
            baggage = {"cabin": known_baggage(fare.get("cabinBaggage")),
                       "checked": known_baggage(fare.get("checkedBaggage"))}
            sellers = sorted({agents[a]["name"] for a in option.get("agentIds", [])})
            # Price, deepLink and ephemeral option/itinerary IDs are deliberately excluded.
            # If fare identity is absent, observations are grouped as an unknown fare per seller.
            fare_key = stable_key(SOURCE, flight_key, "economy", brand, sellers, baggage)
            row = {"flight_key": flight_key, "fare_key": fare_key, "origin": origin,
                   "destination": arr_code, "departure_at": departure.isoformat(),
                   "arrival_at": arrival.isoformat(), "departure_timezone": cfg["airports"][origin],
                   "arrival_timezone": cfg["event_timezone"], "marketing_carrier": marketing,
                   "operating_carrier": operating, "flight_number": number,
                   "duration_minutes": segment.get("durationInMinutes"), "aircraft_type": None,
                   "price": price_amount(option["price"]), "currency": cfg["currency"],
                   "cabin_class": "economy", "fare_name": brand, "seller_names": sellers or None,
                   "fare_identity_known": bool(brand), "availability": "offered",
                   "available_seats": None, "cabin_baggage": baggage["cabin"],
                   "checked_baggage": baggage["checked"], "search_url": search_url(origin, day, cfg),
                   **event_fields(arrival.isoformat(), cfg)}
            # Indistinguishable unknown fares: retain every distinct price as an observation.
            key = stable_key(fare_key, row)
            row["observation_key"] = key
            rows[key] = row
    return list(rows.values()), {"itinerary_count": len(itineraries), "rejected_itineraries": rejected,
                                 "offer_count": len(rows), "fare_identity_policy": "unknown_per_seller"}


class PartnerProvider:
    BASE = "https://partners.api.skyscanner.net/apiservices/v3/flights/live/search"

    def __init__(self, api_key=None, transport=None):
        self.api_key = api_key or os.environ.get("SKYSCANNER_API_KEY")
        self.transport = transport

    async def search(self, origin, day, cfg):
        if not self.api_key:
            raise SearchFailure("partner_api_key_missing")
        d = date.fromisoformat(day)
        query = {"market": cfg["market"], "locale": cfg["locale"], "currency": cfg["currency"],
                 "adults": 1, "childrenAges": [], "cabinClass": "CABIN_CLASS_ECONOMY",
                 "nearbyAirports": False, "queryLegs": [{"originPlaceId": {"iata": origin},
                 "destinationPlaceId": {"iata": cfg["destination_iata"]},
                 "date": {"year": d.year, "month": d.month, "day": d.day}}]}
        deadline = time.monotonic() + cfg["search_timeout_seconds"]
        results = {}
        polls = 0
        async with httpx.AsyncClient(headers={"x-api-key": self.api_key}, timeout=20,
                                     transport=self.transport) as client:
            async def request(endpoint, body):
                response = await client.post(f"{self.BASE}/{endpoint}", json=body)
                if response.status_code in {401, 403, 429}:
                    raise SearchFailure(f"partner_http_{response.status_code}", True)
                if response.status_code != 200:
                    raise SearchFailure(f"partner_http_{response.status_code}")
                return response.json()
            page = await request("create", {"query": query})
            token = page.get("sessionToken")
            while True:
                merge_page(results, page)
                if page.get("status") == "RESULT_STATUS_COMPLETE":
                    rows, meta = normalize(results, origin, day, cfg)
                    return rows, {**meta, "transport": "partner_api", "polls": polls,
                                  "completion_signal": "RESULT_STATUS_COMPLETE"}
                if page.get("status") != "RESULT_STATUS_INCOMPLETE" or not token:
                    raise SearchFailure("unknown_search_status_or_missing_session")
                if time.monotonic() + cfg["poll_interval_seconds"] >= deadline:
                    raise SearchFailure("search_incomplete_timeout", metadata={"polls": polls})
                await asyncio.sleep(cfg["poll_interval_seconds"])
                page = await request(f"poll/{token}", {})
                polls += 1
