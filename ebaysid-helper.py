#!/usr/bin/env python3

import hashlib
import json
import os
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

DATA = Path(os.environ.get("HUNTER_DATA", "/data"))
CDP_URL = os.environ.get("EBAY_CDP_URL", "http://127.0.0.1:9222")
SID_FILE = DATA / "ebaysid.current"
REQUEST_FILE = DATA / "ebaysid.refresh-request"
STATUS_FILE = DATA / "ebaysid-helper-status.json"
POLL_SECONDS = float(os.environ.get("EBAY_SID_POLL_SECONDS", "5"))


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def sid_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12] if value else None


def atomic_write(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_json(path, payload):
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True))


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def current_sid(context):
    for cookie in context.cookies("https://www.ebay.co.uk"):
        if cookie.get("name") == "ebaysid":
            return cookie.get("value")
    return None


def choose_page(context):
    for page in context.pages:
        if "ebay.co.uk/sh/research" in page.url:
            return page
    if context.pages:
        return context.pages[0]
    return context.new_page()


def research_url(keywords):
    params = {
        "marketplace": "EBAY-UK",
        "keywords": keywords or "Dell Latitude 5340",
        "dayRange": "90",
        "offset": "0",
        "limit": "50",
        "tabName": "SOLD",
        "tz": "Europe/London",
    }
    return "https://www.ebay.co.uk/sh/research?" + urllib.parse.urlencode(params)


def page_state(page):
    url = page.url or ""
    try:
        title = page.title()
    except Exception:
        title = ""
    low = (title + " " + url).lower()
    if "splashui" in low or "pardon our interruption" in low or "security measure" in low:
        return "CHALLENGED", title, url
    if "signin.ebay" in low or "sign in" in low:
        return "SIGNIN_REQUIRED", title, url
    if "/sh/research" in url:
        return "CONNECTED", title, url
    return "CONNECTED", title, url


def write_status(status, sid=None, **extra):
    previous = read_json(STATUS_FILE)
    payload = dict(previous)

    # A previous startup/CDP failure must not remain visible after the helper
    # has successfully reconnected.  Preserve last_error only while the
    # current state is ERROR, or when a caller explicitly supplies one.
    if status != "ERROR" and "last_error" not in extra:
        payload.pop("last_error", None)

    payload.update(extra)
    payload.update({
        "status": status,
        "last_seen_at": now_iso(),
        "ebaysid_hash": sid_hash(sid),
        "cdp_url": CDP_URL,
    })
    atomic_json(STATUS_FILE, payload)


def save_sid_if_changed(sid):
    if not sid:
        return False
    old = None
    try:
        old = SID_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    if old == sid:
        return False
    atomic_write(SID_FILE, sid)
    status = read_json(STATUS_FILE)
    status["last_sid_change_at"] = now_iso()
    status["ebaysid_hash"] = sid_hash(sid)
    status["last_seen_at"] = now_iso()
    status.setdefault("status", "CONNECTED")
    if status.get("status") != "ERROR":
        status.pop("last_error", None)
    atomic_json(STATUS_FILE, status)
    print(f"ebaysid updated: {sid_hash(sid)}", flush=True)
    return True


def process_refresh_request(context):
    if not REQUEST_FILE.exists():
        return
    request = read_json(REQUEST_FILE)
    requested_at = request.get("requested_at")
    keywords = request.get("keywords") or "Dell Latitude 5340"
    before = current_sid(context)
    print(
        "refresh requested"
        + (f" at {requested_at}" if requested_at else "")
        + f"; current sid={sid_hash(before)}",
        flush=True,
    )

    page = choose_page(context)
    try:
        page.goto(research_url(keywords), wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        after = current_sid(context)
        state, title, url = page_state(page)
        save_sid_if_changed(after)
        write_status(
            state,
            after,
            last_refresh_request_at=requested_at or now_iso(),
            last_refresh_completed_at=now_iso(),
            page_title=title,
            page_url=url,
            refresh_changed_sid=(after != before),
        )
        print(
            f"refresh complete: state={state} sid={sid_hash(after)} "
            f"changed={after != before}",
            flush=True,
        )
    except Exception as exc:
        write_status(
            "ERROR",
            before,
            last_refresh_request_at=requested_at or now_iso(),
            last_error=repr(exc),
        )
        print("refresh error:", repr(exc), flush=True)
    finally:
        try:
            REQUEST_FILE.unlink()
        except FileNotFoundError:
            pass


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    print(f"eBay SID helper starting; CDP={CDP_URL}", flush=True)

    while True:
        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(CDP_URL, timeout=10000)
                if not browser.contexts:
                    raise RuntimeError("No Chromium browser context")
                context = browser.contexts[0]
                print("connected to Chromium", flush=True)

                while True:
                    sid = current_sid(context)
                    save_sid_if_changed(sid)
                    page = choose_page(context)
                    state, title, url = page_state(page)
                    write_status(
                        state if sid else "NO_SESSION",
                        sid,
                        page_title=title,
                        page_url=url,
                    )
                    process_refresh_request(context)
                    time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            write_status("ERROR", last_error=repr(exc))
            print("helper connection error:", repr(exc), flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
