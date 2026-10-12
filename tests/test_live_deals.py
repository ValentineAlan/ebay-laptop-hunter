import unittest
from unittest.mock import patch
import app


class LiveDealSnapshotTests(unittest.TestCase):
    def test_poll_uses_rendered_snapshot_not_independent_ids(self):
        page = '<tbody><tr data-item-id="v1|123|0"></tr><tr data-item-id="a&amp;b"></tr><tr data-item-id="v1|123|0"></tr></tbody>'
        with patch.object(app, 'dashboard_html', return_value=page), patch.object(app, '_live_deal_ids', side_effect=AssertionError('independent snapshot')):
            self.assertEqual(app._cached_live_deal_ids(), ['v1|123|0', 'a&b'])
        with patch.object(app, 'dashboard_html', return_value='<tbody></tbody>'):
            self.assertEqual(app._cached_live_deal_ids(), [])

    def test_refresh_bypasses_dashboard_cache(self):
        with patch.object(app, '_dashboard_cache_page', 'old'), patch.object(app, '_dashboard_cache_expires', float('inf')), patch.object(app, 'refresh_runtime_settings'), patch.object(app, 'refresh_classifier_rules'), patch.object(app, '_dashboard_health_alert', return_value=''), patch.object(app, '_dashboard_html_base', return_value='<body>new</body>') as render:
            self.assertEqual(app.dashboard_html(), 'old')
            self.assertIn('<body>new', app.dashboard_html(force_refresh=True))
            render.assert_called_once()
