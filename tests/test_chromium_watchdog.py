import importlib.util
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from helper_supervisor import stalled

spec = importlib.util.spec_from_file_location('watchdog', Path(__file__).resolve().parents[1] / 'tools/chromium_watchdog.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


class WatchdogTests(unittest.TestCase):
    def test_worker_deadline(self):
        self.assertFalse(stalled(10, 190))
        self.assertTrue(stalled(10, 191))

    def test_threshold_cooldown_and_hourly_limit(self):
        self.assertFalse(w.recovery_allowed({'failures': 2}, 10000))
        self.assertTrue(w.recovery_allowed({'failures': 3}, 10000))
        self.assertFalse(w.recovery_allowed({'failures': 3, 'attempts': [9500]}, 10000))
        self.assertFalse(w.recovery_allowed({'failures': 3, 'attempts': [7000, 8000]}, 10000))
        self.assertTrue(w.recovery_allowed({'failures': 3, 'attempts': [5000, 8000]}, 10000))

    def test_probe_has_external_timeout(self):
        with patch.object(w, 'run', side_effect=subprocess.TimeoutExpired('docker', 25)) as run:
            self.assertFalse(w.probe('helper'))
            self.assertEqual(run.call_args.kwargs['timeout'], 25)

    def test_healthy_probe_resets_failures(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'state.json'
            w.save(path, {'failures': 2, 'attempts': [100]})
            with patch.object(w, 'running', return_value=True), patch.object(w, 'probe', return_value=True), patch.object(w, 'recover') as recover:
                w.check(SimpleNamespace(helper='h', browser='b'), path)
            self.assertEqual(w.json.loads(path.read_text())['failures'], 0)
            recover.assert_not_called()

    def test_stopped_container_not_started(self):
        with tempfile.TemporaryDirectory() as d, patch.object(w, 'running', return_value=False), patch.object(w, 'recover') as recover:
            w.check(SimpleNamespace(helper='h', browser='b'), Path(d) / 'state.json')
            recover.assert_not_called()

    def test_history_saved_before_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'state.json'
            w.save(path, {'failures': 2})
            def recovery(*args):
                self.assertEqual(len(w.json.loads(path.read_text())['attempts']), 1)
                return True
            with patch.object(w, 'running', return_value=True), patch.object(w, 'probe', return_value=False), patch.object(w, 'recover', side_effect=recovery):
                w.check(SimpleNamespace(helper='h', browser='b'), path)

    def test_helper_only_recovery(self):
        with patch.object(w, 'run', return_value=SimpleNamespace(returncode=0, stdout='')) as run, patch.object(w, 'wait_ready', return_value=True):
            self.assertTrue(w.recover('h', 'b'))
            self.assertNotIn(['docker', 'restart', 'b'], [c.args[0] for c in run.call_args_list])

    def test_browser_escalation_order(self):
        with patch.object(w, 'run', return_value=SimpleNamespace(returncode=0, stdout='')) as run, patch.object(w, 'wait_ready', side_effect=[False, True]):
            self.assertTrue(w.recover('h', 'b'))
            self.assertEqual([c.args[0] for c in run.call_args_list][-3:], [
                ['docker', 'stop', 'h'], ['docker', 'restart', 'b'], ['docker', 'start', 'h']])

    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'state.json'
            path.write_text('invalid')
            with patch.object(w, 'recover') as recover:
                w.check(SimpleNamespace(helper='h', browser='b'), path)
                recover.assert_not_called()


if __name__ == '__main__':
    unittest.main()
