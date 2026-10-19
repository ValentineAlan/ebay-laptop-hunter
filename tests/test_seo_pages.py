import io
import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import seo_pages


class Handler:
    def __init__(self):
        self.wfile = io.BytesIO()
        self.headers = {}
    def send_response(self, status):
        self.status = status
    def send_header(self, key, value):
        self.headers[key] = value
    def end_headers(self):
        pass
    def _send_html(self, value, status=200):
        self.status = status
        self.body = value


class SeoTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('''CREATE TABLE sold_comparables (
            item_id TEXT, brand TEXT, model TEXT, currency TEXT, delivered_price REAL,
            last_sold TEXT, collected_at TEXT, title TEXT, sold_condition TEXT,
            cpu TEXT, ram_gb INTEGER, storage_gb INTEGER)''')
        self.now = datetime.now(timezone.utc).isoformat()
        self.addCleanup(self.conn.close)

    def insert(self, item, **values):
        row = dict(item_id=item, brand='Dell', model='Latitude 5420', currency='GBP',
                   delivered_price=150, last_sold=self.now, collected_at=self.now,
                   title='Laptop', sold_condition='Used', cpu='i5', ram_gb=8, storage_gb=256)
        row.update(values)
        self.conn.execute('INSERT INTO sold_comparables VALUES (' + ','.join('?' for _ in row) + ')', list(row.values()))

    def test_evidence_excludes_duplicates_old_foreign_and_uncertain_condition(self):
        for i in range(5):
            self.insert(str(i))
        self.insert('0')
        self.insert('old', last_sold='2020-01-01')
        self.insert('dollars', currency='USD')
        self.insert('unknown', sold_condition='')
        self.insert('new', sold_condition='New')
        self.insert('fault', title='Faulty')
        assess = lambda title, _: {'decision': 'EXCLUDE' if title == 'Faulty' else 'USE'}
        groups = seo_pages.evidence_groups(self.conn, assess)
        self.assertEqual(len(next(iter(groups.values()))), 5)
        self.conn.execute("DELETE FROM sold_comparables WHERE item_id = '4'")
        self.assertEqual(seo_pages.evidence_groups(self.conn, assess), {})

    def test_category_has_canonical_and_escapes_listing_content(self):
        self.conn.execute('''CREATE TABLE listings (item_id TEXT, title TEXT, condition TEXT,
            status TEXT, total REAL, postage REAL, active INTEGER, end_date TEXT)''')
        self.conn.execute("INSERT INTO listings VALUES ('v1|123456789|0', '<script>bad</script>', 'Used', 'NORMAL', 120, 0, 1, NULL)")
        body = seo_pages.listing_table(self.conn, 'total < 200')
        self.assertIn('&lt;script&gt;', body)
        self.assertNotIn('<script>bad', body)
        self.assertIn('/report?item_id=v1%7C', body)
        self.conn.execute('UPDATE listings SET active=0')
        self.assertIn('No matching listings', seo_pages.listing_table(self.conn, 'total < 200'))
        page = seo_pages.page('/used-laptops-under-200', 'Budget', 'Description', body)
        self.assertIn('href="https://laptoplander.com/used-laptops-under-200"', page)

    def test_sitemap_and_robots_and_missing_model(self):
        handler = Handler()
        self.assertTrue(seo_pages.handle(handler, '/robots.txt', None, None))
        self.assertIn(b'Sitemap: https://laptoplander.com/sitemap.xml', handler.wfile.getvalue())
        # Keep the in-memory fixture open across the route's connection lifecycle.
        class Connection:
            def execute(_, *args):
                return self.conn.execute(*args)
            def close(_):
                pass
        handler = Handler()
        seo_pages.handle(handler, '/sitemap.xml', Connection, None)
        import xml.etree.ElementTree as ET
        root = ET.fromstring(handler.wfile.getvalue())
        self.assertEqual(len(root), 5)
        handler = Handler()
        seo_pages.handle(handler, '/used-laptop-prices/missing', Connection, None)
        self.assertEqual(handler.status, 404)


if __name__ == '__main__':
    unittest.main()
