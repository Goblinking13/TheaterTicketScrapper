"""Create/update the cron-job.org heartbeat without logging credentials."""
import argparse
import json
from pathlib import Path

import httpx


def job_payload(repo, ref, token, enabled):
    return {
        'title': f'{repo}: flights and theater',
        'url': f'https://api.github.com/repos/{repo}/actions/workflows/flightwatch.yml/dispatches',
        'enabled': enabled,
        'saveResponses': False,
        'requestMethod': 1,
        'requestTimeout': 30,
        'schedule': {'timezone': 'UTC', 'expiresAt': 0, 'hours': [-1],
                     'minutes': [17], 'mdays': [-1], 'months': [-1], 'wdays': [-1]},
        'extendedData': {
            'headers': {'Authorization': f'Bearer {token}',
                        'Accept': 'application/vnd.github+json',
                        'Content-Type': 'application/json',
                        'X-GitHub-Api-Version': '2022-11-28'},
            'body': json.dumps({'ref': ref, 'inputs': {'mode': 'tick'}}),
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--ref', default='main')
    parser.add_argument('--cron-key-file', required=True, type=Path)
    parser.add_argument('--github-token-file', required=True, type=Path)
    parser.add_argument('--enable', action='store_true')
    args = parser.parse_args()
    key = args.cron_key_file.read_text().strip()
    token = args.github_token_file.read_text().strip()
    if not key or not token:
        raise SystemExit('Both credential files must be nonempty')
    job = job_payload(args.repo, args.ref, token, args.enable)
    try:
        # Verify the dispatch credential before storing it with cron-job.org.
        with httpx.Client(timeout=30) as github:
            response = github.get(
                f'https://api.github.com/repos/{args.repo}/actions/workflows/flightwatch.yml',
                headers=job['extendedData']['headers'])
            response.raise_for_status()
        with httpx.Client(base_url='https://api.cron-job.org', timeout=30,
                          headers={'Authorization': f'Bearer {key}',
                                   'Content-Type': 'application/json'}) as cron:
            response = cron.get('/jobs')
            response.raise_for_status()
            listing = response.json()
            if listing.get('someFailed'):
                raise RuntimeError('Incomplete job list')
            matches = [j for j in listing['jobs'] if j['title'] == job['title'] and j['url'] == job['url']]
            if len(matches) > 1:
                raise RuntimeError('Multiple matching jobs; resolve manually')
            if matches:
                job_id = matches[0]['jobId']
                response = cron.patch(f'/jobs/{job_id}', json={'job': job})
            else:
                response = cron.put('/jobs', json={'job': job})
            response.raise_for_status()
            if not matches:
                job_id = response.json()['jobId']
            response = cron.get(f'/jobs/{job_id}')
            response.raise_for_status()
            details = response.json()['jobDetails']
            if any(details.get(field) != job[field] for field in ('enabled', 'url', 'requestMethod', 'schedule', 'extendedData')):
                raise RuntimeError('Saved job differs from requested configuration')
            print(f'Cron job {job_id} configured; enabled={args.enable}; hourly at :17 UTC')
    except httpx.HTTPStatusError as exc:
        raise SystemExit(f'Service rejected request: HTTP {exc.response.status_code}') from None
    except Exception as exc:
        raise SystemExit(f'Configuration failed ({type(exc).__name__}); credentials were not logged') from None


if __name__ == '__main__':
    main()
