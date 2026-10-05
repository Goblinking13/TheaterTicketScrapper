from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCE = "skyscanner.com"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def stable_key(*parts):
    return hashlib.sha256(canonical(parts).encode()).hexdigest()


def utcnow():
    return datetime.now(timezone.utc)


def aware(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.utcoffset() is None:
        raise ValueError("An explicit timezone offset is required")
    return dt


def load_config(path="config.json"):
    cfg = json.loads(Path(path).read_text())
    for tz in [*cfg["airports"].values(), cfg["event_timezone"]]:
        ZoneInfo(tz)
    start = aware(cfg["event_start"])
    if start.astimezone(ZoneInfo(cfg["event_timezone"])).utcoffset() != start.utcoffset():
        raise ValueError("event_start offset disagrees with event_timezone")
    for d in cfg["departure_dates"]:
        date.fromisoformat(d)
    if cfg["buffer_minutes"] < 0 or cfg["adults"] != 1 or cfg["cabin_class"] != "economy":
        raise ValueError("Invalid event buffer or unsupported passenger/cabin configuration")
    if cfg["campaign_days"] != 21 or cfg["interval_hours"] != 12:
        raise ValueError("This campaign expects 42 slots of 12 hours")
    return cfg


def campaign_slot(start, now, cfg):
    start = aware(start)
    end = start + timedelta(days=cfg["campaign_days"])
    if not start <= now < end:
        return None
    return int((now - start).total_seconds() // (cfg["interval_hours"] * 3600))


def search_url(origin, day, cfg):
    compact = date.fromisoformat(day).strftime("%y%m%d")
    return (f"https://www.skyscanner.com/transport/flights/{origin.lower()}/"
            f"{cfg['destination_sky_code']}/{compact}/?adults=1&adultsv2=1&"
            "cabinclass=economy&rtn=0&preferdirects=true&currency=EUR")


def event_fields(arrival, cfg):
    arrival = aware(arrival).astimezone(ZoneInfo(cfg["event_timezone"]))
    event = aware(cfg["event_start"])
    deadline = event - timedelta(minutes=cfg["buffer_minutes"])
    minutes = (event - arrival).total_seconds() / 60
    return {"latest_arrival": deadline.isoformat(), "eligible_for_event": arrival <= deadline,
            "minutes_to_event": minutes}


def snapshot(cfg, run_id, origin, day, status, *, offers=None, complete=False,
             error_code=None, metadata=None, observed_at=None, source=SOURCE, search_link=None):
    offers = offers or []
    if status not in {"success", "no_offers", "error", "blocked"}:
        raise ValueError("Unknown search status")
    if status in {"success", "no_offers"} and not complete:
        raise ValueError("Incomplete searches cannot be successful")
    if status == "no_offers" and offers:
        raise ValueError("no_offers cannot contain observations")
    return {"snapshot_id": stable_key(source, cfg["event_id"], run_id, origin, day),
            "run_id": run_id, "source": source, "event_id": cfg["event_id"],
            "observed_at": (observed_at or utcnow()).isoformat(), "origin": origin,
            "departure_date": day, "destination_airports": cfg["destination_airports"],
            "status": status, "complete": complete, "error_code": error_code,
            "search_url": search_link or search_url(origin, day, cfg), "metadata": metadata or {},
            "offers": offers}
