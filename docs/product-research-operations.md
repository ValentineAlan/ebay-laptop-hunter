# Product Research operational alerts — 0.10.53

Operational notifications use `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_ADMIN_CHAT_ID`. Public deal alerts still use `TELEGRAM_CHAT_ID`.

| Condition / transition | Private Telegram behaviour |
| --- | --- |
| Healthy searches / normal 30-second cadence | Silent |
| First challenge | One queued pause alert; automatic probe in 1 hour |
| Recovery response still challenged or signed out | One queued update per response; cooldown 2h, 4h, then 6h |
| Signed out / invalid session | Authentication-required alert with login instruction and Chromium link |
| Waiting on circuit / manual recovery signal | Silent until a real search establishes the outcome |
| Real search closes circuit | One restored alert; explicitly says whether the volume guard still prevents normal requests |
| Rolling 24h allowance exhausted beyond normal cadence | One volume-wait alert with count, limit and next eligible request time |
| Volume wait ends | One allowance-available alert; notes any still-open circuit |
| Guard waits no longer than normal cadence | Silent, avoiding pause/resume messages on each rolling slot |
| HTTP 429 | One throttle alert per episode; further 429s update durable backoff without repeated alerts |
| Three consecutive helper/response failures | One failure warning; further failures silent until recovery |
| Valid response after throttle/failure warning | One healthy-response alert; suppressed when circuit recovery already supplies that alert |
| Restart | Pending delivery resumes; existing circuit/volume states are not replayed |
| Failed Telegram delivery | Retry the same event in FIFO order, initially after 30 seconds, increasing up to one hour |

Circuit, rate and volume restrictions are independent. Probes can bypass volume
only while the circuit is open; they still obey cadence and HTTP 429 backoff.
All admitted requests, including probes and transport failures, count toward
the rolling total. The 1,700 ceiling applies to normal requests; recovery probes
can take the total above it. The loader retains the complete rolling 24 hours.

Request admission reserves telemetry before releasing its lock. Only one
request remains in flight through response validation, preventing the worker
and monitor from independently interpreting the same recovery episode.
No request mutex is held during a volume wait, so recovery remains possible.

Circuit state, recovery experiment and notification are committed together in
SQLite. Queue delivery occurs in a separate daemon and cannot block backlog
processing on Telegram network calls. Event IDs and observation times stay
constant through retries. Telegram has no idempotency key: a crash or uncertain
network result after acceptance can repeat a delivered event. This is durable
at-least-once delivery, not an exactly-once promise.

Diagnostics expose rolling count, normal-request limit, next volume slot,
pending alerts and consecutive failures. Telemetry and its report parser include
24h counts; the parser retains compatibility with older log formats.

## Validation

Run the complete suite with `python -m unittest discover -s tests -v`.
The suite contains 44 valuation tests and 27 operational tests. Tests mock
Telegram and browser responses and use temporary SQLite databases.

The previous 40 valuation tests failed during setup on unmodified `3fa86e84`
because their fixtures referred to the removed `app.VERSION`. Their fixtures
now use `CLASSIFIER_VERSION` and the current rules revision. Sold titles match
the fixture capacities, missing-spec cases remove those capacities from the
title, cache tests seed each generated query, and self-comparison uses realistic
eBay IDs. Assertions reflect the current condition adjustment, confidence,
scoring, two-comparable minimum and compatible-variant rules. Explicit variant
contradictions, stale evidence, duplicate evidence and self-comparison remain
covered. These updates change tests and documentation only; application
behaviour and release version remain unchanged.

## Hunter-only deployment

After the tested commit is merged into `main`, run on TrueNAS:

```sh
docker compose -p ix-ebay-hunter-stack \
  -f /mnt/.ix-apps/app_configs/ebay-hunter-stack/versions/1.0.0/templates/rendered/docker-compose.yaml \
  build --no-cache hunter
docker compose -p ix-ebay-hunter-stack \
  -f /mnt/.ix-apps/app_configs/ebay-hunter-stack/versions/1.0.0/templates/rendered/docker-compose.yaml \
  up -d --no-build --no-deps --force-recreate hunter
docker exec ix-ebay-hunter-stack-hunter-1 python -c \
  'import app, pr_operations; print(app.APP_VERSION); print(app.PRODUCT_RESEARCH_MIN_INTERVAL_SECONDS)'
docker logs --tail 100 ix-ebay-hunter-stack-hunter-1
```

Expected version: `0.10.53`; minimum interval: `30.0` seconds. Check diagnostics
for rolling count and alert queue. Observe naturally occurring guard or circuit
transitions and their corresponding private notifications; do not manufacture
eBay challenges to test alerting. Chromium and the SID helper are preserved.

Older releases cannot restore 12–24h timestamps already discarded by a previous
restart. The fixed guard becomes fully representative after a complete 24-hour
window of retained history. Pending notifications require this release to drain;
an older release ignores the new operational outbox.
