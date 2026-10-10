"""Read-only live deal funnel. Run inside the hunter container via stdin.

docker exec -i ix-ebay-hunter-stack-hunter-1 python - < tools/pr_funnel_report.py
"""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys

# Supports both a repository checkout and `python -` in the deployed container.
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), '/app', str(Path.cwd())]
import app


def open_readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def promotion_blockers(row):
    reasons = []
    if str(row['valuation_confidence'] or '').strip().upper() not in {'MEDIUM', 'HIGH'}:
        reasons.append('confidence below MEDIUM')
    if int(row['comparable_count'] or 0) < 3:
        reasons.append('fewer than 3 comparables')
    if not str(row['valuation_basis'] or '').startswith('SOLD_'):
        reasons.append('no SOLD valuation')
    age = app.evidence_age_days(row['valuation_evidence_at'])
    if age is None or age > app.SOLD_CACHE_MAX_AGE_DAYS:
        reasons.append('missing or expired evidence')
    saving = app.safe_float(row['undervaluation_gbp'])
    q1, q3 = app.safe_float(row['valuation_q1']), app.safe_float(row['valuation_q3'])
    spread = (q3 - q1) / 2 if q1 is not None and q3 is not None and q3 >= q1 else 0
    if saving is None or saving < max(float(app.MIN_UNDERVALUE_GBP), spread):
        reasons.append('saving below promotion margin')
    if (app.safe_float(row['undervaluation_pct']) or 0) < app.MIN_UNDERVALUE_PCT:
        reasons.append('percentage saving below Telegram threshold')
    return reasons


def report(conn, hours, limit):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    revision = app.current_rules_revision(conn)
    # Query builders normally open a connection to read rules_revision. Freeze it
    # to this read-only snapshot; no initialization, network or sender is called.
    original = app.current_rules_revision
    app.current_rules_revision = lambda conn=None: revision
    try:
        rows = conn.execute('SELECT * FROM listings WHERE COALESCE(active,1)=1').fetchall()
        recent = [r for r in rows if str(r['first_seen']) >= cutoff]
        counts = Counter()
        blockers = Counter()
        searches = {r['query_key']: r for r in conn.execute('SELECT * FROM sold_searches')}
        samples = []
        evidence = Counter()
        for row in recent:
            problem = app.product_research_problem(row, conn)
            if problem:
                counts['research blocked: ' + problem] += 1
            else:
                counts['research eligible'] += 1
                queries = app.sold_search_queries(row)
                records = [searches.get(app.research_query_key(q)) for q in queries]
                ok = [r for r in records if r is not None and r['status'] == 'OK']
                if any((r['result_count'] or 0) > 0 for r in ok):
                    counts['query history: at least one OK search with rows'] += 1
                elif ok:
                    counts['query history: OK searches, all zero rows'] += 1
                elif any(r is not None for r in records):
                    counts['query history: records exist, none OK'] += 1
                else:
                    counts['query history: no recorded searches'] += 1
                if row['estimated_value'] is None:
                    problem = app.target_valuation_problem(row, conn)
                    candidates = app.sold_candidates(conn, row)
                    trimmed = app.remove_price_outliers(candidates)
                    selected = app.select_sold_evidence(conn, row)
                    valuation = app.calculate_sold_valuation(conn, row)
                    if problem:
                        reason = problem
                    elif not candidates:
                        reason = 'no accepted sold candidates'
                    elif len(trimmed) < app.MIN_COMPARABLES:
                        reason = 'too few candidates after outlier filtering'
                    elif not selected:
                        reason = 'only relaxed specification evidence'
                    else:
                        reason = 'usable cached valuation not persisted'
                    evidence[reason] += 1
                    if len(samples) < limit:
                        stored = conn.execute(
                            'SELECT COUNT(*) FROM sold_comparables '
                            'WHERE LOWER(brand)=LOWER(?) AND LOWER(model)=LOWER(?)',
                            (row['brand'], row['model'])).fetchone()[0]
                        samples.append({'id': row['item_id'], 'title': row['title'],
                                        'valuation_basis': row['valuation_basis'],
                                        'valuation_problem': problem, 'evidence_result': reason,
                                        'stored_same_brand_model': stored,
                                        'accepted_candidates': len(candidates),
                                        'after_outliers': len(trimmed), 'selected': len(selected),
                                        'cached_valuation': valuation,
                                        'queries': queries,
                                        'searches': [dict(r) if r else None for r in records]})
            if row['valuation_research_at']:
                counts['has listing research-at timestamp'] += 1
            if row['estimated_value'] is not None:
                counts['has estimated value'] += 1
            if (row['deal_score'] or 0) > 0:
                counts['positive DealScore'] += 1
                reasons = promotion_blockers(row)
                blockers.update(reasons)
                if not reasons:
                    counts['meets promotion and Telegram saving gates'] += 1
                    counts['marked notified' if row['telegram_notified_at'] else 'not marked notified'] += 1
        scored = []
        for row in rows:
            if (row['deal_score'] or 0) > 0:
                scored.append({'id': row['item_id'], 'title': row['title'],
                               'score': row['deal_score'], 'saving': row['undervaluation_gbp'],
                               'confidence': row['valuation_confidence'], 'comparables': row['comparable_count'],
                               'blockers': promotion_blockers(row),
                               'promotion_eligible': app.promotion_eligible(row),
                               'notified_at': row['telegram_notified_at'],
                               'telegram_error': row['telegram_notify_error']})
        return {'active': len(rows), 'recent_hours': hours, 'recent_active': len(recent),
                'recent_counts': dict(counts), 'recent_scored_blockers_nonexclusive': dict(blockers),
                'recent_eligible_unvalued_evidence': dict(evidence),
                'all_active_positive_scores': scored, 'unvalued_query_samples': samples,
                'notes': ['Query history may be shared across listings and may be stale.',
                          'A notification timestamp can represent baselining, not actual delivery.',
                          'Counts overlap except the research-block and query-history categories.']}
    finally:
        app.current_rules_revision = original


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=app.DB)
    parser.add_argument('--hours', type=float, default=18)
    parser.add_argument('--samples', type=int, default=10)
    args = parser.parse_args()
    with open_readonly(args.db) as conn:
        conn.execute('BEGIN')
        print(json.dumps(report(conn, args.hours, args.samples), indent=2))
