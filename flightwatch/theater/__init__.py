"""Hourly Vivaticket observations with a durable, idempotent upload queue."""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from pathlib import Path

from ..core import utcnow
from ..storage import atomic_write
from .scraper import TARGET_URL, investigate
from .sync import extract_snapshot, supabase_settings, upload_snapshot


async def flush(pending, settings):
    for path in sorted(pending.glob('*.json'), key=lambda p: int(p.stem)):
        try:
            snapshot = json.loads(path.read_text())
            await asyncio.to_thread(upload_snapshot, snapshot, *settings)
        except Exception as exc:
            print(f"Theater: upload deferred ({type(exc).__name__}); snapshot saved", flush=True)
            return 1
        path.unlink()
        print(f"Theater: Supabase confirmed upload, id={snapshot['id']}", flush=True)
    return 0


async def tick(state, *, now=None, force=False):
    state = Path(state)
    pending = state / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    now = now or utcnow()
    slot = int(now.timestamp() // 3600)
    marker = state / "latest.json"
    latest = json.loads(marker.read_text()) if marker.exists() else {}
    try:
        settings = supabase_settings()
    except ValueError:
        print("Theater: configure THEATER_SUPABASE_URL / THEATER_SUPABASE_KEY", flush=True)
        return 1
    queued = pending / f"{slot}.json"
    if queued.exists():
        latest = {"slot": slot, "captured_at": now.isoformat()}
        atomic_write(marker, json.dumps(latest))
    delivery_failed = await flush(pending, settings)
    if (force or latest.get("slot") != slot) and not queued.exists():
        # Stop collection if uploads remain unavailable for an extended period.
        if sum(p.stat().st_size for p in pending.glob('*.json')) >= 10_000_000:
            print("Theater: queue budget reached; retrying uploads only", flush=True)
        else:
            captures = state / "captures"
            args = argparse.Namespace(url=TARGET_URL, wait=45, headless=True,
                                      capture_root=captures)
            try:
                output = await asyncio.wait_for(investigate(args), timeout=180)
                snapshot = extract_snapshot(output, root=captures)
                atomic_write(queued, json.dumps(snapshot, ensure_ascii=False))
                print(f"Theater {now.isoformat()}: collected, available={snapshot['available_total']}", flush=True)
            except Exception as exc:
                # Log the exception class only: request details can contain session tokens.
                print(f"Theater: collection failed ({type(exc).__name__}); will retry", flush=True)
                return 1
            finally:
                shutil.rmtree(captures, ignore_errors=True)
    if queued.exists():
        # Recover a crash between durable enqueue and updating the schedule marker.
        atomic_write(marker, json.dumps({"slot": slot, "captured_at": now.isoformat()}))
    return 1 if delivery_failed else await flush(pending, settings)
