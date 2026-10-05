"""Run the shared scheduler once on an ephemeral GitHub runner."""
import asyncio
import os

from .core import aware, load_config
from .local_scheduler import collect_all


def main():
    # A cloud runner must never silently restart an existing campaign.
    start = os.environ.get("CAMPAIGN_START")
    if not start:
        raise SystemExit("Configure the permanent CAMPAIGN_START repository variable")
    aware(start)
    raise SystemExit(asyncio.run(collect_all(load_config(), "state")))


if __name__ == "__main__":
    main()
