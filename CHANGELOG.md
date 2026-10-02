# Changelog

Notable changes to Laptop Lander are recorded here.

## 0.10.6

### Telegram routing
- Product Research block/challenge notifications now go to the private administrator chat.
- Product Research recovery notifications now go to the private administrator chat.
- Public Telegram notifications remain reserved for qualifying laptop deals.
- Added `TELEGRAM_ADMIN_CHAT_ID`.

## 0.10.5

### Durable Product Research state
- Moved adaptive Product Research rate state into SQLite.
- Moved Product Research circuit-breaker state into SQLite.
- Learned request pacing now survives application redeploys.
- Legacy JSON state can be migrated into SQLite.
- Browser/helper IPC remains file-based.

## 0.10.3

### Product Research circuit breaker
- Added progressive cooldowns following repeated eBay Product Research challenges.
- Preserved the valuation backlog while Product Research is unavailable.
- Tightened Chromium recovery detection.
- Successful Product Research access resets challenge escalation.

## 0.10.2

### Recovery workflow
- Added Product Research block/recovery notifications.
- Added browser-assisted recovery signalling.
- Added Chromium GUI diagnostics support.

## 0.10.1

### Challenge protection
- Added a Product Research circuit breaker.
- Browse API discovery continues independently while Product Research is paused.

## 0.10.0

### Chromium Product Research
- Moved Product Research requests into the authenticated Chromium browser context.
- Added browser request/response IPC.
- Added explicit eBay anti-bot/challenge detection.

## 0.9.x

### Valuation and UI development
- Expanded sold-evidence handling.
- Improved CPU/model/specification extraction and confidence handling.
- Added fault-aware valuation behaviour.
- Added Product Research caching and reanalysis controls.
- Added Telegram deal alerts.
- Added Laptop Lander branding and dashboard improvements.

## 0.8.0

### Conservative valuation model
- Strengthened sold-comparable requirements.
- Improved deduplication and condition filtering.
- Tightened model, CPU, RAM, storage, display and GPU matching.
- Improved freshness and confidence handling.

For individual patch-level changes, see the Git commit history.
