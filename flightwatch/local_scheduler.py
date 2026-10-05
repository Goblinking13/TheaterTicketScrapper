"""Small launchd tick: persisted campaign start, catch-up and process lock."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import plistlib
from pathlib import Path

from .cli import run
from .core import aware, campaign_slot, load_config, stable_key, utcnow
from .storage import atomic_write
from .theater import tick as theater_tick

LABEL = "com.flightwatch.local"


async def collect_all(cfg, state):
    """One service; each source keeps its own schedule and durable state."""
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    with (state / "collector.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        results = await asyncio.gather(tick(cfg, state), theater_tick(state / "theater"),
                                       return_exceptions=True)
        failed = False
        for name, result in zip(("Flights", "Theater"), results):
            if isinstance(result, BaseException):
                print(f"{name}: scheduler error ({type(result).__name__})", flush=True)
                failed = True
            elif result:
                failed = True
        return int(failed)


async def tick(cfg, state):
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    with (state / "local-scheduler.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        start = os.environ.get("CAMPAIGN_START") or cfg.get("campaign_start")
        if not start:
            start_file = state / "local-start.json"
            if not start_file.exists():
                atomic_write(start_file, json.dumps({"start": utcnow().isoformat()}))
            start = json.loads(start_file.read_text())["start"]
        cfg = dict(cfg, campaign_start=start)
        now = utcnow()
        slot = campaign_slot(start, now, cfg)
        provider = os.environ.get("PROVIDER") or cfg["provider"]
        pending = any((state / "pending").glob("*.json"))
        if slot is None:
            if not pending:
                return 0
            command = "deliver"
        else:
            run_id = "campaign-" + stable_key(aware(start).isoformat(), cfg, provider, slot)
            ledger_file = state / "ledger.json"
            ledger = json.loads(ledger_file.read_text()) if ledger_file.exists() else {}
            expected = {f"{o}:{d}" for o in cfg["airports"] for d in cfg["departure_dates"]}
            if expected.issubset(set(ledger.get(run_id, []))) and not pending:
                return 0
            command = "run"
        print(f"{now.isoformat()}: local scheduler {command}, slot={slot}", flush=True)
        args = argparse.Namespace(command=command, state=str(state), no_upload=False,
                                  test=False, all=False, provider=provider,
                                  origin="VIE", date="2027-02-18")
        return await run(args, cfg)


def write_plist(project, output):
    project = Path(project).resolve()
    logs = project / "state" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    data = {
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", str(project / "scripts" / "local-run.sh")],
        "WorkingDirectory": str(project),
        "RunAtLoad": True,
        "StartInterval": 300,
        "StandardOutPath": str(logs / "scheduler.log"),
        "StandardErrorPath": str(logs / "scheduler-error.log"),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(plistlib.dumps(data))
    print(f"Created {output}; no background service installed yet.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-plist")
    args = parser.parse_args()
    if args.write_plist:
        write_plist(Path(__file__).resolve().parent.parent, args.write_plist)
        return
    raise SystemExit(asyncio.run(collect_all(load_config(), "state")))


if __name__ == "__main__":
    main()
