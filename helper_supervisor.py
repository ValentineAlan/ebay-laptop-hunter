"""Bound the synchronous Playwright worker even when Chromium stops responding."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

HEARTBEAT = Path('/tmp/ebay-helper-progress')
MAX_STALL_SECONDS = 180


def stalled(last_progress, now, limit=MAX_STALL_SECONDS):
    return now - last_progress > limit


def supervise(script):
    child = None

    def stop(*_):
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        HEARTBEAT.touch()
        child = subprocess.Popen(
            [sys.executable, '-u', script, '--worker'], start_new_session=True
        )
        observed = HEARTBEAT.stat().st_mtime_ns
        last_progress = time.monotonic()
        while child.poll() is None:
            time.sleep(2)
            try:
                current = HEARTBEAT.stat().st_mtime_ns
            except FileNotFoundError:
                current = observed
            if current != observed:
                observed = current
                last_progress = time.monotonic()
            if stalled(last_progress, time.monotonic()):
                print('Helper watchdog: worker stalled for 180s; restarting worker', flush=True)
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
                break
        time.sleep(5)
