"""GitHub artifact checkpoint recovery. Metadata failures never start a fresh campaign."""
import argparse
import asyncio
import io
import json
import os
import zipfile
from pathlib import Path

import httpx

from .storage import atomic_write

PREFIX = "flightwatch-state-"


async def checkpoint(command, root, transport=None):
    repo, token = os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"]
    api = f"https://api.github.com/repos/{repo}"
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        timeout=60, transport=transport) as client:
        async def get(path):
            r = await client.get(api + path)
            r.raise_for_status()
            return r.json()
        artifacts = []
        page = 1
        while True:
            data = await get(f"/actions/artifacts?per_page=100&page={page}")
            artifacts.extend(a for a in data["artifacts"] if a["name"].startswith(PREFIX) and not a["expired"])
            if len(data["artifacts"]) < 100:
                break
            page += 1
        # Artifact IDs are allocated independently and need not increase with
        # creation time. Use GitHub's timestamp to recover the newest state.
        artifacts.sort(key=lambda a: (a.get("created_at", ""), a["id"]), reverse=True)
        if command == "restore":
            if not artifacts:
                runs = await get("/actions/workflows/flightwatch.yml/runs?per_page=100")
                prior = [r for r in runs["workflow_runs"] if r["id"] != int(os.environ["GITHUB_RUN_ID"])
                         and r["status"] == "completed"]
                if prior:
                    raise RuntimeError("Checkpoint missing after previous run; restore state manually")
                Path(root).mkdir(parents=True, exist_ok=True)
                atomic_write(Path(root) / "ledger.json", "{}")
                # One-time migration metadata is supplied as a GitHub Secret,
                # never as committed state or a workflow input.
                seed = json.loads(os.environ.get("INITIAL_COLLECTOR_STATE") or "{}")
                allowed = {"ledger.json", "local-start.json", "theater/latest.json"}
                if not isinstance(seed, dict) or not set(seed).issubset(allowed):
                    raise RuntimeError("Invalid initial collector state")
                for name, data in seed.items():
                    atomic_write(Path(root) / name, json.dumps(data))
                return
            # Get signed download URL; never forward GitHub token to the storage host.
            r = await client.get(api + f"/actions/artifacts/{artifacts[0]['id']}/zip", follow_redirects=False)
            if r.status_code != 302:
                r.raise_for_status()
                raise RuntimeError("Expected artifact download redirect")
            async with httpx.AsyncClient(timeout=60, transport=transport) as downloader:
                archive = await downloader.get(r.headers["location"])
                archive.raise_for_status()
            target = Path(root).resolve()
            target.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
                for member in z.infolist():
                    path = (target / member.filename).resolve()
                    if not path.is_relative_to(target) or member.file_size > 120_000_000:
                        raise RuntimeError("Unsafe checkpoint")
                z.extractall(target)
            if not (target / "ledger.json").exists():
                raise RuntimeError("Checkpoint has no campaign ledger")
            print(f"Recovered checkpoint {artifacts[0]['id']}")
        else:
            current_name = PREFIX + os.environ["GITHUB_RUN_ID"] + "-" + os.environ["GITHUB_RUN_ATTEMPT"]
            if not artifacts or artifacts[0]["name"] != current_name:
                raise RuntimeError("New checkpoint not confirmed; retain all old copies")
            # Keep one fallback. Each retained checkpoint contains the complete pending queue.
            for artifact in artifacts[2:]:
                r = await client.delete(api + f"/actions/artifacts/{artifact['id']}")
                r.raise_for_status()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["restore", "prune"])
    p.add_argument("--state", default="state")
    args = p.parse_args()
    try:
        asyncio.run(checkpoint(args.command, args.state))
    except Exception as exc:
        # URLs/exceptions may contain signed credentials: print type only.
        raise SystemExit(f"Checkpoint operation failed ({type(exc).__name__}); see README recovery steps")


if __name__ == "__main__":
    main()
