# Changelog

Notable changes to Laptop Lander are recorded here.

## 0.10.32

### Search landing pages
- Added permanent, server-rendered SEO landing pages for `/cheap-laptops`, `/used-laptops`, `/laptops-under-200` and `/laptops-under-300`.
- Each page has unique search-focused metadata, canonical URL, H1, explanatory copy and CollectionPage structured data.
- Landing pages show current qualifying LaptopLander deals from the database; budget pages enforce the stated delivered-price ceiling.
- Every page explicitly states that LaptopLander finds deals rather than selling laptops.
- Added crawlable internal links between landing pages and from the homepage, and included all new pages in the sitemap.

## 0.10.31

### SEO and public positioning
- Repositioned the homepage around finding cheap laptops and used laptop deals while making clear that LaptopLander is a deal finder, not a laptop seller.
- Added a search-focused title, meta description, canonical URL, Open Graph and social metadata.
- Added WebSite structured data and consistent public `LaptopLander` branding.
- Added `/robots.txt` and `/sitemap.xml` routes.
- Marked diagnostics, analytics and settings pages `noindex,nofollow`.
- Added canonical metadata to the privacy notice.

## 0.10.29

### Evidence freshness, rule seeding and public hardening
- Persist the newest `collected_at` timestamp from the exact SOLD evidence set used by each valuation and gate public deals/alerts on that timestamp.
- Added a case-insensitive sold-model expression index and compatibility backfill for existing valuations.
- Prioritise currently displayed SOLD deals by evidence age so refresh work is less likely to starve.
- Built-in classifier defaults are now seeded once; later user deletions survive restarts.
- Validate public valuation-evidence item IDs and return a controlled 500 fragment on evidence-rendering failures.
- Replaced detailed public infrastructure errors with a generic delayed-data notice.
- Added analytics-consent withdrawal and `_ga*` cookie cleanup.
- Removed dead dashboard/settings code and the unused `shlex` import.
- Stopped persisting the live eBay SID cookie; the app now reads only its hash from helper status.
- Hardened Product Research URL validation against malformed ports.

## 0.10.28

### Valuation freshness and privacy
- Public deal tables and live-deal polling now require recent SOLD evidence within the configured sold-cache age.
- Public valuation age is based on the newest matching sold comparable's `collected_at`, not the last research attempt.
- Removed the obsolete `product-research.curl` prerequisite from valuation processing; Chromium remains the Product Research transport.
- Google Analytics is now loaded only after explicit analytics consent.
- Added a public privacy notice and updated the Product Research pause banner.

## 0.10.27

### Fault rules, public endpoint hardening and scoring
- Added plural `hinges` and `keycaps` moderate fault rules and revision-driven reanalysis.
- Preserved SOLD/ACTIVE valuation provenance when classifier rules change.
- Cached compiled fault patterns per rules revision.
- Added application throttling and caching for public evidence/live-deal endpoints.
- Kept price-refresh scoring consistent with the sold-valuation Q1 bonus.
- Added expiry cleanup for in-memory analytics/login rate-limit keys.

## 0.10.26

### Valuation provenance, proxy handling and Product Research IPC
- Prevented active asking-price fallback valuations from generating deal economics.
- Tightened fault-negation handling so unrelated negated phrases do not hide real faults.
- Added trusted-proxy handling for application rate-limit client keys.
- Validated Product Research browser IPC requests and bounded browser fetch time.

## 0.10.25

### Operational hardening
- Protected diagnostics with administrator authentication.
- Added application-side analytics and login throttling.
- Added public dashboard caching and analytics retention.
- Treats silent Product Research parse failures as errors rather than fresh zero-result searches.
- Added startup persistent-log rotation.

## 0.10.24

### Product Research IPC
- Prevented timed-out Product Research IPC requests from deleting a newer request file.
- Added matching request-ID checks to helper cleanup.

## 0.10.23

### Deal economics and promotion
- Recomputed deal economics when listing prices change.
- Tightened fault phrase matching and negation handling.
- Allowed refurbished laptops through ordinary-laptop filtering.
- Added stricter promotion eligibility for hero placement and Telegram alerts.

## 0.10.22

### Reanalysis valuation recovery
- Recalculate valuation immediately after classifier reanalysis.
- Recover valuations blanked by the earlier reanalysis path.

## 0.10.21

### Reanalysis priority
- Prioritised visible deals, auctions and stale classifier rows in the reanalysis queue.

## 0.10.20

### Windows 11 classification
- Classifies supported older Intel Core-i families as unofficial Windows 11 candidates even when benchmark data is absent or below the former performance gate.
- Added the `Win11 unofficial` dashboard badge and caveat wording.

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
