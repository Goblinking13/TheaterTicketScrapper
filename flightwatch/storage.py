from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .core import canonical, stable_key


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


class Outbox:
    def __init__(self, root="state", budget=100_000_000):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.pending = self.root / "pending"
        self.pending.mkdir(exist_ok=True)
        self.budget = budget
        if not (self.root / "ledger.json").exists():
            atomic_write(self.root / "ledger.json", "{}")

    def enqueue(self, snapshot):
        path = self.pending / f"{snapshot['snapshot_id']}.json"
        content = canonical(snapshot)
        if path.exists():
            if path.read_text() != content:
                raise ValueError("Snapshot ID collision: refusing to overwrite durable data")
            return
        # Keep the just-collected snapshot even if it crosses the budget, then stop collection.
        atomic_write(path, content)

    def full(self):
        return sum(p.stat().st_size for p in self.pending.glob("*.json")) >= self.budget

    def files(self):
        return sorted(self.pending.glob("*.json"))

    async def flush(self, sink, deadline=None):
        delivered, failed = 0, 0
        for path in self.files():
            if deadline is not None and time.monotonic() >= deadline:
                break
            payload = json.loads(path.read_text())
            try:
                await sink.deliver(payload)
            except (httpx.HTTPError, DeliveryFailure):
                failed += 1
                # An unavailable/full database won't improve for the next payload this run.
                break
            else:
                path.unlink()  # acknowledged snapshot only; price history remains in the DB
                delivered += 1
        return {"delivered": delivered, "failed": failed, "pending": len(self.files())}


class DeliveryFailure(Exception):
    pass


class SupabaseSink:
    def __init__(self, cfg, url=None, key=None, transport=None):
        self.url = (url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
        self.key = key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        self.cfg, self.transport = cfg, transport
        if not self.key or urlparse(self.url).scheme != "https":
            raise DeliveryFailure("supabase_configuration_missing_or_invalid")

    async def deliver(self, snapshot):
        async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
            response = await client.post(self.url + "/rest/v1/rpc/flightwatch_ingest",
                headers={"apikey": self.key, "Authorization": f"Bearer {self.key}"},
                json={"p_snapshot": snapshot,
                      "p_digest": stable_key(snapshot),
                      "p_scraper_budget": self.cfg["scraper_budget_bytes"],
                      "p_database_budget": self.cfg["database_budget_bytes"]})
        # Never print the response body; it may contain provider/database secrets.
        if response.status_code != 200 or response.json().get("accepted") is not True:
            raise DeliveryFailure("snapshot_delivery_not_acknowledged")
