"""Run the shared scheduler once on an ephemeral GitHub runner."""
import asyncio
import argparse
import os

from .core import aware, load_config
from .local_scheduler import collect_all
from .cli import run
from .theater import tick as theater_tick


async def verify(cfg):
    args = argparse.Namespace(command="run", state="state", no_upload=False,
                              test=True, all=False, provider="ryanair",
                              origin="VIE", date="2027-02-18")
    results = await asyncio.gather(run(args, cfg),
                                   theater_tick("state/theater", force=True),
                                   return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            print(f"Verification failed ({type(result).__name__})", flush=True)
    return int(any(isinstance(result, BaseException) or result for result in results))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    # A cloud runner must never silently restart an existing campaign.
    start = os.environ.get("CAMPAIGN_START")
    if not start:
        raise SystemExit("Configure the permanent CAMPAIGN_START repository variable")
    aware(start)
    task = verify(load_config()) if args.verify else collect_all(load_config(), "state")
    raise SystemExit(asyncio.run(task))


if __name__ == "__main__":
    main()
