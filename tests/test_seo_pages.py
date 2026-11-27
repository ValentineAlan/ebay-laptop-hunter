import io
import json
import re
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
        self.assertIn('\u00a3120.00', body)
        self.assertIn('&lt;script&gt;', body)
        self.assertNotIn('<script>bad', body)
        self.assertIn('/report?item_id=v1%7C', body)
        self.conn.execute('UPDATE listings SET active=0')
        self.assertIn('No matching listings', seo_pages.listing_table(self.conn, 'total < 200'))
        page = seo_pages.page('/used-laptops-under-200', 'Budget', 'Description', body)
        self.assertIn('href="https://laptoplander.com/used-laptops-under-200"', page)

    def test_missing_conditions_are_filtered_before_rows_reach_python(self):
        for i in range(30):
            self.insert(str(i), sold_condition=None if i % 2 else '')
        fetched = []
        def row_factory(cursor, row):
            fetched.append(row)
            return sqlite3.Row(cursor, row)
        self.conn.row_factory = row_factory
        groups = seo_pages.evidence_groups(self.conn, None)
        self.assertEqual(groups, {})
        self.assertEqual(fetched, [])

    def test_duplicate_assessments_are_reused_only_within_request(self):
        for i in range(5):
            self.insert(str(i))
            self.insert(str(i))
        calls = []
        def assess(title, condition):
            calls.append((title, condition))
            return {'decision': 'USE'}
        groups = seo_pages.evidence_groups(self.conn, assess)
        self.assertEqual(len(next(iter(groups.values()))), 5)
        self.assertEqual(calls, [('Laptop', 'Used')])
        # A subsequent request must see changed classifier decisions immediately.
        self.assertEqual(seo_pages.evidence_groups(self.conn, lambda *_: {'decision': 'EXCLUDE'}), {})

    def test_excluded_newer_record_does_not_hide_older_eligible_observation(self):
        from datetime import timedelta
        for i in range(5):
            self.insert(str(i))
        newer = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
        self.insert('0', title='Faulty', collected_at=newer)
        self.insert('1', title='Laptop', sold_condition='Refurbished', collected_at=newer)
        calls = []
        def assess(title, condition):
            calls.append((title, condition))
            return {'decision': 'EXCLUDE' if title == 'Faulty' or condition == 'Refurbished' else 'USE'}
        groups = seo_pages.evidence_groups(self.conn, assess)
        rows = next(iter(groups.values()))
        self.assertEqual({r['item_id'] for r in rows}, {str(i) for i in range(5)})
        self.assertTrue(all(r['title'] == 'Laptop' and r['sold_condition'] == 'Used' for r in rows))
        self.assertEqual(len(calls), 3)

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
        self.assertIn('content="noindex,follow"', handler.body)

    def test_permanent_redirects_without_database_access(self):
        for source, target in [
            ('/index.html', '/'),
            ('/laptops-under-200', '/used-laptops-under-200'),
            ('/laptops-under-200/', '/used-laptops-under-200'),
            ('/buying-guides/', '/buying-guides'),
            *[(path + '/', path) for path in seo_pages.CATEGORIES],
        ]:
            with self.subTest(source=source):
                handler = Handler()
                self.assertTrue(seo_pages.handle(handler, source, None, None))
                self.assertEqual(handler.status, 301)
                self.assertEqual(handler.headers['Location'], seo_pages.ORIGIN + target)
                self.assertEqual(handler.headers['Content-Length'], '0')
        self.assertFalse(seo_pages.handle(Handler(), '/unknown/', None, None))
        self.assertFalse(seo_pages.handle(Handler(), '/', None, None))

    def test_structured_data_matches_visible_hierarchy_and_escapes_markup(self):
        title = 'Laptop </script><script>alert(1)</script>'
        path = '/used-laptops-under-200'
        page = seo_pages.page(path, title, 'Budget choices', '')
        scripts = re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S)
        self.assertEqual(len(scripts), 1)
        graph = json.loads(scripts[0])['@graph']
        self.assertEqual(graph[0]['@type'], 'WebSite')
        self.assertEqual(graph[1]['url'], seo_pages.ORIGIN + path)
        self.assertEqual(graph[1]['name'], title)
        crumbs = graph[2]['itemListElement']
        self.assertEqual([c['position'] for c in crumbs], [1, 2, 3])
        self.assertEqual(crumbs[1]['item'], seo_pages.ORIGIN + '/buying-guides')
        self.assertIn('aria-label="Breadcrumb"', page)
        self.assertIn('aria-current="page"', page)
        self.assertNotIn('<script>alert(1)', page)
        self.assertIn('href="/windows-11-used-laptops"', page)
        self.assertIn('href="/usb-c-charging-used-laptops"', page)
        hub = seo_pages.page('/buying-guides', 'Guides', 'Choose a laptop', '')
        hub_graph = json.loads(re.search(
            r'<script type="application/ld\+json">(.*?)</script>', hub, re.S)[1])['@graph']
        self.assertEqual(len(hub_graph[2]['itemListElement']), 2)


if __name__ == '__main__':
    unittest.main()
