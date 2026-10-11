import contextlib
import io
import json
import sqlite3
import sys
import uuid
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class ValuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp_path = Path(__file__).resolve().parent / ('.tmp-' + uuid.uuid4().hex)
        self.tmp_path.mkdir()
        self.addCleanup(self.cleanup_db)
        self.db_patch = patch.object(app, 'DB', str(self.tmp_path / 'hunter.db'))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        app.init_db()
        self.conn = app.connect_db()
        self.target = dict(item_id='target', title='Dell Latitude 5420 i5-1135G7 8GB RAM 256GB SSD',
            brand='Dell', model='Latitude 5420', cpu='i5-1135G7', cpu_confidence='EXACT',
            cpu_generation=11, ram_gb=8, storage_gb=256, total=150., postage=0.,
            condition='Used', status='NORMAL', classifier_version=app.CLASSIFIER_VERSION,
            rules_revision=app.current_rules_revision(self.conn),
            buying_options=json.dumps(['FIXED_PRICE']))

    def tearDown(self):
        self.conn.close()

    def cleanup_db(self):
        # Only remove files inside this test's explicitly-created directory.
        for file in self.tmp_path.iterdir():
            file.unlink()
        self.tmp_path.rmdir()

    def sold(self, ident, price=400, **changes):
        row = dict(query_key=app.research_query_key('Dell Latitude 5420'), item_id=str(ident),
            title=self.target['title'] + ' listing ' + str(ident), brand='Dell', model='Latitude 5420',
            cpu='i5-1135G7', cpu_generation=11, ram_gb=8, storage_gb=256,
            avg_sold_price=price, avg_postage=0, delivered_price=price, units_sold=1,
            last_sold=app.iso_now(), collected_at=app.iso_now(), currency='GBP',
            evidence_version=app.SOLD_EVIDENCE_VERSION)
        row.update(changes)
        if 'title' not in changes:
            # Keep advertised specs consistent with each test's stored specs.
            # Production deliberately prefers explicit title capacities.
            row['title'] = (f"{row['brand']} {row['model']} {row['cpu']} "
                            f"{row['ram_gb']}GB RAM {row['storage_gb']}GB SSD listing {ident}")
        self.insert('sold_comparables', row)

    def insert(self, table, row):
        self.conn.execute(f'INSERT INTO {table} ({",".join(row)}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))
        self.conn.commit()

    def active(self, ident, **changes):
        row = dict(self.target, item_id=str(ident), title=self.target['title'] + ' listing ' + str(ident),
                   first_seen=app.iso_now(), last_seen=app.iso_now(), active=1, total=500)
        row.update(changes)
        self.insert('listings', row)

    def pool(self, n=3):
        for i in range(n):
            self.sold(i, 390+i*10)

    def test_zero_result_cache_is_seven_days_but_positive_cache_is_one(self):
        for key, rows in [('empty', 0), ('positive', 2), ('error', 0)]:
            self.insert('sold_searches', dict(query_key=key, keywords=key,
                searched_at=(app.utcnow()-timedelta(days=2)).isoformat(),
                status='ERROR' if key == 'error' else 'OK', result_count=rows))
        self.assertTrue(app.sold_search_is_fresh(self.conn, 'empty'))
        self.assertFalse(app.sold_search_is_fresh(self.conn, 'positive'))
        self.assertFalse(app.sold_search_is_fresh(self.conn, 'error'))
        self.conn.execute("UPDATE sold_searches SET searched_at=? WHERE query_key='empty'",
                          ((app.utcnow()-timedelta(days=7)).isoformat(),))
        self.assertFalse(app.sold_search_is_fresh(self.conn, 'empty'))

    def test_insufficient_retries_escalate_and_spec_change_resets(self):
        self.active('retry')
        now = app.utcnow()
        with patch.object(app, 'utcnow', return_value=now):
            for count, days in [(1, 1), (2, 3), (3, 7), (4, 7)]:
                row = self.conn.execute("SELECT * FROM listings WHERE item_id='retry'").fetchone()
                app.defer_insufficient_research(self.conn, row)
                row = self.conn.execute("SELECT * FROM listings WHERE item_id='retry'").fetchone()
                self.assertEqual(row['valuation_retry_count'], count)
                self.assertEqual(row['valuation_retry_at'], (now+timedelta(days=days)).isoformat())
                self.assertTrue(app.research_retry_deferred(row))
            changed = dict(row, cpu='i7-1165G7')
            self.assertFalse(app.research_retry_deferred(changed))
            app.defer_insufficient_research(self.conn, changed)
            self.assertEqual(self.conn.execute("SELECT valuation_retry_count FROM listings").fetchone()[0], 1)
            self.assertTrue(app.research_retry_deferred(dict(row, total=1)))

    def test_partial_research_does_not_defer_until_queries_complete(self):
        self.active('partial')
        with patch.object(app, 'product_research_search', return_value=[]) as search:
            for i in range(4):
                app.collect_needed_sold_data(self.conn, maximum=1)
                row = self.conn.execute("SELECT * FROM listings WHERE item_id='partial'").fetchone()
                self.assertEqual(row['valuation_retry_count'], 1 if i == 3 else 0)
            self.assertEqual(search.call_count, 4)
            app.collect_needed_sold_data(self.conn, maximum=1)
            self.assertEqual(search.call_count, 4)

    def test_shared_cache_wait_does_not_escalate_failure_count(self):
        self.active('wait')
        initial = app.utcnow()
        with patch.object(app, 'utcnow', return_value=initial), patch.object(app, 'product_research_search', return_value=[]):
            app.collect_needed_sold_data(self.conn, maximum=4)
        later = initial + timedelta(days=2)
        with patch.object(app, 'utcnow', return_value=later), patch.object(app, 'collect_sold_search') as search:
            app.collect_needed_sold_data(self.conn, maximum=4)
            search.assert_not_called()
        row = self.conn.execute("SELECT * FROM listings WHERE item_id='wait'").fetchone()
        self.assertEqual(row['valuation_retry_count'], 1)
        self.assertEqual(row['valuation_retry_at'], (initial+timedelta(days=7)).isoformat())

    def test_scheduler_reserves_one_in_five_turns_for_old_work(self):
        fresh = dict(first_seen=app.iso_now(), item_id='fresh')
        old = dict(first_seen=(app.utcnow()-timedelta(days=2)).isoformat(), item_id='old')
        self.assertEqual([app.research_schedule([old, fresh], i)[0]['item_id']
                          for i in range(5)], ['fresh']*4+['old'])
        self.assertEqual(app.research_schedule([old], 0), [old])
        self.assertEqual(app.research_schedule([fresh], 4), [fresh])

    def test_worker_uses_persisted_backlog_turn(self):
        self.active('old', first_seen=(app.utcnow()-timedelta(days=2)).isoformat(),
                    model='Latitude 5430', cpu='i5-1245U', title='Dell Latitude 5430 i5-1245U 8GB 256GB')
        self.active('fresh')
        with patch.object(app, 'collect_sold_search', return_value=0) as search:
            app.collect_needed_sold_data(self.conn, maximum=1)
            self.assertIn('5420', search.call_args.args[1])
            self.conn.execute('UPDATE research_daily SET scheduler_turn=4')
            self.conn.commit()
            search.reset_mock()
            app.collect_needed_sold_data(self.conn, maximum=1)
            self.assertIn('5430', search.call_args.args[1])
            self.assertEqual(self.conn.execute('SELECT scheduler_turn FROM research_daily').fetchone()[0], 5)

    def test_daily_backlog_deduplicates_shared_queries_and_excludes_deferred(self):
        self.active('a')
        self.active('b')
        rows = self.conn.execute('SELECT * FROM listings').fetchall()
        app.research_demand_snapshot(self.conn, rows)
        daily = self.conn.execute('SELECT * FROM research_daily').fetchone()
        self.assertEqual(daily['backlog_listings'], 2)
        self.assertEqual(daily['backlog_queries'], 4)
        for row in rows:
            app.defer_insufficient_research(self.conn, row)
        self.conn.commit()
        app.research_demand_snapshot(self.conn, self.conn.execute('SELECT * FROM listings').fetchall())
        daily = self.conn.execute('SELECT * FROM research_daily').fetchone()
        self.assertEqual(daily['backlog_queries'], 0)
        self.assertEqual(daily['retry_deferred'], 2)

    def test_request_metrics_distinguish_new_repeat_page_and_probe(self):
        url = 'https://www.ebay.co.uk/sh/research/api/search?keywords=Dell+Latitude+5420&offset='
        app._product_research_response_context.repeat_query = False
        self.addCleanup(setattr, app._product_research_response_context, 'repeat_query', None)
        app._product_research_response_context.listing_first_seen = app.iso_now()
        self.addCleanup(setattr, app._product_research_response_context, 'listing_first_seen', None)
        app.record_research_request(url+'0')
        app.record_research_request(url+'50')
        app._product_research_response_context.repeat_query = True
        app._product_research_response_context.listing_first_seen = (app.utcnow()-timedelta(days=2)).isoformat()
        app.record_research_request(url+'0')
        app.record_research_request(url+'0', recovery_probe=True)
        rows = self.conn.execute('SELECT * FROM research_requests ORDER BY id').fetchall()
        self.assertEqual([(r['repeat_query'], r['page_offset'], r['recovery_probe']) for r in rows],
                         [(0, 0, 0), (0, 50, 0), (1, 0, 0), (1, 0, 1)])
        self.assertEqual([r['fresh_listing'] for r in rows], [1, 1, 0, 0])

    def test_demand_metrics_render_in_diagnostics(self):
        self.conn.execute("INSERT INTO research_daily(day,valuations_produced,backlog_queries,retry_deferred) VALUES (?,2,4,3)", (app.utc_day(),))
        self.conn.execute("INSERT INTO research_requests(requested_at,query_key,page_offset,repeat_query,recovery_probe) VALUES (?,'test',0,0,0)", (app.iso_now(),))
        self.conn.commit()
        with patch.object(app.urllib.request, 'urlopen', side_effect=RuntimeError('no network')):
            rendered = app.diagnostics_html()
        self.assertIn('new queries 1', rendered)
        self.assertIn('valuations 2', rendered)
        self.assertIn('due queries 4', rendered)

    def test_cached_success_clears_deferral_and_counts_once(self):
        self.pool()
        self.active('cached')
        row = self.conn.execute("SELECT * FROM listings WHERE item_id='cached'").fetchone()
        app.defer_insufficient_research(self.conn, row)
        value = app.calculate_sold_valuation(self.conn, row)
        app.persist_listing_valuation(self.conn, row, value)
        row = self.conn.execute("SELECT * FROM listings WHERE item_id='cached'").fetchone()
        self.assertIsNone(row['valuation_retry_at'])
        app.persist_listing_valuation(self.conn, row, value)
        self.assertEqual(self.conn.execute('SELECT valuations_produced FROM research_daily').fetchone()[0], 1)

    def test_cached_success_is_applied_during_retry_wait_without_network(self):
        self.active('cached')
        row = self.conn.execute("SELECT * FROM listings WHERE item_id='cached'").fetchone()
        app.defer_insufficient_research(self.conn, row)
        self.conn.commit()
        self.pool()
        with patch.object(app, 'collect_sold_search') as search:
            app.collect_needed_sold_data(self.conn, maximum=1)
            search.assert_not_called()
        row = self.conn.execute("SELECT * FROM listings WHERE item_id='cached'").fetchone()
        self.assertIsNotNone(row['estimated_value'])
        self.assertIsNone(row['valuation_retry_at'])

    def test_specific_models_bypass_old_persisted_patterns(self):
        cases = [
            ('Dell Vostro 15 3515 AMD Ryzen 5 3450U', 'Vostro 3515'),
            ('Lenovo Laptop V14 G3 i5-1235U', 'V14 G3'),
            ('Lenovo ThinkPad P14s Gen2 i5-1135G7', 'ThinkPad P14s Gen 2'),
            ('Lenovo ThinkPad P15s Gen2 i7-1165G7', 'ThinkPad P15s Gen 2'),
            ('MSI GS63 Stealth 8RE i7-8750H', 'GS63 Stealth 8RE'),
            ('Alienware 16X Aurora AC16251', 'Alienware 16X Aurora AC16251')]
        with patch.object(app, 'classifier_rule_values', return_value=[r'\bThinkPad\s+P14s\b']):
            for title, expected in cases:
                model = app.identify_model(title, {})
                self.assertEqual(model, expected)
                self.assertTrue(app.precise_model_for_valuation(app.identify_brand(title, {}), model))
        for brand, model in [('Dell', 'Vostro'), ('Lenovo', 'V14'), ('ASUS', 'VivoBook'), ('HP', 'Victus 15')]:
            self.assertFalse(app.precise_model_for_valuation(brand, model))

    def test_targeted_model_migration_is_idempotent(self):
        self.active('vostro', title='Dell Vostro 15 3515 AMD Ryzen 5 3450U 16GB 256GB', model='Vostro')
        self.active('good')
        self.sold('vostro-sold', title='Dell Vostro 15 3515 Ryzen 5 3450U 16GB 256GB', model='Vostro')
        app.repair_v01060_model_identities(self.conn)
        row = self.conn.execute("SELECT * FROM listings WHERE item_id='vostro'").fetchone()
        self.assertEqual(row['model'], 'Vostro 3515')
        self.assertEqual(row['valuation_basis'], 'REANALYSIS_REQUIRED')
        self.assertIsNone(row['classifier_version'])
        self.assertEqual(self.conn.execute("SELECT model FROM sold_comparables WHERE item_id='vostro-sold'").fetchone()[0], 'Vostro 3515')
        app.repair_v01060_model_identities(self.conn)
        self.assertEqual(self.conn.execute("SELECT classifier_version FROM listings WHERE item_id='good'").fetchone()[0], app.CLASSIFIER_VERSION)

    def preview_results(self, count, title=None):
        return [self.research_result(listing={'itemId': str(900000000100+i),
                'title': (title or self.target['title']) + ' listing ' + str(i), 'condition': 'Used'},
                avgsalesprice=f'£{390+i*10}.00') for i in range(count)]

    def test_supported_first_page_skips_second_page(self):
        app._product_research_response_context.research_target = self.target
        self.addCleanup(setattr, app._product_research_response_context, 'research_target', None)
        with patch.object(app, 'PRODUCT_RESEARCH_LIMIT', 5), patch.object(app, 'product_research_search', return_value=self.preview_results(5)) as search:
            app.collect_sold_search(self.conn, 'Dell Latitude 5420')
        self.assertEqual(search.call_count, 1)
        self.assertEqual(self.conn.execute('SELECT page_count FROM sold_searches').fetchone()[0], 1)
        self.assertIsNotNone(app.calculate_sold_valuation(self.conn, self.target))

    def test_insufficient_first_page_fetches_second_without_write_lock(self):
        app._product_research_response_context.research_target = self.target
        self.addCleanup(setattr, app._product_research_response_context, 'research_target', None)
        def search(query, offset):
            self.assertFalse(self.conn.in_transaction)
            if offset == 0:
                return self.preview_results(3, 'Dell Latitude 5430 i5-1245U 8GB RAM 256GB SSD')
            return self.preview_results(3)
        with patch.object(app, 'PRODUCT_RESEARCH_LIMIT', 3), patch.object(app, 'product_research_search', side_effect=search) as fetch:
            app.collect_sold_search(self.conn, 'Dell Latitude 5420')
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(self.conn.execute('SELECT page_count FROM sold_searches').fetchone()[0], 2)

    def test_low_confidence_first_page_keeps_second_page(self):
        app._product_research_response_context.research_target = self.target
        self.addCleanup(setattr, app._product_research_response_context, 'research_target', None)
        with patch.object(app, 'PRODUCT_RESEARCH_LIMIT', 3), patch.object(app, 'product_research_search', return_value=self.preview_results(3)) as search:
            app.collect_sold_search(self.conn, 'Dell Latitude 5420')
        self.assertEqual(search.call_count, 2)

    def test_page_preview_failure_restores_previous_evidence(self):
        self.sold('original')
        parsed = [app._parse_sold_result(r) for r in self.preview_results(5)]
        with patch.object(app, 'calculate_sold_valuation', side_effect=RuntimeError('preview failed')):
            with self.assertRaisesRegex(RuntimeError, 'preview failed'):
                app.research_page_sufficient(self.conn, app.research_query_key('Dell Latitude 5420'), parsed, self.target)
        self.assertEqual(self.conn.execute('SELECT item_id FROM sold_comparables').fetchone()[0], 'original')
        self.assertFalse(self.conn.in_transaction)

    def test_page_preview_restores_previous_evidence(self):
        self.sold('original')
        key = app.research_query_key('Dell Latitude 5420')
        parsed = [app._parse_sold_result(r) for r in self.preview_results(5)]
        self.assertTrue(app.research_page_sufficient(self.conn, key, parsed, self.target))
        ids = [r[0] for r in self.conn.execute('SELECT item_id FROM sold_comparables')]
        self.assertEqual(ids, ['original'])
        self.assertFalse(self.conn.in_transaction)

    def test_page_preview_does_not_promote_duplicate_rows(self):
        parsed = [app._parse_sold_result(self.preview_results(1)[0])]*3
        self.assertFalse(app.research_page_sufficient(self.conn, app.research_query_key('Dell Latitude 5420'), parsed, self.target))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM sold_comparables').fetchone()[0], 0)

    def test_model_query_order_changes_only_after_sufficient_observations(self):
        queries = app.sold_search_queries(self.target)
        key = app.research_model_key(self.target)
        for tier in range(4):
            self.conn.execute('INSERT INTO research_query_yield VALUES (?,?,?,?,?)', (key,tier,2,0,2))
        self.assertEqual(app.prioritise_research_queries(self.conn, self.target, queries), queries)
        self.conn.execute('UPDATE research_query_yield SET trials=3,requests=3')
        self.conn.execute('UPDATE research_query_yield SET supported=3 WHERE tier=2')
        ranked = app.prioritise_research_queries(self.conn, self.target, queries)
        self.assertEqual(ranked[0], queries[2])
        other = dict(self.target, model='Latitude 5430')
        self.assertEqual(app.prioritise_research_queries(self.conn, other, queries), queries)

    def test_query_yield_ignores_transport_failures_and_counts_pages(self):
        query = app.sold_search_queries(self.target)[0]
        key = app.research_query_key(query)
        self.insert('sold_searches', dict(query_key=key, keywords=query, status='ERROR', searched_at=app.iso_now()))
        app.record_research_yield(self.conn, self.target, query, None)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM research_query_yield').fetchone()[0], 0)
        self.conn.execute("UPDATE sold_searches SET status='OK',page_count=2")
        app.record_research_yield(self.conn, self.target, query, dict(estimated_value=400))
        row = self.conn.execute('SELECT * FROM research_query_yield').fetchone()
        self.assertEqual((row['trials'], row['supported'], row['requests']), (1,1,2))

    def test_condition_review_does_not_spend_its_own_research_budget(self):
        self.active('blocked', status='HIGH_RISK', fault_reasons='["motherboard"]')
        with patch.object(app, 'collect_sold_search') as fetch:
            app.collect_needed_sold_data(self.conn, maximum=1)
            fetch.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT backlog_queries FROM research_daily').fetchone()[0], 0)

    def test_cosmetic_condition_still_researched(self):
        self.active('cosmetic', status='MODERATE', fault_reasons='["scratches"]')
        with patch.object(app, 'collect_sold_search', return_value=0) as fetch:
            app.collect_needed_sold_data(self.conn, maximum=1)
            fetch.assert_called_once()

    def test_blocked_listing_does_not_prevent_shared_normal_listing_search(self):
        self.active('blocked', status='HIGH_RISK', fault_reasons='["motherboard"]')
        self.active('ordinary')
        with patch.object(app, 'collect_sold_search', return_value=0) as fetch:
            app.collect_needed_sold_data(self.conn, maximum=1)
            fetch.assert_called_once()
        self.assertEqual(self.conn.execute('SELECT backlog_listings FROM research_daily').fetchone()[0], 1)

    def test_one_aggregate_is_insufficient(self):
        self.sold('one', units_sold=100)
        self.assertIsNone(app.calculate_sold_valuation(self.conn, self.target))

    def test_model_number_before_laptop_is_not_a_bulk_listing(self):
        for title in (
            'Dell Latitude 3190 Laptop, 11.6", 4Gb, 64Gb, Windows 11 Pro 26H2',
            'Dell Latitude 5420 Laptop i5-1135G7 8GB RAM 256GB SSD',
        ):
            with self.subTest(title=title):
                self.assertTrue(app.ordinary_laptop(title, 'Used'))
        self.assertFalse(app.ordinary_laptop('2 laptops Dell Latitude 5420', 'Used'))
        self.assertFalse(app.ordinary_laptop('Lot of 2 Dell Latitude laptops', 'Used'))
        self.assertFalse(app.ordinary_laptop('2 x Dell Latitude 5420', 'Used'))

    def test_model_number_before_laptop_can_supply_sold_evidence(self):
        self.target['title'] = 'Dell Latitude 5420 Laptop i5-1135G7 8GB RAM 256GB SSD'
        self.pool()
        value = app.calculate_sold_valuation(self.conn, self.target)
        self.assertIsNotNone(value)
        self.assertEqual(value['count'], 3)

    def test_x_series_models_are_not_multipack_quantities(self):
        for title in (
            'LENOVO THINKPAD X13 Gen 2 Core i5-1135G7 16GBRAM 256GBSSD ULTRABOOK',
            'Lenovo ThinkPad X380 Yoga 2-in-1 i5-8350U 8GB 128GB SSD Touch',
        ):
            with self.subTest(title=title):
                self.assertTrue(app.ordinary_laptop(title, 'Used'))
        for title in (
            'Joblot 10x Dell Latitude 3190 Laptop 64GB 4GB',
            'Dell Latitude laptops x10', 'Dell Latitude laptops x 10',
            'Dell Latitude laptops ×10', 'Dell Latitude laptops x 2',
        ):
            with self.subTest(title=title):
                self.assertFalse(app.ordinary_laptop(title, 'Used'))

    def test_aggregate_cannot_dominate(self):
        for i, price in enumerate([200, 210, 220]):
            self.sold(i, price)
        self.sold('bulk', 600, units_sold=100)
        value = app.calculate_sold_valuation(self.conn, self.target)
        self.assertEqual(value['estimated_value'], 210)
        self.assertEqual(value['count'], 3)
        self.assertNotEqual(value['confidence'], 'HIGH')

    def test_higher_spec_is_not_used_without_adjustment(self):
        for i in range(3):
            self.sold(i, ram_gb=16, storage_gb=512)
        self.assertIsNone(app.calculate_sold_valuation(self.conn, self.target))

    def test_different_cpu_same_generation_is_not_exact(self):
        for i in range(3):
            self.sold(i, cpu='i7-1165G7')
        self.assertIsNone(app.calculate_sold_valuation(self.conn, self.target))

    def test_faulty_and_incomplete_targets_are_not_valued(self):
        self.pool()
        for status in ['LOW_COST', 'MODERATE', 'HIGH_RISK']:
            with self.subTest(status=status):
                result = app.calculate_valuation(self.conn, dict(self.target, status=status))
                self.assertIsNone(result['estimated_value'])
                self.assertIsNone(result['deal_score'])

    def test_unknown_specs_and_generic_cpu_withhold_value(self):
        self.pool()
        for fields in [dict(ram_gb=None, title='Dell Latitude 5420 i5-1135G7 256GB SSD'),
                       dict(storage_gb=None, title='Dell Latitude 5420 i5-1135G7 8GB RAM'),
                       dict(cpu='Intel Core i5 11th Gen', cpu_confidence='GENERATION'),
                       dict(cpu_confidence='MODEL')]:
            with self.subTest(fields=fields):
                self.assertIsNone(app.calculate_valuation(self.conn, dict(self.target, **fields))['estimated_value'])

    def test_legacy_target_requires_reanalysis(self):
        self.pool()
        result = app.calculate_valuation(self.conn, dict(self.target, classifier_version='0.7.9'))
        self.assertEqual(result['basis'], 'REANALYSIS_REQUIRED')

    def test_changed_rules_revision_requires_reanalysis(self):
        self.pool()
        revision = app.current_rules_revision(self.conn) + 1
        self.conn.execute("UPDATE app_meta SET value=? WHERE key='rules_revision'", (str(revision),))
        self.conn.commit()
        stale = app.calculate_valuation(self.conn, self.target)
        self.assertEqual(stale['basis'], 'REANALYSIS_REQUIRED')
        current = app.calculate_valuation(self.conn, dict(self.target, rules_revision=revision))
        self.assertEqual(current['estimated_value'], 400)

    def test_unambiguous_title_specs_fill_missing_stored_values(self):
        self.pool()
        for changes in [dict(ram_gb=None, storage_gb=None)]:
            with self.subTest(changes=changes):
                value = app.calculate_sold_valuation(self.conn, dict(self.target, **changes))
                self.assertEqual(value['estimated_value'], 400)
                self.assertIn('SPEC_EXACT=3', value['basis'])

    def test_capacity_conflict_uses_lower_supported_price_and_low_confidence(self):
        self.pool(8)
        for i in range(8):
            self.sold('big' + str(i), 600, ram_gb=16, storage_gb=512)
            self.sold('ram' + str(i), 500, ram_gb=16, storage_gb=256)
            self.sold('storage' + str(i), 450, ram_gb=8, storage_gb=512)
        target = dict(self.target, ram_gb=16, storage_gb=512)
        value = app.calculate_sold_valuation(self.conn, target)
        self.assertIsNotNone(value)
        self.assertLess(value['estimated_value'], 600)
        self.assertEqual(value['confidence'], 'LOW')
        self.assertTrue(value['basis'].startswith('SOLD_CONSERVATIVE_SPEC_CONFLICT:'))
        self.assertEqual(app.valuation_target_for_basis(target, value['basis'])['ram_gb'], 8)

    def test_unpriced_conflict_does_not_use_active_fallback(self):
        self.pool()
        target = dict(self.target, ram_gb=16, storage_gb=512)
        self.assertIsNone(app.calculate_sold_valuation(self.conn, target))
        with patch.object(app, 'calculate_active_valuation') as fallback:
            value = app.calculate_valuation(self.conn, target)
            fallback.assert_not_called()
        self.assertEqual(value['basis'], 'SPEC_CONFLICT_INSUFFICIENT_EVIDENCE')

    def test_cpu_conflict_uses_evidence_prices_not_cpu_ranking(self):
        self.pool(8)
        for i in range(8):
            self.sold('i7' + str(i), 250, cpu='i7-1165G7')
        target = dict(self.target, cpu='i7-1165G7')
        value = app.calculate_sold_valuation(self.conn, target)
        self.assertEqual(value['estimated_value'], 250)
        self.assertEqual(value['confidence'], 'LOW')
        self.assertEqual(app.valuation_target_for_basis(target, value['basis'])['cpu'], 'i7-1165G7')

    def test_generic_title_cpu_conflict_requires_lower_family_evidence(self):
        target = dict(self.target, cpu='i7-1165G7', title='Dell Latitude 5420 11th Gen i5 8GB RAM 256GB SSD')
        for i in range(8):
            self.sold('i7' + str(i), 600, cpu='i7-1165G7')
        self.assertIsNone(app.calculate_sold_valuation(self.conn, target))
        self.pool(8)
        value = app.calculate_sold_valuation(self.conn, target)
        self.assertIsNotNone(value)
        self.assertLess(value['estimated_value'], 600)
        self.assertEqual(value['confidence'], 'LOW')

    def test_conflicting_capacity_queries_cover_both_advertised_values(self):
        target = dict(self.target, ram_gb=16, storage_gb=512)
        queries = app.sold_search_queries(target, self.conn)
        self.assertTrue(any('8GB 256GB' in q for q in queries), queries)
        self.assertTrue(any('16GB 512GB' in q for q in queries), queries)

    def test_cached_sweep_removes_unsupported_conflicting_existing_value(self):
        self.pool()
        self.active('conflict', ram_gb=16, storage_gb=512, estimated_value=600,
                    deal_score=90, valuation_confidence='HIGH', valuation_basis='SOLD_OLD')
        app.apply_cached_sold_valuations(self.conn)
        row = self.conn.execute("SELECT * FROM listings WHERE item_id='conflict'").fetchone()
        self.assertIsNone(row['estimated_value'])
        self.assertEqual(row['valuation_basis'], 'SPEC_CONFLICT_INSUFFICIENT_EVIDENCE')

    def test_bad_comparable_titles_are_excluded(self):
        for suffix in [' LOT OF 2', ' job lot', ' faulty', ' choose 8/16GB', ' no ram', ' spares']:
            with self.subTest(suffix=suffix):
                self.conn.execute('DELETE FROM sold_comparables')
                for i in range(3):
                    self.sold(i, title=self.target['title'] + suffix + ' listing ' + str(i))
                self.assertIsNone(app.calculate_sold_valuation(self.conn, self.target))

    def test_repairable_sold_condition_is_adjusted_and_refurbished_is_accepted(self):
        for suffix, price, decision in [(' no charger', 424, 'USE_ADJUSTED'),
                                        (' refurbished', 400, 'USE'),
                                        (' 12 month warranty', 400, 'USE')]:
            with self.subTest(suffix=suffix):
                self.conn.execute('DELETE FROM sold_comparables')
                for i in range(3):
                    self.sold(i, title=self.target['title'] + suffix + ' listing ' + str(i))
                evidence = app.select_sold_evidence(self.conn, self.target)
                self.assertEqual(len(evidence), 3)
                self.assertTrue(all(c['condition_decision'] == decision for c in evidence))
                self.assertTrue(all(c['raw_total'] == 400 for c in evidence))
                self.assertEqual(app.calculate_sold_valuation(self.conn, self.target)['estimated_value'], price)
                if decision == 'USE_ADJUSTED':
                    self.assertTrue(all(c['condition_adjustment_pct'] == 6 for c in evidence))

    def test_old_sale_or_collection_is_excluded(self):
        for field, days in [('last_sold', 91), ('collected_at', 8)]:
            with self.subTest(field=field):
                self.conn.execute('DELETE FROM sold_comparables')
                for i in range(3):
                    self.sold(i, **{field: (app.utcnow()-timedelta(days=days)).isoformat()})
                self.assertIsNone(app.calculate_sold_valuation(self.conn, self.target))

    def test_unknown_sale_date_is_excluded(self):
        for i in range(3):
            self.sold(i, last_sold='not a date')
        self.assertIsNone(app.calculate_sold_valuation(self.conn, self.target))

    def test_same_item_across_queries_is_deduplicated(self):
        for i in range(3):
            self.sold('one', query_key=str(i))
        self.assertIsNone(app.calculate_sold_valuation(self.conn, self.target))

    def test_identical_titles_are_deduplicated(self):
        for i in range(3):
            self.sold(i, title=self.target['title'])
        self.assertIsNone(app.calculate_sold_valuation(self.conn, self.target))

    def test_independent_matching_pool_remains_useful(self):
        self.pool()
        value = app.calculate_sold_valuation(self.conn, self.target)
        self.assertEqual(value['estimated_value'], 400)
        self.assertEqual(value['confidence'], 'LOW')
        self.assertEqual(value['deal_score'], 63.8)  # 85-point raw score × LOW multiplier 0.75
        self.assertEqual(len(app.sold_evidence_for_basis(self.conn, self.target, value['basis'])), 3)

    def test_large_independent_tight_pool_uses_current_confidence_and_score(self):
        self.pool(12)
        value = app.calculate_sold_valuation(self.conn, self.target)
        self.assertEqual(value['confidence'], 'HIGH')
        self.assertEqual(value['deal_score'], 95)  # 85 raw + 10 Q1 bonus

    def test_no_bargain_score_above_conservative_value(self):
        self.pool()
        value = app.calculate_sold_valuation(self.conn, dict(self.target, total=500))
        self.assertEqual(value['deal_score'], 0)

    def test_auction_gets_no_buy_now_score(self):
        self.pool()
        value = app.calculate_sold_valuation(self.conn, dict(self.target, buying_options='["AUCTION"]'))
        self.assertIsNone(value['deal_score'])

    def test_ended_and_old_active_rows_are_excluded(self):
        for fields in [dict(active=0), dict(last_seen='2020-01-01'), dict(end_date='2020-01-01')]:
            with self.subTest(fields=fields):
                self.conn.execute('DELETE FROM listings')
                for i in range(12):
                    self.active(i, **fields)
                self.assertIsNone(app.calculate_active_valuation(self.conn, self.target)['estimated_value'])

    def test_active_prices_are_reference_only(self):
        for i in range(3):
            self.active(i)
        value = app.calculate_valuation(self.conn, self.target)
        self.assertEqual(value['estimated_value'], 500)
        self.assertEqual(value['confidence'], 'ASKING_PRICES_ONLY')
        self.assertIsNone(value['deal_score'])
        self.assertIsNone(value['undervaluation_gbp'])

    def test_unknown_postage_is_not_free(self):
        self.assertIsNone(app.shipping_price({}))
        self.pool()
        result = app.calculate_valuation(self.conn, dict(self.target, total=None, postage=None))
        self.assertEqual(result['basis'], 'UNKNOWN_DELIVERED_COST')

    def test_gbp_price_and_shipping_validation(self):
        self.assertEqual(app.item_price({'price': {'value': '150', 'currency': 'GBP'}}), 150)
        for currency in ['USD', 'EUR', None]:
            self.assertIsNone(app.item_price({'price': {'value': '150', 'currency': currency}}))
        self.assertEqual(app.shipping_price({'shippingOptions': [{'shippingCost': {'value': '0', 'currency': 'GBP'}}]}), 0)

    def test_precise_model_parsing(self):
        for title, model in [('Dell Latitude 7320 Detachable', 'Latitude 7320 Detachable'),
                             ('Microsoft Surface Pro 7+', 'Surface Pro 7+'),
                             ('Microsoft Surface Pro 7 Plus', 'Surface Pro 7+')]:
            self.assertEqual(app.identify_model(title, {}), model)
            self.assertTrue(app.precise_model_for_valuation(None, model))
        for model in ['ThinkPad X1 Carbon', 'ThinkPad T14', 'ThinkPad T14s', 'IdeaPad 3', 'Unknown 1234']:
            self.assertFalse(app.precise_model_for_valuation(None, model))

    def test_fault_negation(self):
        self.assertEqual(app.classify_faults('No BIOS password, hinges good')[0], 'NORMAL')
        self.assertEqual(app.classify_faults('No BIOS password, but no power')[0], 'HIGH_RISK')

    def test_zero_dispersion_outlier(self):
        rows = [dict(total=p) for p in [200, 200, 200, 2000]]
        self.assertEqual([r['total'] for r in app.remove_price_outliers(rows)], [200]*3)

    def research_result(self, **changes):
        data = dict(listing={'itemId': '123', 'title': self.target['title']},
                    avgsalesprice='£400.00', averageshipping='£5.00', itemssold=2,
                    datelastsold=app.iso_now())
        data.update(changes)
        return data

    def test_research_parser_cost_basis(self):
        self.assertEqual(app._parse_sold_result(self.research_result())['delivered_price'], 405)
        self.assertIsNone(app._parse_sold_result(self.research_result(averageshipping=None))['delivered_price'])
        self.assertEqual(app._parse_sold_result(self.research_result(averageshipping=None, freeshipping=True))['delivered_price'], 400)
        self.assertIsNone(app._parse_sold_result(self.research_result(avgsalesprice='$400'))['delivered_price'])
        self.assertIsNone(app._parse_sold_result(self.research_result(averageshipping='€5'))['delivered_price'])

    def test_legacy_sold_rows_are_not_reused(self):
        for i in range(3):
            self.sold(i, evidence_version=None)
        self.assertIsNone(app.calculate_sold_valuation(self.conn, self.target))

    def test_collection_inserts_versioned_evidence(self):
        with patch.object(app, 'product_research_search', return_value=[self.research_result()]):
            self.assertEqual(app.collect_sold_search(self.conn, 'Dell Latitude 5420'), 1)
        row = self.conn.execute('SELECT * FROM sold_comparables').fetchone()
        self.assertEqual(row['evidence_version'], app.SOLD_EVIDENCE_VERSION)
        self.assertEqual(row['currency'], 'GBP')

    def test_successful_valuation_does_not_prevent_refresh(self):
        self.pool()
        self.active('target', estimated_value=400)
        self.insert('sold_searches', dict(query_key='dell latitude 5420', keywords='Dell Latitude 5420',
                    searched_at=(app.utcnow()-timedelta(days=2)).isoformat(), status='OK'))
        with patch.object(app.os.path, 'exists', return_value=True), patch.object(app, 'collect_sold_search', return_value=3) as fetch:
            app.collect_needed_sold_data(self.conn, maximum=1)
            fetch.assert_called_once()

    def test_fresh_query_does_not_refresh(self):
        self.active('target')
        for query in app.sold_search_queries(self.target):
            self.insert('sold_searches', dict(query_key=app.research_query_key(query), keywords=query,
                        searched_at=app.iso_now(), status='OK'))
        with patch.object(app.os.path, 'exists', return_value=True), patch.object(app, 'collect_sold_search') as fetch:
            app.collect_needed_sold_data(self.conn)
            fetch.assert_not_called()

    def test_fresh_queries_still_value_unvalued_listing_from_cache(self):
        self.pool()
        self.active('target', estimated_value=None)
        for query in app.sold_search_queries(self.target):
            self.insert('sold_searches', dict(query_key=app.research_query_key(query), keywords=query,
                        searched_at=app.iso_now(), status='OK', result_count=3))
        with patch.object(app, 'collect_sold_search') as fetch:
            app.collect_needed_sold_data(self.conn, maximum=1)
            fetch.assert_not_called()
        row = self.conn.execute("SELECT * FROM listings WHERE item_id='target'").fetchone()
        self.assertEqual(row['estimated_value'], 400)
        self.assertTrue(row['valuation_basis'].startswith('SOLD_'))

    def test_network_budget_can_leave_later_cached_valuation_unprocessed(self):
        self.pool()
        self.active('older-network', model='Latitude 9999',
                    first_seen='2025-01-01T00:00:00+00:00')
        self.active('cached-ready', first_seen='2026-01-01T00:00:00+00:00')
        with patch.object(app, 'collect_sold_search', return_value=0):
            app.collect_needed_sold_data(self.conn, maximum=1)
        self.assertIsNone(self.conn.execute(
            "SELECT estimated_value FROM listings WHERE item_id='cached-ready'").fetchone()[0])
        with patch.object(app, 'collect_sold_search') as fetch:
            self.assertEqual(app.apply_cached_sold_valuations(self.conn), 1)
            fetch.assert_not_called()
        self.assertEqual(self.conn.execute(
            "SELECT estimated_value FROM listings WHERE item_id='cached-ready'").fetchone()[0], 400)

    def test_cached_sweep_retains_condition_and_evidence_gates(self):
        self.pool()
        self.active('review-required', status='HIGH_RISK', fault_reasons='["faulty"]')
        self.active('no-evidence', model='Latitude 9999')
        with patch.object(app, 'collect_sold_search') as fetch:
            self.assertEqual(app.apply_cached_sold_valuations(self.conn), 0)
            fetch.assert_not_called()
        self.assertEqual(self.conn.execute(
            'SELECT COUNT(*) FROM listings WHERE estimated_value IS NOT NULL').fetchone()[0], 0)

    def test_worker_runs_local_sweep_before_open_circuit_gate(self):
        with patch.object(app, '_product_research_load_rate_state',
                          return_value={'interval_seconds': 30}), \
             patch.object(app, 'connect_db') as connect, \
             patch.object(app, 'refresh_runtime_settings'), \
             patch.object(app, 'refresh_classifier_rules'), \
             patch.object(app, 'apply_cached_sold_valuations', return_value=1) as sweep, \
             patch.object(app, 'notify_pending_telegram_deals', return_value=0) as notify, \
             patch.object(app, '_product_research_circuit_is_open', return_value=True), \
             patch.object(app, '_product_research_circuit_remaining', return_value=60), \
             patch.object(app, 'collect_needed_sold_data') as network, \
             patch.object(app.time, 'sleep', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                app._product_research_worker()
            sweep.assert_called_once_with(connect.return_value)
            notify.assert_called_once_with(connect.return_value)
            network.assert_not_called()

    def test_migration_is_idempotent_and_preserves_history(self):
        self.active('target', estimated_value=500, classifier_version='0.7.9')
        self.sold('old', evidence_version=None)
        with contextlib.redirect_stdout(io.StringIO()):
            app.repair_v078_model_and_sold_cache(self.conn)
            app.repair_v080_valuation_cache(self.conn)
        self.assertIsNone(self.conn.execute('SELECT estimated_value FROM listings').fetchone()[0])
        self.conn.execute('UPDATE listings SET estimated_value=123')
        self.conn.commit()
        app.repair_v080_valuation_cache(self.conn)
        self.assertEqual(self.conn.execute('SELECT estimated_value FROM listings').fetchone()[0], 123)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM sold_comparables').fetchone()[0], 1)

    def test_revalue_and_dashboard_smoke(self):
        self.active('target', total=150)
        self.pool()
        app.revalue_all(self.conn)
        rendered = app.dashboard_html()
        saved = self.conn.execute("SELECT * FROM listings WHERE item_id='target'").fetchone()
        self.assertEqual(saved['estimated_value'], 400)
        self.assertEqual(saved['comparable_count'], 3)
        self.assertTrue(saved['valuation_basis'].startswith('SOLD_'))
        self.assertIn('<th>Normally sells for</th>', rendered)
        self.assertIn(self.target['title'], rendered)
        report = app.listing_report_html('target')
        self.assertIn('Sold-price evidence', report)
        self.assertIn('£400.00', report)

    def test_explicit_variant_contradictions_are_excluded(self):
        variants = [('OLED', 'IPS'), ('RTX 3050', 'RTX 3060'), ('FHD', '4K'),
                    ('60Hz', '144Hz'), ('HDD', 'eMMC'), ('eMMC', 'HDD'),
                    ('non-touch', 'touchscreen')]
        for target_feature, sold_feature in variants:
            with self.subTest(target=target_feature, sold=sold_feature):
                self.conn.execute('DELETE FROM sold_comparables')
                for i in range(3):
                    self.sold(i, title=self.target['title'] + ' ' + sold_feature + ' listing ' + str(i))
                target = dict(self.target, title=self.target['title'] + ' ' + target_feature)
                self.assertIsNone(app.calculate_sold_valuation(self.conn, target))
                matching = dict(self.target, title=self.target['title'] + ' ' + sold_feature)
                selected = app.select_sold_evidence(self.conn, matching)
                self.assertEqual(len(selected), 3)
                self.assertTrue(all(c['tier'] == 'EXACT' for c in selected))
                self.assertEqual(app.calculate_sold_valuation(self.conn, matching)['estimated_value'], 400)

    def test_omitted_optional_variant_is_compatible_at_reduced_weight(self):
        for feature in ['OLED', 'RTX 3050', '4K', '144Hz', 'HDD', 'eMMC', 'touchscreen']:
            with self.subTest(feature=feature):
                self.conn.execute('DELETE FROM sold_comparables')
                for i in range(3):
                    self.sold(i, title=self.target['title'] + ' ' + feature + ' listing ' + str(i))
                compatible = app.select_sold_evidence(self.conn, self.target)
                matching = dict(self.target, title=self.target['title'] + ' ' + feature)
                exact = app.select_sold_evidence(self.conn, matching)
                self.assertEqual(len(compatible), 3)
                self.assertTrue(all(c['tier'] == 'COMPATIBLE' for c in compatible))
                for comp, match in zip(compatible, exact):
                    self.assertAlmostEqual(comp['weight'] / match['weight'], 0.75, places=5)

    def test_unidentified_sold_rows_are_not_independent_evidence(self):
        for i in range(3):
            self.sold('title:' + str(i))
        self.assertIsNone(app.calculate_sold_valuation(self.conn, self.target))

    def test_target_cannot_be_its_own_comparable(self):
        self.sold('298374124379')
        self.sold('298374124380')
        self.sold('298374124381')
        target = dict(self.target, item_id='v1|298374124379|0')
        selected = app.select_sold_evidence(self.conn, target)
        self.assertEqual({c['row']['item_id'] for c in selected},
                         {'298374124380', '298374124381'})
        self.assertEqual(app.calculate_sold_valuation(self.conn, target)['count'], 2)
        self.conn.execute("DELETE FROM sold_comparables WHERE item_id='298374124381'")
        self.assertIsNone(app.calculate_sold_valuation(self.conn, target))

    def test_failed_cache_replacement_retains_previous_evidence(self):
        self.sold('old')
        self.conn.execute("""CREATE TRIGGER reject_test_row BEFORE INSERT ON sold_comparables
            WHEN NEW.item_id='reject' BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
        self.conn.commit()
        bad = self.research_result(listing={'itemId': 'reject', 'title': self.target['title']})
        with patch.object(app, 'product_research_search', return_value=[self.research_result(), bad]):
            self.assertEqual(app.collect_sold_search(self.conn, 'Dell Latitude 5420'), 0)
        self.assertEqual([r[0] for r in self.conn.execute('SELECT item_id FROM sold_comparables')], ['old'])
        self.assertEqual(self.conn.execute('SELECT status FROM sold_searches').fetchone()[0], 'ERROR')

    def test_failed_network_refresh_retains_previous_evidence(self):
        self.sold('old')
        with patch.object(app, 'product_research_search', side_effect=RuntimeError('offline')):
            self.assertEqual(app.collect_sold_search(self.conn, 'Dell Latitude 5420'), 0)
        self.assertEqual(self.conn.execute('SELECT item_id FROM sold_comparables').fetchone()[0], 'old')

    def test_future_query_timestamp_is_not_fresh(self):
        self.insert('sold_searches', dict(query_key='future', keywords='future', status='OK',
                    searched_at=(app.utcnow()+timedelta(days=1)).isoformat()))
        self.assertFalse(app.sold_search_is_fresh(self.conn, 'future'))

    def test_product_research_nested_display_structure(self):
        def display(text):
            return {'_type': 'TextualDisplay', 'textSpans': [{'_type': 'TextSpan', 'text': text}]}
        result = self.research_result(avgsalesprice=display('£400.00'),
                    averageshipping=display('£5.00'), itemssold=display('8'),
                    datelastsold=display(app.utcnow().strftime('%d %b %Y')))
        parsed = app._parse_sold_result(result)
        self.assertEqual(parsed['delivered_price'], 405)
        self.assertEqual(parsed['units_sold'], 8)
        self.assertIsNotNone(app.evidence_age_days(parsed['last_sold']))

    def test_availability_recheck_refreshes_legacy_listing_without_second_request(self):
        self.active('target', classifier_version='0.7.9', first_seen='2020-01-01')
        detail = dict(itemId='target', title=self.target['title'], condition='Used',
            price={'value': '175', 'currency': 'GBP'}, buyingOptions=['FIXED_PRICE'],
            shippingOptions=[{'shippingCost': {'value': '5', 'currency': 'GBP'}}])
        with patch.object(app, 'ebay_get_item', return_value=('ACTIVE', detail)) as get, patch.object(app, 'get_item') as second:
            self.assertEqual(app.recheck_active_bin_listings(self.conn, 'fake', maximum=1), (1, 0))
            get.assert_called_once()
            second.assert_not_called()
        saved = self.conn.execute('SELECT * FROM listings').fetchone()
        self.assertEqual(saved['classifier_version'], app.CLASSIFIER_VERSION)
        self.assertEqual(saved['total'], 180)
        self.assertEqual(app.browse_usage_today(self.conn), 1)

    def test_availability_recheck_respects_budget(self):
        self.active('target', first_seen='2020-01-01')
        with patch.object(app, 'can_detail', return_value=False), patch.object(app, 'ebay_get_item') as get:
            self.assertEqual(app.recheck_active_bin_listings(self.conn, 'fake', maximum=1), (0, 0))
            get.assert_not_called()

    def test_unknown_shipping_survives_analysis_save_and_display(self):
        summary = dict(itemId='target', title=self.target['title'], condition='Used',
                       price={'value': '150', 'currency': 'GBP'}, buyingOptions=['FIXED_PRICE'])
        item = app.analyse_listing(self.conn, 'fake', summary, fetch_detail=False)
        app.save_listing(self.conn, item)
        with contextlib.redirect_stdout(io.StringIO()):
            app.display_listing(item)
        self.assertIsNone(self.conn.execute('SELECT total FROM listings').fetchone()[0])
        app.update_known_summary(self.conn, summary)
        self.assertIsNone(self.conn.execute('SELECT total FROM listings').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
