"""Observe Vivaticket in Chromium; do not reserve or buy tickets."""

import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, Error as PlaywrightError

TARGET_URL = (
    "https://teatrodiroma.vivaticket.it/index.php?"
    "nvpg[sell]&cmd=prices&tcode=tl016248&pcode=14459254"
)
KEYWORDS = (
    "ticket", "price", "event", "performance", "seat", "sector", "availability",
    "prezz", "spettacol", "posti", "settori", "disponibil", "pcode", "tcode", "map",
)
LOG = logging.getLogger("vivaticket")


async def investigate(args):
    root = Path(__file__).resolve().parent
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output = root / "output" / run_id
    responses = root / "responses" / run_id
    output.mkdir(parents=True)
    responses.mkdir(parents=True)
    records = []
    pending = set()
    json_count = 0
    navigation_error = None

    async def capture(response):
        nonlocal json_count
        request = response.request
        content_type = response.headers.get("content-type", "")
        parsed_url = urlsplit(response.url)
        searchable_url = (parsed_url.path + "?" + parsed_url.query).lower()
        xhr = request.resource_type in ("xhr", "fetch")
        interesting = xhr or (
            request.resource_type not in ("image", "font", "stylesheet", "media")
            and any(word in searchable_url for word in KEYWORDS)
        )
        record = {
            "received_at": datetime.now(timezone.utc).isoformat(),
            "method": request.method,
            "url": response.url,
            "status": response.status,
            "content_type": content_type,
            "resource_type": request.resource_type,
            "interesting": interesting,
        }
        if xhr:
            record["post_data"] = request.post_data
        records.append(record)
        response_number = len(records)
        if interesting:
            LOG.info("%s %s | status=%s | type=%s | %s",
                     request.method, response.url, response.status,
                     content_type or "(missing)", request.resource_type)
        # Redirects and empty responses have no body to inspect.
        if 300 <= response.status < 400 or response.status in (204, 205, 304):
            return
        # Keep XML too: the observed ticket backend returns XML rather than JSON.
        # Try JSON even if a server labels an XHR response as text/html.
        if "json" in content_type.lower() or xhr or "text/" in content_type.lower():
            try:
                body = await response.body()
            except PlaywrightError as exc:
                record["body_error"] = str(exc)
                LOG.warning("Could not read response body: %s (%s)", response.url, exc)
                return
            if "xml" in content_type.lower():
                destination = responses / f"{response_number:04d}_{parsed_url.hostname}.xml"
                destination.write_bytes(body)
                record["xml_file"] = str(destination.relative_to(root))
                LOG.info("Saved XML: %s", destination.relative_to(root))
            if request.resource_type == "document" and "html" in content_type.lower():
                destination = output / f"document-{response_number:04d}.html"
                destination.write_bytes(body)
                record["html_file"] = str(destination.relative_to(root))
            try:
                value = json.loads(body)
            except (ValueError, UnicodeError):
                return
            json_count += 1
            host = urlsplit(response.url).hostname or "unknown"
            destination = responses / f"{json_count:04d}_{host}.json"
            destination.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            record["json_file"] = str(destination.relative_to(root))
            LOG.info("Saved JSON: %s", destination.relative_to(root))

    def capture_done(task):
        pending.discard(task)
        if not task.cancelled() and task.exception():
            LOG.warning("Capture error: %s", task.exception())

    def on_response(response):
        task = asyncio.create_task(capture(response))
        pending.add(task)
        task.add_done_callback(capture_done)

    def on_request(request):
        if request.resource_type in ("xhr", "fetch"):
            LOG.info("Request: %s %s [%s]", request.method, request.url, request.resource_type)

    def on_failed(request):
        records.append({"method": request.method, "url": request.url,
                        "resource_type": request.resource_type, "failure": request.failure})
        LOG.warning("Failed: %s %s | %s", request.method, request.url, request.failure)

    async with async_playwright() as playwright:
        LOG.info("Opening Chromium (headless=%s)", args.headless)
        browser = await playwright.chromium.launch(headless=args.headless)
        context = await browser.new_context(
            record_har_path=str(output / "network.har"),
            record_har_mode="full",
            record_har_content="embed",
            viewport={"width": 1440, "height": 1000},
        )
        # Context listeners include new tabs and frames, and run before navigation.
        context.on("request", on_request)
        context.on("response", on_response)
        context.on("requestfailed", on_failed)
        page = await context.new_page()
        try:
            LOG.info("Navigating to %s", args.url)
            try:
                await page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
            except PlaywrightError as exc:
                navigation_error = str(exc)
                LOG.warning("Navigation did not complete: %s", exc)
            LOG.info("Observing for %s seconds. You can inspect the page and open ticket details.", args.wait)
            LOG.info("The script does not click purchase or reserve controls.")
            await asyncio.sleep(args.wait)
            for index, tab in enumerate(context.pages, start=1):
                if not tab.is_closed():
                    filename = "rendered.html" if tab == page else f"rendered-tab-{index}.html"
                    (output / filename).write_text(await tab.content(), encoding="utf-8")
            await page.screenshot(path=str(output / "page.png"), full_page=True, timeout=15000)
            LOG.info("Saved rendered HTML and screenshot. Final URL: %s", page.url)
        finally:
            # Finish body reads before closing the context, which writes the HAR.
            while pending:
                results = await asyncio.gather(*list(pending), return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        LOG.warning("Capture error: %s", result)
            await context.close()
            await browser.close()
            (output / "network.json").write_text(
                json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
            (output / "run.json").write_text(json.dumps({
                "target_url": args.url, "headless": args.headless,
                "wait_seconds": args.wait, "navigation_error": navigation_error,
                "responses": len(records), "json_responses": json_count,
            }, indent=2), encoding="utf-8")
            LOG.info("Captured %s responses/failures, including %s JSON bodies", len(records), json_count)
            LOG.info("HAR and HTML: %s", output)
            LOG.info("JSON bodies: %s", responses)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=TARGET_URL, help="Page to investigate")
    parser.add_argument("--headless", action="store_true", help="Hide Chromium (visible by default)")
    parser.add_argument("--wait", type=float, default=45, help="Seconds to observe after navigation (default: 45)")
    args = parser.parse_args()
    if args.wait < 0:
        parser.error("--wait must be nonnegative")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(investigate(args))
    except KeyboardInterrupt:
        LOG.info("Stopped by user")


if __name__ == "__main__":
    main()
