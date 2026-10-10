"""Read-only classification audit; no listing refreshes or Telegram sends."""
from collections import Counter, defaultdict
import json
import sys
import sqlite3
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), '/app', str(Path.cwd())]
import app


def open_readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def audit(conn):
    app.refresh_runtime_settings(conn)
    app.refresh_classifier_rules(conn)
    revision = app.current_rules_revision(conn)
    counts, gaps, faults, statuses = Counter(), Counter(), Counter(), Counter()
    samples = defaultdict(list)
    for row in conn.execute('SELECT * FROM listings WHERE COALESCE(active,1)=1 AND estimated_value IS NULL'):
        counts['active_unvalued'] += 1
        if row['classifier_version'] != app.CLASSIFIER_VERSION or int(row['rules_revision'] or 0) != revision:
            counts['reanalysis_required'] += 1
            continue
        identity_ok = app.exact_spec_identity(row) or app.auction_generation_identity(row)
        groups = []
        if not identity_ok:
            counts['incomplete_identity_or_spec'] += 1
            ram, storage = app.effective_ram_storage(row)
            cpu = app.parse_cpu(row['cpu'], 'VALUATION')
            if not app.precise_model_for_valuation(row['brand'], row['model']):
                gaps['imprecise_model'] += 1
                groups.append('imprecise_model')
            if not cpu or cpu.get('confidence') != 'EXACT' or row['cpu_confidence'] != 'EXACT':
                gaps['cpu_not_exact'] += 1
                groups.append('cpu_not_exact')
            if not ram:
                gaps['missing_ram'] += 1
                groups.append('missing_ram')
            if not storage:
                gaps['missing_storage'] += 1
                groups.append('missing_storage')
            candidate = dict(row)
            title_cpu = app.parse_cpu(row['title'], 'TITLE')
            if title_cpu and title_cpu.get('confidence') == 'EXACT':
                candidate.update(cpu=title_cpu['name'], cpu_confidence='EXACT')
            if app.exact_spec_identity(candidate):
                counts['identity_recoverable_using_exact_title_cpu'] += 1
                groups.append('identity_recoverable_using_exact_title_cpu')
        elif not row['total'] or row['postage'] is None:
            counts['unknown_delivered_cost'] += 1
        elif app.valuation_condition_requires_review(row):
            counts['condition_review'] += 1
            statuses[str(row['status'])] += 1
            try:
                reasons = json.loads(row['fault_reasons'] or '[]')
            except (ValueError, TypeError):
                reasons = [str(row['fault_reasons'])]
            if not isinstance(reasons, list):
                reasons = [str(reasons)]
            faults.update(str(r) for r in reasons)
            title_level, title_reasons = app.classify_faults(row['title'], row['condition'])
            if row['status'] not in ('NORMAL', None, '') and title_level == 'NORMAL':
                counts['condition_flags_not_present_in_title'] += 1
                groups.append('condition_flags_not_present_in_title')
            if row['status'] in ('NORMAL', None, ''):
                counts['normal_status_rejected_by_ordinary_laptop_filter'] += 1
                groups.append('normal_status_rejected_by_ordinary_laptop_filter')
            groups.append('condition_review')
        for group in groups:
            if len(samples[group]) < 3:
                samples[group].append({k: row[k] for k in (
                    'item_id', 'title', 'url', 'condition', 'status', 'fault_reasons',
                    'brand', 'model', 'cpu', 'cpu_confidence', 'ram_gb', 'storage_gb', 'detail_status')})
    return {'counts': dict(counts), 'missing_identity_fields_nonexclusive': dict(gaps),
            'condition_statuses': dict(statuses), 'fault_reason_counts_nonexclusive': dict(faults),
            'samples': dict(samples),
            'notes': ['No network requests or database writes.',
                      'Faults absent from a title may be genuine faults documented in its description.',
                      'Descriptions are not retained in this database; source context requires separate verification.',
                      'Title CPU recovery is hypothetical; no stored identity or eligibility is changed.']}


if __name__ == '__main__':
    with open_readonly(app.DB) as conn:
        conn.execute('BEGIN')
        print(json.dumps(audit(conn), indent=2))
