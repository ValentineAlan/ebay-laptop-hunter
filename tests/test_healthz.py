import http.client
from http.server import HTTPServer
import json
import threading
import unittest
from unittest.mock import patch

import app


class HealthEndpointTests(unittest.TestCase):
    def test_liveness_is_uncached_and_independent_of_database_and_rendering(self):
        # Exercise an actual HTTP request, but never start app workers or use
        # credentials, a production database, or external networking.
        with patch.object(app, 'connect_db', side_effect=AssertionError('database accessed')), \
             patch.object(app.seo_pages, 'handle', side_effect=AssertionError('page rendering accessed')), \
             patch.object(app.DashboardHandler, 'log_message'):
            server = HTTPServer(('127.0.0.1', 0), app.DashboardHandler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                for path in ('/healthz', '/healthz?probe=monitor'):
                    with self.subTest(path=path):
                        connection = http.client.HTTPConnection(*server.server_address, timeout=3)
                        try:
                            connection.request('GET', path)
                            response = connection.getresponse()
                            body = response.read()
                            self.assertEqual(response.status, 200)
                            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
                            self.assertEqual(response.getheader('Content-Type'), 'application/json; charset=utf-8')
                            self.assertEqual(int(response.getheader('Content-Length')), len(body))
                            self.assertEqual(json.loads(body), {'status': 'ok', 'app_version': app.APP_VERSION})
                        finally:
                            connection.close()
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
