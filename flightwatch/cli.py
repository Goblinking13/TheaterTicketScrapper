from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import httpx

from .core import aware, campaign_slot, load_config, snapshot, stable_key, utcnow
from .providers import PartnerProvider, SearchFailure, WebsiteProvider, browser_probe
from .readiness import diagnose
from .ryanair import RyanairProvider
from .storage import DeliveryFailure, Outbox, SupabaseSink, atomic_write


async def run(args, cfg):
    began = time.monotonic()
    deadline = began + cfg["run_budget_seconds"]
    outbox = Outbox(args.state, cfg["outbox_budget_bytes"])
    sink = None
    if not args.no_upload:
        try:
            sink = SupabaseSink(cfg)
        except DeliveryFailure:
            print("Supabase не подключён: снимки остаются в state/pending.")
    if sink:
        print(json.dumps(await outbox.flush(sink, deadline)))
    if args.command == "deliver":
        return 1 if outbox.files() else 0
    if args.command == "probe":
        if args.origin not in cfg["airports"] or args.date not in cfg["departure_dates"]:
            raise ValueError("Probe route/date must be configured")
        try:
            evidence = await browser_probe(args.origin, args.date, cfg)
        except Exception as exc:
            evidence = {"status": "error", "error_code": "browser_probe_failed",
                        "exception_type": type(exc).__name__}
        snap = snapshot(cfg, "probe-" + str(uuid.uuid4()), args.origin, args.date,
                        evidence["status"], error_code=evidence["error_code"], metadata=evidence)
        outbox.enqueue(snap)
        if args.output:
            atomic_write(args.output, json.dumps(snap, indent=2, ensure_ascii=False))
        print(json.dumps(evidence))
        return 2
    start = os.environ.get("CAMPAIGN_START") or cfg.get("campaign_start")
    now = utcnow()
    provider_name = args.provider or os.environ.get("PROVIDER") or cfg["provider"]
    provider = {"partner": PartnerProvider, "website": WebsiteProvider, "ryanair": RyanairProvider}[provider_name]()
    source = getattr(provider, "source", "skyscanner.com")
    def make_snapshot(run_id, origin, day, status, **kwargs):
        return snapshot(cfg, run_id, origin, day, status, source=source,
                        search_link=provider.search_link(origin, day, cfg) if hasattr(provider, "search_link") else None,
                        **kwargs)
    try:
        if args.test:
            run_id = "manual-" + str(uuid.uuid4())
            searches = [(args.origin, args.date)]
            if args.origin not in cfg["airports"] or args.date not in cfg["departure_dates"]:
                raise ValueError("Test route/date must be configured")
        elif getattr(args, "all", False):
            run_id = "manual-" + str(uuid.uuid4())
            searches = [(o, d) for o in cfg["airports"] for d in cfg["departure_dates"]]
        else:
            if not start:
                raise ValueError("Set CAMPAIGN_START once; it must not be runner startup time")
            slot = campaign_slot(start, now, cfg)
            if slot is None:
                print("Вне 21-дневной кампании; повторная доставка выполнена.")
                return 1 if outbox.files() else 0
            run_id = "campaign-" + stable_key(aware(start).isoformat(), cfg, provider_name, slot)
            searches = [(o, d) for o in cfg["airports"] for d in cfg["departure_dates"]]
        ledger_path = Path(args.state) / "ledger.json"
        ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
        completed = ledger.setdefault(run_id, [])
        circuit = None
        issues = 0
        for origin, day in searches:
            identity = f"{origin}:{day}"
            if identity in completed:
                continue
            # Recover after a crash between durable enqueue and ledger update.
            sid = stable_key(source, cfg["event_id"], run_id, origin, day)
            existing = outbox.pending / f"{sid}.json"
            if existing.exists():
                completed.append(identity)
                atomic_write(ledger_path, json.dumps(ledger))
                continue
            if outbox.full():
                print("Бюджет очереди достигнут: сбор остановлен, данные сохранены.")
                issues += 1
                break
            try:
                if circuit:
                    raise SearchFailure("skipped_after_access_block", True, {"attempted": False})
                if time.monotonic() - began >= cfg["run_budget_seconds"]:
                    raise SearchFailure("run_budget_exhausted", metadata={"attempted": False})
                rows, meta = await asyncio.wait_for(provider.search(origin, day, cfg),
                                                    cfg["search_timeout_seconds"] + 5)
                snap = make_snapshot(run_id, origin, day, "success" if rows else "no_offers",
                                offers=rows, complete=True, metadata=meta)
            except SearchFailure as exc:
                if exc.blocked:
                    circuit = True
                snap = make_snapshot(run_id, origin, day, "blocked" if exc.blocked else "error",
                                error_code=exc.code, metadata=exc.metadata)
                # Explicitly recorded source coverage is expected and must not fail every hosted job.
                if exc.code != "origin_not_in_ryanair_airport_inventory":
                    issues += 1
            except (httpx.HTTPError, asyncio.TimeoutError):
                snap = make_snapshot(run_id, origin, day, "error", error_code="network_or_timeout")
                issues += 1
            except (KeyError, ValueError, TypeError):
                snap = make_snapshot(run_id, origin, day, "error", error_code="response_schema_mismatch")
                issues += 1
            outbox.enqueue(snap)
            completed.append(identity)
            atomic_write(ledger_path, json.dumps(ledger))
            print(f"{origin} {day}: {snap['status']} ({len(snap['offers'])} offers)")
            if sink:
                report = await outbox.flush(sink, deadline)
                if report["failed"]:
                    sink = None
                    print("Доставка отложена: очередь сохранена для следующего запуска.")
            if not circuit and snap["metadata"].get("attempted") is not False:
                await asyncio.sleep(cfg["between_searches_seconds"])
        if outbox.full():
            issues += 1
        return 1 if issues or outbox.files() else 0

    finally:
        if hasattr(provider, "aclose"):
            await provider.aclose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "probe", "deliver", "estimate", "doctor"])
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--state", default="state")
    parser.add_argument("--provider", choices=["website", "partner", "ryanair"])
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--test", action="store_true", help="Manually search one configured route/date")
    scope.add_argument("--all", action="store_true", help="Manually search all configured airports/dates without CAMPAIGN_START")
    parser.add_argument("--origin", default="VIE")
    parser.add_argument("--date", default="2027-02-18")
    parser.add_argument("--no-upload", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    cfg = load_config(args.config)
    if args.command == "doctor":
        issues, notes = diagnose(cfg, scheduled=not (args.test or args.all))
        for note in notes:
            print(note)
        for issue in issues:
            print("Нужно исправить: " + issue)
        if not issues:
            print("Переменные настроены. Живой доступ к источнику этим не проверен.")
        raise SystemExit(1 if issues else 0)
    if args.command == "estimate":
        searches = 42 * len(cfg["airports"]) * len(cfg["departure_dates"])
        print("Planning assumption: 3000 bytes/offer including indexes + 4096 bytes/search")
        for n in [0, 5, 10, 25, 50, 100]:
            size = searches * (4096 + n * 3000)
            print(f"{n:3} offers/search: {searches*n:7} observations, {size/1_000_000:.1f} MB")
        return
    try:
        code = asyncio.run(run(args, cfg))
    except ValueError as exc:
        parser.error(str(exc))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
