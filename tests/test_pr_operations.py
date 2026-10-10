import copy
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from pr_operations import Operations


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.saved = {}
        self.now = 100000.0
        self.send = Mock(return_value=(True, ""))
        self.ops = Operations(lambda: copy.deepcopy(self.saved), self.write,
                              self.send, clock=lambda: self.now)

    def write(self, state, updates):
        self.saved = copy.deepcopy(state)

    def messages(self):
        return [e["text"] for e in self.saved.get("pending", [])]

    def test_volume_transition_suppression_and_release(self):
        self.ops.volume(1700, 1700, self.now + 100)
        self.ops.volume(1701, 1700, self.now + 200)
        self.assertEqual(len(self.messages()), 1)
        self.ops.volume(1699, 1700, 0, circuit_open=True)
        self.assertEqual(len(self.messages()), 2)
        self.assertIn("circuit is still open", self.messages()[1])
        self.ops.volume(1699, 1700, 0)
        self.assertEqual(len(self.messages()), 2)

    def test_failed_delivery_survives_restart_and_retries_same_event(self):
        self.ops.volume(1700, 1700, self.now + 100)
        original = copy.deepcopy(self.saved["pending"][0])
        self.send.return_value = (False, "offline")
        self.assertFalse(self.ops.deliver_one())
        self.assertEqual(self.saved["pending"][0]["attempts"], 1)
        restarted = Operations(lambda: copy.deepcopy(self.saved), self.write,
                               self.send, clock=lambda: self.now)
        self.assertFalse(restarted.deliver_one())
        self.assertEqual(self.send.call_count, 1)
        self.now += 31
        self.send.return_value = (True, "")
        self.assertTrue(restarted.deliver_one())
        self.assertEqual(self.send.call_args.args[0], original["text"])
        self.assertEqual(self.saved["pending"], [])

    def test_fifo_and_new_event_added_during_delivery(self):
        self.ops.volume(1700, 1700, self.now + 100)
        first = self.messages()[0]
        def send(text):
            self.ops.volume(1699, 1700, 0)
            return True, ""
        self.ops.send = send
        self.assertTrue(self.ops.deliver_one())
        self.assertEqual(len(self.messages()), 1)
        self.assertNotEqual(self.messages()[0], first)

    def test_concurrent_transition_queues_once(self):
        threads = [threading.Thread(target=self.ops.volume,
                   args=(1700, 1700, self.now + 100)) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.messages()), 1)

    def test_three_failures_one_warning_then_one_recovery(self):
        self.ops.failure("TIMEOUT")
        self.ops.failure("BROWSER_ERROR")
        self.assertEqual(self.messages(), [])
        self.ops.failure("UNEXPECTED")
        self.ops.failure("TIMEOUT")
        self.assertEqual(len(self.messages()), 1)
        self.ops.healthy()
        self.ops.healthy()
        self.assertEqual(len(self.messages()), 2)

    def test_429_suppression_and_circuit_recovery_supersedes_warning(self):
        self.ops.throttled(self.now + 300, 60)
        self.ops.throttled(self.now + 600, 120)
        self.assertEqual(len(self.messages()), 1)
        self.ops.healthy(circuit_recovered=True)
        self.assertEqual(len(self.messages()), 1)
        self.ops.throttled(self.now + 300, 60)
        self.assertEqual(len(self.messages()), 2)


class ProductResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 100000.0
        self.saved = {}
        self.patches = [
            patch.object(app, 'DB', str(Path(self.tmp.name) / 'hunter.db')),
            patch.object(app.time, 'time', side_effect=lambda: self.now),
            patch.object(app, '_product_research_telemetry_loaded', False),
            patch.object(app, '_product_research_telemetry_state', copy.deepcopy(app._product_research_telemetry_state)),
            patch.object(app, '_product_research_rate_loaded', True),
            patch.object(app, '_product_research_rate', dict(interval_seconds=30., success_streak=0, backoff_until=0.)),
            patch.object(app, '_product_research_next_request_at', 0.),
            patch.object(app, '_session_state_update'),
            patch.object(app.os, 'unlink'),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        app.init_db()
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        app._product_research_response_context.telemetry = None
        app._product_research_response_context.search_lock_held = False

    def seed(self, times):
        app.runtime_state_set(app.RUNTIME_STATE_PRODUCT_RESEARCH_TELEMETRY,
                              dict(request_times=times, request_count=len(times), experiment={}))
        app._product_research_telemetry_loaded = False

    def pending(self):
        return app._product_research_operations.state().get('pending', [])

    def test_restart_keeps_12_to_24_hour_requests_and_discards_expired_future(self):
        self.seed([self.now-23*3600, self.now-13*3600, self.now-3600,
                   self.now-86400, self.now+1])
        state = app._product_research_telemetry_load()
        self.assertEqual(len(state['request_times']), 3)
        self.assertEqual(app._product_research_volume_snapshot()[0], 3)

    def test_guard_boundary_and_extra_recovery_probes(self):
        self.seed([self.now-100] * 1699)
        self.assertEqual(app._product_research_volume_wait_seconds(), 0)
        self.seed([self.now-100] * 1700)
        self.assertEqual(app._product_research_volume_wait_seconds(), 86300)
        self.seed([self.now-200, self.now-100] + [self.now-50] * 1699)
        self.assertEqual(app._product_research_volume_wait_seconds(), 86300)
        self.now += 86300
        self.assertEqual(app._product_research_volume_wait_seconds(), 0)

    def test_guard_does_not_repeat_at_normal_cadence(self):
        self.seed([self.now-86400+20] * 1700)
        app._product_research_update_volume_state()
        self.assertEqual(self.pending(), [])
        self.seed([self.now-100] * 1700)
        app._product_research_update_volume_state()
        self.assertEqual(len(self.pending()), 1)
        self.now += 86400
        app._product_research_update_volume_state()
        self.assertEqual(len(self.pending()), 2)
        self.seed([self.now-86400+20] * 1700)
        app._product_research_update_volume_state()
        self.assertEqual(len(self.pending()), 2)

    def test_cooldowns_and_single_recovery_alert(self):
        for expected in [3600, 7200, 14400, 21600, 21600]:
            app._product_research_open_circuit('eBay Chromium session challenged')
            self.assertEqual(app._product_research_circuit_state()['cooldown_seconds'], expected)
        self.assertEqual(len(self.pending()), 5)
        self.assertTrue(app._product_research_close_circuit())
        self.assertFalse(app._product_research_close_circuit())
        self.assertEqual(len(self.pending()), 6)
        self.assertFalse(app._product_research_circuit_is_open())

    def test_concurrent_close_queues_one_recovery(self):
        app._product_research_open_circuit('challenged')
        with patch.object(app, '_product_research_telemetry_mark_recovery'):
            threads = [threading.Thread(target=app._product_research_close_circuit) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(self.pending()), 2)

    def test_circuit_and_alert_commit_rollback_together(self):
        with app.connect_db() as conn:
            conn.execute("CREATE TRIGGER fail_operations BEFORE INSERT ON runtime_state "
                         "WHEN NEW.key='product_research_operations' BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(Exception):
            app._product_research_open_circuit('challenged')
        self.assertFalse(app._product_research_circuit_is_open())
        self.assertEqual(self.pending(), [])

    def test_failed_recovery_commit_preserves_circuit_and_experiment(self):
        app._product_research_telemetry_mark_recovery()
        app._product_research_open_circuit('challenged')
        before = app._product_research_experiment_snapshot()
        with app.connect_db() as conn:
            conn.execute("CREATE TRIGGER fail_operations BEFORE INSERT ON runtime_state "
                         "WHEN NEW.key='product_research_operations' BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(Exception):
            app._product_research_close_circuit()
        self.assertTrue(app._product_research_circuit_is_open())
        self.assertEqual(app._product_research_experiment_snapshot(), before)
        self.assertEqual(len(self.pending()), 1)

    def test_auth_required_has_login_instruction_and_no_false_ok(self):
        with patch.object(app, '_product_research_fetch', return_value='{"error":"auth_required","reason_code":"invalid_session"}'), \
             patch.object(app, '_product_research_response_outcome') as outcome:
            with self.assertRaisesRegex(RuntimeError, 'SESSION_INVALID'):
                app.product_research_search('Dell')
        outcome.assert_called_once_with('AUTH_REQUIRED')
        message = self.pending()[0]['text']
        self.assertIn('Sign in to eBay', message)
        self.assertNotIn('No action required', message)

    def test_validated_success_only_and_unexpected_failure(self):
        with patch.object(app, '_product_research_fetch', return_value='{"_type":"SearchResultsModule","results":[]}'), \
             patch.object(app, '_product_research_response_outcome') as outcome:
            self.assertEqual(app.product_research_search('Dell'), [])
        outcome.assert_called_once_with('OK')
        with patch.object(app, '_product_research_fetch', return_value='{"error":"unknown"}'), \
             patch.object(app, '_product_research_response_outcome') as outcome:
            with self.assertRaisesRegex(RuntimeError, 'RESPONSE'):
                app.product_research_search('Dell')
        outcome.assert_called_once_with('UNEXPECTED')
        self.assertEqual(app._product_research_operations.state()['failure_count'], 1)

    def test_recovery_with_full_guard_does_not_claim_processing_resumed(self):
        self.seed([self.now-100] * 1700)
        app._product_research_open_circuit('challenged')
        with patch.object(app, '_product_research_fetch', return_value='{"_type":"SearchResultsModule"}') as fetch:
            app.product_research_search('Dell', allow_probe=True)
        self.assertTrue(fetch.call_args.kwargs['bypass_volume_limit'])
        message = self.pending()[-1]['text']
        self.assertIn('still waits for rolling-volume allowance', message)
        self.assertNotIn('has resumed valuation processing', message)
        self.assertFalse(app._product_research_circuit_is_open())

    def test_healthy_monitor_probe_cannot_bypass_guard(self):
        with patch.object(app, '_product_research_fetch', return_value='{"_type":"SearchResultsModule"}') as fetch:
            app.product_research_search('Dell', allow_probe=True)
        self.assertFalse(fetch.call_args.kwargs['bypass_volume_limit'])

    def test_open_circuit_probe_bypasses_only_volume_and_counts_request(self):
        self.seed([self.now-100] * 1700)
        app._product_research_open_circuit('challenged')
        app._product_research_rate['backoff_until'] = self.now + 60
        def advance(seconds):
            self.now += seconds
        try:
            with patch.object(app.time, 'sleep', side_effect=advance):
                telemetry = app._product_research_wait_for_slot(bypass_volume_limit=True)
            self.assertEqual(self.now, 100060)
            self.assertEqual(telemetry['counts'][1440], 1701)
        finally:
            if app._product_research_response_context.search_lock_held:
                app._product_research_search_lock.release()
                app._product_research_response_context.search_lock_held = False

    def test_normal_wait_rechecks_open_circuit(self):
        self.seed([self.now-100] * 1700)
        with patch.object(app.time, 'sleep', side_effect=lambda _: app._product_research_open_circuit('challenged')):
            with self.assertRaisesRegex(RuntimeError, 'CIRCUIT_OPEN'):
                app._product_research_wait_for_slot()

    def test_last_normal_slot_is_reserved_before_another_caller(self):
        self.seed([self.now-100] * 1699)
        try:
            first = app._product_research_wait_for_slot()
            self.assertEqual(first['counts'][1440], 1700)
        finally:
            if app._product_research_response_context.search_lock_held:
                app._product_research_search_lock.release()
                app._product_research_response_context.search_lock_held = False
        with patch.object(app.time, 'sleep', side_effect=RuntimeError('volume waiting')):
            with self.assertRaisesRegex(RuntimeError, 'volume waiting'):
                app._product_research_wait_for_slot()
        self.assertEqual(app._product_research_volume_snapshot()[0], 1700)

    def test_http_403_challenge_uses_circuit_path_once(self):
        telemetry = app._product_research_telemetry_begin()
        payload = dict(request_id='test-request', status=403,
                       body='<html>Pardon our interruption</html>')
        with patch.object(app, '_product_research_wait_for_slot', return_value=telemetry), \
             patch.object(app.secrets, 'token_hex', return_value='test-request'), \
             patch.object(app, '_atomic_json_write'), \
             patch.object(app, '_read_json_file', return_value=payload), \
             patch.object(app, '_product_research_telemetry_log') as log:
            with self.assertRaisesRegex(RuntimeError, 'SESSION_CHALLENGED'):
                app.product_research_search('Dell')
        self.assertEqual(log.call_count, 1)
        self.assertEqual(log.call_args.args[1], 'CHALLENGE')
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(app._product_research_operations.state()['failure_count'], 0)

    def test_http_429_has_one_throttle_path_without_failure_warning(self):
        telemetry = app._product_research_telemetry_begin()
        payload = dict(request_id='test-request', status=429, body='', retry_after='120')
        with patch.object(app, '_product_research_wait_for_slot', return_value=telemetry), \
             patch.object(app.secrets, 'token_hex', return_value='test-request'), \
             patch.object(app, '_atomic_json_write'), \
             patch.object(app, '_read_json_file', return_value=payload):
            with self.assertRaises(app.ProductResearchRateLimited):
                app.product_research_search('Dell')
        self.assertEqual(len(self.pending()), 1)
        self.assertIn('rate limited', self.pending()[0]['text'])
        self.assertFalse(app._product_research_circuit_is_open())
        self.assertEqual(app._product_research_rate['backoff_until'], self.now + 120)
        self.assertEqual(app._product_research_operations.state().get('failure_count', 0), 0)

    def test_recovery_probe_loses_bypass_when_another_probe_closes_circuit(self):
        self.seed([self.now-100] * 1700)
        app._product_research_open_circuit('challenged')
        app._product_research_rate['backoff_until'] = self.now + 60
        slept = []
        def advance(seconds):
            slept.append(seconds)
            if len(slept) == 1:
                app._product_research_close_circuit()
                self.now += seconds
            else:
                raise RuntimeError('still volume guarded')
        with patch.object(app.time, 'sleep', side_effect=advance):
            with self.assertRaisesRegex(RuntimeError, 'still volume guarded'):
                app._product_research_wait_for_slot(bypass_volume_limit=True)
        self.assertEqual(len(app._product_research_telemetry_state['request_times']), 1700)

    def test_single_inflight_request_releases_gate_on_transport_failure(self):
        payload = dict(request_id='test-request', status=0, body='', error='helper disconnected')
        with patch.object(app.secrets, 'token_hex', return_value='test-request'), \
             patch.object(app, '_atomic_json_write'), \
             patch.object(app, '_read_json_file', return_value=payload):
            with self.assertRaisesRegex(RuntimeError, 'BROWSER_ERROR'):
                app.product_research_search('Dell')
        self.assertTrue(app._product_research_search_lock.acquire(blocking=False))
        app._product_research_search_lock.release()

    def test_queued_alert_survives_database_restart(self):
        app._product_research_open_circuit('challenged')
        event = self.pending()[0]
        sender = Mock(return_value=(True, ''))
        restarted = Operations(
            lambda: app.runtime_state_get(app.RUNTIME_STATE_PRODUCT_RESEARCH_OPERATIONS, {}),
            app._product_research_operations_write, sender, clock=lambda: self.now)
        self.assertTrue(restarted.deliver_one())
        self.assertEqual(sender.call_args.args[0], event['text'])
        self.assertEqual(self.pending(), [])

    def test_telemetry_reports_24h_and_auth_failure_counts(self):
        self.seed([self.now-13*3600])
        app._product_research_telemetry_mark_recovery()
        telemetry = app._product_research_telemetry_begin()
        app._product_research_telemetry_log(telemetry, 'AUTH_REQUIRED')
        experiment = app._product_research_experiment_snapshot()
        self.assertEqual(experiment['challenge_outcome'], 'AUTH_REQUIRED')
        self.assertEqual(experiment['rolling_at_challenge'][1440], 2)
        self.assertIn('24h 2', '\n'.join(app._product_research_experiment_lines(experiment)))

    def test_report_parser_accepts_old_and_new_telemetry(self):
        prefix = ('Product Research telemetry: PR #1 +0:01:00 interval=30.0s '
                  'outcome=CHALLENGE rolling=1m:1,5m:1,15m:1,30m:1,60m:1')
        for suffix in [' session:1', ',6h:1,12h:1 session:1', ',6h:1,12h:1,24h:1700 session:1']:
            result = subprocess.run([sys.executable, 'tools/pr_telemetry_report.py'],
                                    input=prefix+suffix, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('30-second challenge summary', result.stdout)
            if '24h' in suffix:
                self.assertIn('1700', result.stdout)


if __name__ == '__main__':
    unittest.main()
