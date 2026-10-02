# eBay Laptop Hunter

Self-hosted eBay UK laptop deal hunter with:

- eBay listing discovery
- CPU/model/RAM/storage extraction
- Windows 11 and USB-C capability assessment
- eBay Product Research sold-price evidence
- valuation and apparent-undervaluation scoring
- local web dashboard
- persistent Chromium session for Product Research
- automatic `ebaysid` recovery via Chrome DevTools Protocol

Current version:

- Laptop Lander / Hunter: `0.10.6`
- Chromium/Product Research helper: bundled with the application

See `CHANGELOG.md` for release history.

## Current 0.10.x architecture

Laptop Lander now separates live-listing discovery from sold-price research:

- eBay Browse API handles live-listing discovery.
- eBay Product Research runs through the persistent Chromium session.
- Product Research uses adaptive request pacing with durable SQLite state.
- Learned Product Research pacing survives redeploys.
- eBay Product Research challenges open a circuit breaker rather than repeatedly consuming the valuation backlog.
- Browse discovery continues while Product Research is paused.
- Product Research can automatically resume after recovery.
- Product Research operational alerts are sent privately to the administrator.
- Public Telegram notifications are reserved for qualifying laptop deals.
- Durable application state is stored in SQLite; browser/helper IPC remains file-based.

## Historical: valuation changes introduced in 0.8.0

This release favours fewer, better-supported valuations over optimistic bargain
scores. At least three distinct, recent sold listing IDs/titles must match an
identified model, exact CPU, RAM and storage capacity. Multi-sale rows no longer
gain extra price influence or count as independent listings. Identical titles are
conservatively deduplicated, which can also exclude legitimate repeated sales.

Known faults, missing parts, bundles, options listings and explicit refurbished/
warranty offerings are excluded from the ordinary-used valuation pool. Faulty or
incomplete targets are marked for review instead of being assigned working-laptop
values. Generation-less modern ThinkPads and unrecognised model codes are not
valued. Detachable and Surface Pro 7+ variants retain their identities. Explicit
display, dedicated-GPU, refresh-rate and HDD/eMMC title markers must also match;
an advertised OLED or RTX configuration cannot price an unspecified target.

Sold evidence must have a parseable last-sale date within 90 days and have been
collected within seven days. Queries refresh after 24 hours even when an estimate
already exists. Recency weights have a 60-day half-life; these weights use a row's
last sale, not individual transaction dates. Log-price median absolute deviation
filtering rejects gross outliers, including when most prices are identical.

Confidence uses effective listing count and price spread, and is capped at MEDIUM
because sold titles do not establish seller diversity, battery health, display
configuration or complete condition. LOW scores are capped at 45 and MEDIUM at 75.
The score uses the comparable lower quartile, while the displayed apparent saving
uses the median. Quartiles describe comparable spread, not a confidence interval.

Only current, recently observed fixed-price listings contribute to the active
asking-price reference. That reference never generates a bargain score or an
apparent saving. Auction targets do not receive a buy-now score.

Unknown postage is no longer free shipping. Browse prices must explicitly be GBP;
Product Research prices must carry GBP currency evidence (a GBP code or £ symbol).
Unrecognised Product Research price/date structures are excluded, so changes to
eBay's response format may reduce coverage until the parser is updated. Optionally
set `BUYER_POSTCODE` in `.env` for location-aware Browse shipping estimates.

This is a conservative matching release, not a calibrated specification or repair
cost model. It does not estimate display/GPU premiums, battery replacement costs,
seller warranty premiums, resale fees or net profit. Do not interpret the apparent
saving as resale profit. Buyer Protection is not added automatically: the current
data sources still need live verification of their respective fee bases.

### Upgrade

1. Stop the hunter and back up its persistent `hunter.db`.
2. Replace the application files and rebuild/start the hunter:

   ```bash
   docker compose up -d --build hunter
   ```

3. Keep the existing data/browser volumes and Product Research login. The one-time
   migration clears old estimates and expires research searches, preserving raw
   history. Existing active listings are reanalysed as they are rediscovered or
   checked for availability; research is then recollected under the API budgets.
   Expect temporarily fewer valuations and permanently fewer unsupported scores.

To roll back, stop the hunter and restore both the previous application version
and the database backup.

Run the regression tests without eBay credentials:

```bash
python -B -m unittest discover -s tests -v
```

The tests cover synthetic cases and mocked responses, not live eBay transactions.

## Architecture

The Compose stack has three services:

- `hunter` — discovery, valuation and dashboard
- `browser` — persistent LinuxServer Chromium session
- `sid-helper` — reads/refreshes the live eBay browser session via CDP

The helper shares the Chromium network namespace and connects directly to
`127.0.0.1:9222`, so the DevTools port is not exposed on the host/LAN.

## Security

**Never commit browser/session material.**

In particular, do not commit:

- `product-research.curl`
- `ebaysid.current`
- `ebaysid.refresh-request`
- `ebaysid-helper-status.json`
- HAR/cookie exports
- the Chromium profile/config directory
- `.env`
- `hunter.db` or logs

`product-research.curl` and Chromium profile data should be treated as
credentials because they can contain authenticated session information.

## Quick start

1. Copy the environment template:

   ```bash
   cp .env.example .env
   ```

2. Edit `.env` and supply your own eBay API credentials and persistent paths.

3. Build and start:

   ```bash
   docker compose build
   docker compose up -d
   ```

4. Open the dashboard:

   ```text
   http://TRUENAS-IP:8080
   ```

5. Open Chromium and sign in to eBay Seller Hub/Product Research:

   ```text
   http://TRUENAS-IP:30310
   ```

   or:

   ```text
   https://TRUENAS-IP:30311
   ```

## TrueNAS SCALE

For GitHub-based rebuilds that retain existing app secrets and persistent data,
see [TrueNAS update instructions](docs/truenas-updates.md).

For the current setup, use host paths such as:

```text
/mnt/Decathlon/appdata/ebay-laptop-hunter/data
/mnt/Decathlon/appdata/ebay-browser/config
```

You can install `docker-compose.yml` using TrueNAS **Install via YAML**,
replacing environment substitutions with values supported by your local
TrueNAS app configuration if required.

The browser profile is persistent, so recreating the containers does not
require a fresh eBay login as long as the browser config host path is kept.

## Session status

The helper writes session status into the shared data directory. The hunter
dashboard displays whether Product Research currently has a working session,
when it was last checked, and when the session was last refreshed.

The helper only exposes a short hash of the SID in status/log output; the raw
SID remains in local persistent storage.

## Useful commands

```bash
docker compose ps
docker compose logs --tail 100 hunter
docker compose logs --tail 100 sid-helper
docker compose logs --tail 100 browser
```

## Repository setup

Example:

```bash
git init
git add .
git commit -m "Initial eBay Laptop Hunter release"
git branch -M main
git remote add origin git@github.com:YOUR_USERNAME/ebay-laptop-hunter.git
git push -u origin main
```

Before the first push, run:

```bash
git status
git diff --cached
```

and verify that no credentials, cookies, session files, database files or
Chromium profile files are staged.
