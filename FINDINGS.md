# Vivaticket reconnaissance findings

Investigated on 4 October 2026 using visible Playwright Chromium. The initial target run observed the page for 30 seconds; a second run checked its manual seat-selection view; a final 10-second target run verified XML and original-HTML saving. No tickets were selected or reserved.

## Where the data comes from

The page uses both server-rendered HTML and an internal XML API. The original HTTP HTML response already contains the event title, performance date, venue, six price zones, reduction labels, sector availability, total prices, and a detailed breakdown of ticket prices and commissions. These values are not exclusively inserted by JavaScript. Compare `output/20261004T180632.309317Z/document-0007.html` with `rendered.html` in the same folder.

The seat map loads performance data and room geometry through XHR requests to `/wmsbackend.php`. The browser's captured `map.load.min.js` confirms the request construction and how the XML is interpreted. The observed JSON response contains `token`, `renewInSec`, and `cookieDomain`; it appears to be security/session traffic and contains no ticket information.

## Best endpoint candidate

Exact request observed on the target page:

```text
GET https://teatrodiroma.vivaticket.it/wmsbackend.php?cmd=getMapImage&perfid=14459254&roomid=tl016248&tickSystem=undefined
200 OK
Content-Type: text/xml;charset=UTF-8
```

Despite its name, `getMapImage` returns structured XML, not an image. The response contains:

- `performance`: title, genre, venue, organizer, performance time, sales window, and availability-refresh interval.
- `reductions/reduction`: full-price and reduced-price labels and their identifiers.
- `zones/zone`: zone identifiers, names, colors, availability counts, and prices.
- Each `price`: base price, presale charge, and commission, expressed in cents. The frontend associates prices with reductions by their position in the reduction list.
- `seats/chunk`: compact seat-zone and seat-status data, keyed by chunk offset.

Saved payload: `responses/20261004T180632.309317Z/0065_teatrodiroma.vivaticket.it.xml`.

The captured event is **NON POSSO NARRARE LA MIA VITA**, Teatro Argentina, **18 February 2027 at 20:00**. The HTML and XML agree on this title and time. Request `perfid=14459254` differs from the XML's `performance id="14322117"`; preserve both identifiers until their relationship is understood. The actual browser request sent the literal `tickSystem=undefined`; whether that parameter can be omitted has not been tested.

| Zone | Available in capture | Full price including commission |
|---|---:|---:|
| Platea | 63 | €48.53 |
| Palco Platea | 44 | €38.61 |
| Palco I Ordine | 57 | €38.61 |
| Palco II Ordine | 64 | €38.61 |
| Palco III Ordine | 32 | €28.68 |
| Palco IV Ordine | 42 | €28.68 |

These are a snapshot of availability on this sales channel. Platea's XML values `price="4400"`, `presale="0"`, `commission="453"` match the HTML's €48.53 total. Zero-priced subscription entries also exist; they should not automatically be interpreted as free public tickets.

## Supporting room-layout endpoint

```text
GET https://teatrodiroma.vivaticket.it/wmsbackend.php?cmd=getMapXmlGz&room=tl016248
200 OK
Content-Type: application/xml
```

Saved payload: `responses/20261004T180632.309317Z/0071_teatrodiroma.vivaticket.it.xml`.

This contains room metadata and 1,075 seat records with identifiers, indexes, coordinates, and row/seat descriptions. It describes the room layout rather than current sellable inventory. The frontend joins the performance chunks to room seats using `chunk offset + position`. Its code treats status `0` as available, subject to price and restriction checks; interpreting every other status would need further investigation.

## Other observations and limits

The browser passed through Incapsula/security traffic and a Queue-it redirect before reaching the actual HTML. Standalone anonymous API access has not been tested; a future scraper should first reproduce the successful browser session. A few transient security response bodies became unavailable during navigation; their requests and metadata remain recorded.

The manual seat-selection page (`cmd=pricesmap`) showed a login requirement. No ticket backend XHR appeared in that unauthenticated run. The captured frontend source mentions `cmd=getMapData` for interactive maps, but that endpoint was not observed or verified and is not the primary recommendation.

The final target HAR contains 84 entries. Its metadata file contains 91 response/failure records because failed requests can have both a response and a failure notification. The verified artifacts include one parsed JSON response, two XML responses, original HTML, rendered HTML, and a screenshot.

For a future scraper of a known performance, `getMapImage` is the strongest observed candidate; use `getMapXmlGz` when individual seat descriptions are needed. Event discovery, authentication requirements for other performances, and standalone endpoint access remain outside this reconnaissance implementation.
