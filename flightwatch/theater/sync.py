"""Collect a fresh Vivaticket snapshot and upload it to Supabase."""

import argparse
import asyncio
import json
import logging
import os
import ssl
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import certifi

from .scraper import TARGET_URL, investigate

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("sync")


def extract_snapshot(output, root=None):
    """Use only the successful performance XML from this specific browser run."""
    root = Path(root) if root is not None else ROOT
    run = json.loads((output / "run.json").read_text(encoding="utf-8"))
    query = parse_qs(urlsplit(run["target_url"]).query)
    requested_id = query.get("pcode", [None])[0]
    room_code = query.get("tcode", [None])[0]
    if not requested_id or not room_code:
        raise ValueError("URL must contain pcode and tcode")
    records = json.loads((output / "network.json").read_text(encoding="utf-8"))
    for record in reversed(records):
        endpoint = urlsplit(record["url"])
        params = parse_qs(endpoint.query)
        if (endpoint.hostname != urlsplit(run["target_url"]).hostname
                or endpoint.path != "/wmsbackend.php"
                or params.get("cmd") != ["getMapImage"]
                or params.get("perfid") != [requested_id]
                or params.get("roomid") != [room_code]
                or record.get("status") != 200 or not record.get("xml_file")):
            continue
        performance = ET.parse(root / record["xml_file"]).getroot().find("performance")
        if performance is None or performance.get("rcode") != room_code:
            raise ValueError("Performance XML is missing or belongs to a different room")
        reductions = performance.findall("reductions/reduction")
        zones = []
        for zone in performance.findall("zones/zone"):
            prices = zone.findall("price")
            if not reductions or len(prices) != len(reductions):
                raise ValueError("Cannot safely associate prices with reductions")
            tariffs = []
            for reduction, price in zip(reductions, prices):
                amounts = {f"{field}_cents": int(price.attrib[field])
                           for field in ("price", "presale", "commission")}
                if any(amount < 0 for amount in amounts.values()):
                    raise ValueError("Negative ticket price in XML")
                tariffs.append({
                    "reduction_id": reduction.attrib["id"],
                    "label": reduction.attrib["description"],
                    "is_subscription": reduction.attrib["id"] == "SUBSTICKET",
                    **amounts,
                    "total_cents": sum(amounts.values()),
                })
            available = int(zone.attrib["avail"])
            if available < 0:
                raise ValueError("Negative availability in XML")
            zones.append({"id": zone.attrib["id"], "code": zone.attrib["code"],
                          "name": zone.attrib["name"], "available": available,
                          "tariffs": tariffs})
        if not zones:
            raise ValueError("No price zones found; refusing to send empty data")
        starts_at = datetime.strptime(performance.attrib["timeStart"], "%Y%m%d%H%M")
        starts_at = starts_at.replace(tzinfo=ZoneInfo("Europe/Rome"))
        # Older reconnaissance captures have no received_at; use their run timestamp.
        captured_at = record.get("received_at") or datetime.strptime(
            output.name, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc).isoformat()
        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, output.name + ":" + run["target_url"])),
            "captured_at": captured_at,
            "source_url": run["target_url"],
            "request_performance_id": requested_id,
            "performance_id": performance.attrib["id"],
            "room_code": room_code,
            "event_title": performance.attrib["title"],
            "venue": performance.attrib["roomName"],
            "starts_at": starts_at.isoformat(),
            "currency": "EUR",
            "available_total": sum(zone["available"] for zone in zones),
            "zones": zones,
        }
    raise ValueError("No fresh performance XML captured. Try --wait 60 and inspect the browser; no data was uploaded.")


def supabase_settings():
    url = (os.getenv("THEATER_SUPABASE_URL") or os.getenv("SUPABASE_URL", "")).strip().rstrip("/")
    key = (os.getenv("THEATER_SUPABASE_KEY") or os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")).strip()
    if not url or not key or "REPLACE_ME" in key or "your-project-ref" in url:
        raise ValueError("Fill SUPABASE_URL and SUPABASE_SECRET_KEY in .env first (see .env.example)")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("SUPABASE_URL must be an HTTPS project origin, without a path or credentials")
    if not (key.startswith("sb_secret_") or key.startswith("eyJ")):
        raise ValueError("Use a secret key or legacy service_role key, not a publishable/anon key")
    return url, key


def upload_snapshot(snapshot, url, key):
    headers = {"apikey": key, "Content-Type": "application/json",
               "Prefer": "resolution=merge-duplicates,return=minimal"}
    # New sb_secret keys belong only in apikey; legacy JWTs also use Bearer auth.
    if not key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {key}"
    request = Request(url + "/rest/v1/ticket_snapshots?on_conflict=id",
                      data=json.dumps(snapshot, ensure_ascii=False).encode("utf-8"),
                      headers=headers, method="POST")
    try:
        with urlopen(request, timeout=30,
                     context=ssl.create_default_context(cafile=certifi.where())) as response:
            if response.status not in (200, 201, 204):
                raise ValueError(f"Unexpected Supabase status: {response.status}")
    except HTTPError as exc:
        # Do not print keys, request headers, or a remote response that could echo them.
        hints = {401: "check the API key", 403: "check the service_role key and table grants",
                 404: "run supabase.sql and check the project URL",
                 400: "check that the table matches supabase.sql"}
        raise ValueError(f"Supabase HTTP {exc.code}: {hints.get(exc.code, 'check Supabase API logs')}") from None
    except URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise ValueError("Supabase TLS certificate could not be verified; update certifi. The snapshot is saved locally.") from None
        raise ValueError("Could not connect to Supabase. The snapshot is saved locally for retry.") from None
    LOG.info("Supabase confirmed upload: ticket_snapshots, id=%s", snapshot["id"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=TARGET_URL)
    parser.add_argument("--wait", type=float, default=20, help="Browser observation time in seconds")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Collect and save without uploading")
    parser.add_argument("--upload-file", type=Path, help="Retry a saved snapshot.json without opening Chromium")
    args = parser.parse_args()
    if args.wait < 0:
        parser.error("--wait must be nonnegative")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        settings = supabase_settings() if not args.dry_run else None
        if args.upload_file:
            snapshot = json.loads(args.upload_file.read_text(encoding="utf-8"))
        else:
            output = asyncio.run(investigate(args))
            snapshot = extract_snapshot(output)
            destination = output / "snapshot.json"
            destination.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            LOG.info("Saved structured snapshot: %s", destination)
        LOG.info("%s | %s | %s | zones=%s | available=%s",
                 snapshot["event_title"], snapshot["venue"], snapshot["starts_at"],
                 len(snapshot["zones"]), snapshot["available_total"])
        if settings:
            upload_snapshot(snapshot, *settings)
        else:
            LOG.info("Dry run complete; upload disabled")
    except (ValueError, KeyError, OSError, ET.ParseError) as exc:
        LOG.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOG.info("Stopped by user")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
