import unittest
from unittest.mock import patch
from tests import test_valuation as fixtures
import app


class Windows11CategoryTests(fixtures.ValuationTests):
    # Use only database setup helpers, not the inherited valuation test suite.
    def test_reviewed_models(self):
        groups = {
            app.WIN11_OFFICIAL_BASIC: ['Intel Pentium N5030', 'Intel Pentium N5000', 'Intel Pentium 5405U', 'Intel Pentium Gold 6405U'],
            app.WIN11_MAC_VM: ['Apple M1', 'Apple M1 Pro', 'Apple M2', 'Apple M2 Pro', 'Apple M3', 'Apple M4'],
            app.WIN11_CHROMEOS: ['MediaTek Kompanio 500', 'MediaTek Kompanio 520', 'MediaTek Kompanio 540'],
            app.WIN11_UNSUITABLE: ['Intel Celeron '+x for x in ['N3350','N3060','N2840','N2940','N3050','N3160','N2830']] + ['Intel Pentium '+x for x in ['N4200','N3700']] + ['Intel Atom N270','Intel Atom N450','Intel Core 2 Duo T6670','Intel Core 2 Duo T5800'] + ['AMD '+x for x in app.WIN11_OLD_AMD],
            app.WIN11_UNKNOWN: ['Intel Pentium 4415U'],
        }
        with patch.object(app, 'cpu_benchmark_for_cpu', return_value=None):
            for state, names in groups.items():
                for name in names:
                    with self.subTest(cpu=name):
                        result = app.windows11_cpu_assessment(self.conn, app._stored_cpu_descriptor(name))
                        self.assertEqual(result['state'], state)
                        self.assertEqual(result['value'], True if state == app.WIN11_OFFICIAL_BASIC else None if state == app.WIN11_UNKNOWN else False)

    def test_exact_boundaries(self):
        for name in ['Intel Pentium N50300', 'Intel Celeron N5030', 'AMD A4-9120', 'Apple M9', 'Intel Pentium 4417U']:
            self.assertIsNone(app.windows11_named_cpu_category(app._stored_cpu_descriptor(name)))

    def test_migration_preserves_valuation_and_is_idempotent(self):
        self.active('mac', cpu='Apple M2', estimated_value=500, deal_score=35, win11_state='UNKNOWN')
        with patch.object(app, 'cpu_benchmark_for_cpu', return_value=None):
            app.repair_v01062_windows11_categories(self.conn)
            app.repair_v01062_windows11_categories(self.conn)
        row = self.conn.execute("SELECT * FROM listings WHERE item_id='mac'").fetchone()
        self.assertEqual(row['win11_state'], app.WIN11_MAC_VM)
        self.assertEqual(row['win11'], 0)
        self.assertEqual(row['estimated_value'], 500)
        self.assertEqual(row['deal_score'], 35)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM app_migrations WHERE migration_key LIKE ?', ('v0.10.62%',)).fetchone()[0], 1)

# Keep the shared fixture while avoiding inherited tests being rerun.
for name in list(vars(fixtures.ValuationTests)):
    if name.startswith('test_'):
        setattr(Windows11CategoryTests, name, None)
