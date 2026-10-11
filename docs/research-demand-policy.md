# Product Research demand policy — 0.10.61

The 1,700 normal-request rolling allowance and 30-second minimum cadence are unchanged. These changes reduce repeat work and make fresh listings compete with each other instead of sitting behind the entire old backlog.

## Scheduling

The continuous worker performs one logical query per scheduling turn. Four turns prefer listings first seen within the previous 24 hours; the fifth prefers older listings. Either pool falls back to the other when it has no due work. The persistent turn counter survives restarts and resets each UTC day. Within each pool, existing oldest-attempt ordering is retained. A query may require two page requests, so this is a four-to-one scheduling ratio, not an exact allocation of HTTP requests.

## Retry policy

Successful queries with zero usable rows remain fresh for at least seven days. Successful queries with rows retain the configured normal cache lifetime (24 hours by default). Transport/authentication failures are not treated as successful zero-result searches.

An unvalued listing whose complete query ladder has been searched without a supported valuation waits one day after its first unsuccessful pass, three days after the second, and seven days after subsequent passes. A partial pass stopped by the per-cycle budget does not advance that counter. Merely waiting on shared cache entries does not advance it either: the worker waits until the earliest shared query expiry.

Changes to the listing's identity, advertised specifications, title or rules revision invalidate its listing retry delay. Price changes alone do not invalidate search evidence. Query-level cache reuse still applies. Newly shared supported evidence is applied even during a listing retry delay, and a persisted valuation clears the retry state. Existing condition, evidence-quality and promotion gates remain in force.

## Model repair

Explicit Dell Vostro/Inspiron screen-size prefixes are removed without losing the four-digit model number. Generation-qualified ThinkPads retain Gen2/G2 forms despite older persisted recognition patterns. Lenovo V14/V15 generation identities, Alienware 16X Aurora AC platform codes, and MSI GS Stealth generation/suffix codes are recognised; generic family names remain blocked.

A one-time migration queues only affected active listings for detail reanalysis. Cached sold rows with an explicit recoverable model in their title are repaired too, preserving their original evidence age. Affected listings must finish reanalysis before valuation eligibility returns. This is not a promise that any repaired listing qualifies as a deal.

## Demand measurement

Diagnostics and `tools/pr_funnel_report.py` expose UTC-day request counts from deployment onward:

- Total admitted requests, including recovery probes and requests that subsequently fail.
- First-page requests for previously unseen queries versus repeat queries.
- Additional page requests.
- Requests spent on listings first seen within the preceding 24 hours.
- Valuations produced (newly saved or using a new evidence timestamp).
- Due listing/query counts and retry-deferred listing counts, sampled approximately every five minutes while the worker is processing normally.

Counters are not reconstructed from old cache rows. The first day is partial. “New queries” means queries first requested, including previously unsearched old backlog, not only newly discovered listings. Fresh-listing request counts separate that work. These measure work performed; remaining query counts are potential work and can overstate actual requests because research stops once sufficient evidence is found. Request detail is retained for 30 days. Backlog snapshots include their observation timestamp and can be stale while the worker waits on the circuit or volume guard.

## Validation and deployment

113 tests pass, including scheduling fallback and persistent turns, seven-day negative caching, retry escalation/reset, partial passes, shared-cache waits, cached evidence during deferral, model repair, request accounting and diagnostics rendering. Python compilation and diff checks pass.

On TrueNAS:

```sh
cd /mnt/Decathlon/appdata/ebay-laptop-hunter-git &&
git pull --ff-only &&
python -m py_compile app.py pr_operations.py &&
./redeploy.sh
```

Expected running application version: `0.10.61`. No admin Telegram messages are added for ordinary scheduling or retry waits.

## Additional efficiencies — 0.10.61

A full first page is previewed against the real target using the same stored-row representation and complete valuation checks. Further pages are skipped only when the combined evidence supports a MEDIUM/HIGH valuation with at least three selected comparables. LOW confidence, insufficient evidence and duplicate-only pages retain pagination. The preview restores the previous cache, including on exceptions, before any further request; no write transaction spans a network/cadence wait. Direct searches without a target retain their configured pagination.

Completed successful searches contribute model-specific query-tier outcomes: trials, supported valuations and page requests. Errors and cached-only reuse do not train the ordering. Specific-first order remains until every competing tier has at least three completed observations; tiers then rank by supported valuations per request, with original order retained for ties. This uses ordinary research outcomes, not additional exploratory calls. Evidence filters and early-stop requirements are unchanged.

The worker no longer initiates research solely for a listing requiring condition review under the current valuation rules. Cosmetic-only listings that can be valued still receive research. Matching ordinary listings can collect shared evidence for the same model. Structural query generation remains available to diagnostic callers; the funnel report separately labels condition-review network suppression and excludes that work from due-query counts. Once classification/condition rules make a listing assessable, normal scheduling applies.

Tests cover first-page sufficiency, LOW-confidence pagination, duplicates, rollback and write-lock release, learned ordering thresholds and model isolation, transport-error exclusion, page accounting, cosmetic eligibility and shared ordinary-listing work.
