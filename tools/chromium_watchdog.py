#!/usr/bin/env python3
"""Host-side cron watchdog. Requires Docker; does not expose its socket to apps."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time

PROBE = '''
import json, os, time
from datetime import datetime
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(os.environ.get('EBAY_CDP_URL', 'http://127.0.0.1:9222'), timeout=10000)
    session = browser.new_browser_cdp_session()
    session.send('Browser.getVersion')
    for context in browser.contexts:
        for page in context.pages:
            page.evaluate('1 + 1')
    session.detach()
if os.environ.get('WATCHDOG_CHECK_HEARTBEAT') == '1':
    with open('/data/ebaysid-helper-status.json') as f:
        state = json.load(f)
    age = time.time() - datetime.fromisoformat(state['last_seen_at'].replace('Z', '+00:00')).timestamp()
    if age > 180 or age < -60:
        raise RuntimeError('helper heartbeat stale')
'''


def run(args, timeout=30):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def probe(helper, heartbeat=True):
    try:
        result = run(['docker', 'exec', '-e',
                      'WATCHDOG_CHECK_HEARTBEAT=' + ('1' if heartbeat else '0'),
                      helper, 'timeout', '--signal=KILL', '22s', 'python', '-c', PROBE], timeout=25)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def recovery_allowed(state, now):
    attempts = [t for t in state.get('attempts', []) if now - t < 3600]
    return (state.get('failures', 0) >= 3 and len(attempts) < 2
            and (not attempts or now - max(attempts) >= 900))


def save(path, state):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state))
    os.replace(temporary, path)


def log(message):
    print(datetime.now(timezone.utc).isoformat(), message, flush=True)


def running(name):
    result = run(['docker', 'inspect', '--format', '{{.State.Running}}', name])
    return result.returncode == 0 and result.stdout.strip() == 'true'


def wait_ready(helper):
    # Each probe has its own hard OS-side timeout, including hung page.evaluate.
    for _ in range(3):
        time.sleep(5)
        if probe(helper, heartbeat=False):
            return True
    return False


def recover(helper, browser):
    # Preserve evidence without printing environment variables, cookies or URLs.
    for command in (
        ['docker', 'stats', '--no-stream', '--format',
         '{{.Name}} CPU={{.CPUPerc}} MEM={{.MemUsage}} PIDS={{.PIDs}}', helper, browser],
        ['docker', 'inspect', '--format',
         '{{.Name}} OOM={{.State.OOMKilled}} Started={{.State.StartedAt}} Restarts={{.RestartCount}}',
         helper, browser],
    ):
        try:
            result = run(command)
            log(result.stdout.strip())
        except subprocess.TimeoutExpired:
            log('Diagnostic command timed out')
    log('Restarting helper')
    if run(['docker', 'restart', helper], timeout=45).returncode != 0:
        raise RuntimeError('helper restart failed')
    if wait_ready(helper):
        return True
    log('CDP still unresponsive; restarting browser and helper')
    if run(['docker', 'stop', helper], timeout=45).returncode != 0:
        raise RuntimeError('helper stop failed')
    try:
        if run(['docker', 'restart', browser], timeout=60).returncode != 0:
            raise RuntimeError('browser restart failed')
    finally:
        # Do not leave helper deliberately stopped if browser restart fails.
        if run(['docker', 'start', helper], timeout=45).returncode != 0:
            raise RuntimeError('helper start failed')
    return wait_ready(helper)


def check(args, path):
    try:
        state = json.loads(path.read_text()) if path.exists() else {}
    except (ValueError, OSError):
        # Fail closed: corrupted history must not bypass restart limits.
        log('Invalid watchdog state; refusing automatic restart')
        return
    if not running(args.helper) or not running(args.browser):
        state['failures'] = 0
        save(path, state)
        log('Container stopped or absent; leaving deployment unchanged')
        return
    healthy = probe(args.helper)
    state['failures'] = 0 if healthy else state.get('failures', 0) + 1
    now = time.time()
    if healthy:
        save(path, state)
        return
    log('Unresponsive browser/helper: consecutive checks=' + str(state['failures']))
    if not recovery_allowed(state, now):
        save(path, state)
        return
    state['attempts'] = [t for t in state.get('attempts', []) if now - t < 3600] + [now]
    state['failures'] = 0
    save(path, state)  # Persist before mutations, including unsuccessful attempts.
    try:
        recovered = recover(args.helper, args.browser)
        log('Control connection recovered' if recovered else 'Recovery failed; restart limits remain active')
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        log('Recovery failed: ' + str(exc))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--helper', default='ix-ebay-hunter-stack-sid-helper-1')
    parser.add_argument('--browser', default='ix-ebay-hunter-stack-browser-1')
    parser.add_argument('--state-dir', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    directory = Path(args.state_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'watchdog.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        check(args, directory / 'state.json')


if __name__ == '__main__':
    main()
