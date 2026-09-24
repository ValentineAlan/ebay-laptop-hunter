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

Current bundled versions:

- Hunter: `0.7.9`
- SID helper: `0.1.1`

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
