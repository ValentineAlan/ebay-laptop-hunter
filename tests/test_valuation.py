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

    def test_unambiguous_title_specs_override_missing_or_incorrect_stored_values(self):
        self.pool()
        for changes in [dict(ram_gb=None, storage_gb=None), dict(ram_gb=16, storage_gb=512)]:
            with self.subTest(changes=changes):
                value = app.calculate_sold_valuation(self.conn, dict(self.target, **changes))
                self.assertEqual(value['estimated_value'], 400)
                self.assertIn('SPEC_EXACT=3', value['basis'])

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
