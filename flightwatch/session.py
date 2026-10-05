"""Local headed research session; browser credentials never enter scraper checkpoints."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from .core import load_config, search_url


def json_shape(value, depth=0):
    """Record a response contract without tokens, user data, or market-price claims."""
    if depth > 12:
        return "<depth limit>"
    if isinstance(value, dict):
        if len(value) > 15:
            first = next(iter(value.values()), None)
            return {"<map entry>": json_shape(first, depth + 1), "<entry count>": len(value)}
        return {key: json_shape(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return {"<array length>": len(value), "<item>": json_shape(value[0], depth + 1) if value else None}
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    return "string"


def safe_endpoint(url):
    parsed = urlsplit(url)
    path = "/".join("<id>" if len(part) > 24 or re.fullmatch(r"[0-9a-fA-F-]{16,}", part)
                    else part for part in parsed.path.split("/"))
    return {"host": parsed.hostname, "path": path}  # no query, fragments or credentials


async def research(origin, day, cfg, root, seconds):
    from playwright.async_api import async_playwright

    root = Path(root).resolve()
    # Keep credentials outside state/: that directory is uploaded by GitHub Actions.
    checkpoint = Path("state").resolve()
    if root == checkpoint or root.is_relative_to(checkpoint):
        raise ValueError("Browser profile must stay outside state/, which is uploaded to GitHub")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    contracts = root / "contracts"
    contracts.mkdir(exist_ok=True, mode=0o700)
    tasks = set()
    count = 0
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(str(root / "profile"), headless=False)
        page = await context.new_page()

        async def capture(response):
            nonlocal count
            endpoint = safe_endpoint(response.url)
            host = endpoint['host'] or ''
            if not any(host == domain or host.endswith('.' + domain)
                       for domain in ('skyscanner.com', 'skyscanner.net')):
                return
            if 'json' not in response.headers.get('content-type', '') or count >= 50:
                return
            if any(x in response.url.lower() for x in ('captcha', 'challenge', '/px/', 'analytics')):
                return
            try:
                body = await response.body()
                if len(body) > 5_000_000:
                    return
                payload = json.loads(body)
                count += 1
                content = {'endpoint': endpoint, 'method': response.request.method,
                           'http_status': response.status, 'response_shape': json_shape(payload)}
                request_body = response.request.post_data
                if request_body:
                    try:
                        content['request_shape'] = json_shape(json.loads(request_body))
                    except (ValueError, TypeError):
                        content['request_shape'] = '<not JSON>'
                target = contracts / f'response-{count:03}.json'
                target.write_text(json.dumps(content, ensure_ascii=False, indent=2))
                os.chmod(target, 0o600)
                print(f'Сохранена схема JSON-ответа {count}', flush=True)
            except Exception:
                return  # telemetry capture must not interfere with the user's browser

        def schedule(response):
            task = asyncio.create_task(capture(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        page.on('response', schedule)
        try:
            await page.goto(search_url(origin, day, cfg), wait_until='domcontentloaded', timeout=60000)
            print('Браузер открыт. Если появилась CAPTCHA, пройдите её в окне сайта. '
                  'Сессия сохранится локально. Ожидание ограничено указанным временем.', flush=True)
            await asyncio.sleep(seconds)
        finally:
            page.remove_listener('response', schedule)
            if tasks:
                await asyncio.gather(*list(tasks), return_exceptions=True)
            await context.close()
    print(f'Схем ответов: {count}. Это диагностические файлы, не снимки цен.', flush=True)


def main():
    p = argparse.ArgumentParser(description='Local browser session for inspecting an accessible real search')
    p.add_argument('--origin', default='VIE')
    p.add_argument('--date', default='2027-02-18')
    p.add_argument('--config', default='config.json')
    p.add_argument('--private-dir', default='.private/skyscanner')
    p.add_argument('--seconds', type=int, default=180)
    a = p.parse_args()
    cfg = load_config(a.config)
    if a.origin not in cfg['airports'] or a.date not in cfg['departure_dates']:
        p.error('Use a configured airport and departure date')
    if not 1 <= a.seconds <= 900:
        p.error('--seconds must be between 1 and 900')
    try:
        asyncio.run(research(a.origin, a.date, cfg, a.private_dir, a.seconds))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
